"""Tool definitions and dispatch for the conversation engine.

Every tool is `strict` — the API then guarantees the input validates against the
schema, so handlers can trust their arguments. Handlers return plain dicts that
get JSON-encoded straight into the `tool_result` block.

`update_lead_profile` is not in the original tool list in CLAUDE.md; the engine
needs a way to persist what it learns mid-conversation rather than only at the
end. See docs/decisions.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.scoring import ScoringInput, active_listing_prices, score_lead
from app.core.logging import get_logger, mask_phone
from app.models import Agent, Appointment, Conversation, Lead, Listing
from app.models.enums import (
    AppointmentStatus,
    AppointmentType,
    ConversationStatus,
    LeadPurpose,
    LeadStatus,
    ListingStatus,
    PropertyType,
)
from app.services.booking import sync_to_calendar
from app.services.google_calendar import GoogleCalendarClient
from app.services.scheduling import (
    SLOT_MINUTES,
    find_available_slots,
    resolve_timezone,
)

log = get_logger(__name__)

MAX_LISTING_RESULTS = 5


class ToolError(Exception):
    """A tool failed in a way the model should see and react to."""


@dataclass
class ToolContext:
    """Per-turn state a tool handler may read or mutate."""

    session: AsyncSession
    agent: Agent
    lead: Lead
    conversation: Conversation
    inbound_message_count: int = 0
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Set when the agent has connected Google Calendar (M4); None runs DB-only.
    calendar: GoogleCalendarClient | None = None
    # Side effects the engine acts on after the turn.
    escalated: bool = False
    escalation_reason: str | None = None
    booked_appointment_id: uuid.UUID | None = None
    calendar_sync_failed: str | None = None


def _decimal(value: float | int | str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ToolError(f"{value!r} is not a valid amount") from exc


# --------------------------------------------------------------------------- schemas

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_listing_details",
        "description": (
            "Look up properties in this agent's inventory. Call with a listing_id for one "
            "specific property, or with criteria to find what matches a lead's "
            "requirements. This is the only source of property facts — price, size, "
            "locality, RERA number, description. If a detail is not in the result, it is "
            "not known and must not be stated."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "listing_id": {
                    "type": ["string", "null"],
                    "description": "UUID of a specific listing, when known.",
                },
                "city": {"type": ["string", "null"], "description": "City to search in."},
                "locality": {
                    "type": ["string", "null"],
                    "description": "Locality or area name, e.g. Bopal.",
                },
                "property_type": {
                    "type": ["string", "null"],
                    "enum": [*(t.value for t in PropertyType), None],
                },
                "bhk": {"type": ["integer", "null"], "description": "Number of bedrooms."},
                "budget_min": {"type": ["number", "null"], "description": "Minimum price in INR."},
                "budget_max": {"type": ["number", "null"], "description": "Maximum price in INR."},
            },
            "required": [
                "listing_id",
                "city",
                "locality",
                "property_type",
                "bhk",
                "budget_min",
                "budget_max",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_lead_profile",
        "description": (
            "Record what you have learned about this lead. Call it as soon as they tell "
            "you something, not once at the end. Pass null for anything they have not "
            "told you — null leaves the existing value untouched."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
                "budget_min": {"type": ["number", "null"], "description": "In INR."},
                "budget_max": {"type": ["number", "null"], "description": "In INR."},
                "preferred_locations": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
                "property_type": {
                    "type": ["string", "null"],
                    "enum": [*(t.value for t in PropertyType), None],
                },
                "bhk": {"type": ["integer", "null"]},
                "timeline_months": {
                    "type": ["integer", "null"],
                    "description": "Months until they intend to buy.",
                },
                "loan_preapproved": {"type": ["boolean", "null"]},
                "purpose": {
                    "type": ["string", "null"],
                    "enum": [*(p.value for p in LeadPurpose), None],
                },
                "site_visit_willing": {"type": ["boolean", "null"]},
                "notes": {
                    "type": ["string", "null"],
                    "description": "Anything else the human agent should know.",
                },
            },
            "required": [
                "name",
                "budget_min",
                "budget_max",
                "preferred_locations",
                "property_type",
                "bhk",
                "timeline_months",
                "loan_preapproved",
                "purpose",
                "site_visit_willing",
                "notes",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "score_lead",
        "description": (
            "Recompute this lead's 0-100 score from what has been recorded so far. Call "
            "after you have their budget and timeline. Returns the score, the hot/warm/"
            "cold band, and the reason for every factor — the human agent reads these."
        ),
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "check_availability",
        "description": (
            "Real open slots on the agent's calendar. Offer the lead two or three of "
            "these; never invent a time or promise one you have not checked."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "appointment_type": {
                    "type": "string",
                    "enum": [t.value for t in AppointmentType],
                    "description": "call for a phone call, site_visit to view a property.",
                },
                "search_days": {
                    "type": ["integer", "null"],
                    "description": "How many days ahead to look. Defaults to 7.",
                },
            },
            "required": ["appointment_type", "search_days"],
            "additionalProperties": False,
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Book a slot the lead has agreed to. Only use a starts_at returned by "
            "check_availability, and only after they have confirmed that specific time."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "starts_at": {
                    "type": "string",
                    "description": (
                        "ISO-8601 UTC start time, exactly as check_availability returned it."
                    ),
                },
                "appointment_type": {
                    "type": "string",
                    "enum": [t.value for t in AppointmentType],
                },
                "listing_id": {
                    "type": ["string", "null"],
                    "description": "The property for a site visit, when there is one.",
                },
                "notes": {
                    "type": ["string", "null"],
                    "description": "Anything the agent should know before the meeting.",
                },
            },
            "required": ["starts_at", "appointment_type", "listing_id", "notes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Hand this conversation to the human agent. Use it for a request to speak to "
            "a person, price negotiation, a high-value budget, frustration, or anything "
            "legal or contractual. After calling it, tell the lead the agent will follow "
            "up shortly — and stop trying to handle the issue yourself."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "One line the human agent will read as context.",
                },
                "urgency": {"type": "string", "enum": ["normal", "high"]},
            },
            "required": ["reason", "urgency"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = frozenset(schema["name"] for schema in TOOL_SCHEMAS)


# -------------------------------------------------------------------------- handlers


def _serialise_listing(listing: Listing) -> dict[str, Any]:
    return {
        "listing_id": str(listing.id),
        "title": listing.title,
        "property_type": listing.property_type.value,
        "status": listing.status.value,
        "locality": listing.locality,
        "city": listing.city,
        "price_inr": float(listing.price),
        "bhk": listing.bhk,
        "carpet_area_sqft": listing.carpet_area_sqft,
        "description": listing.description,
        "rera_id": listing.rera_id,
    }


async def _get_listing_details(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = select(Listing).where(
        Listing.agent_id == ctx.agent.id,
        Listing.is_active.is_(True),
        Listing.status == ListingStatus.AVAILABLE,
    )

    if listing_id := args.get("listing_id"):
        try:
            query = query.where(Listing.id == uuid.UUID(str(listing_id)))
        except ValueError as exc:
            raise ToolError(f"{listing_id!r} is not a valid listing id") from exc
    else:
        if city := args.get("city"):
            query = query.where(Listing.city.ilike(f"%{city}%"))
        if locality := args.get("locality"):
            query = query.where(Listing.locality.ilike(f"%{locality}%"))
        if property_type := args.get("property_type"):
            query = query.where(Listing.property_type == PropertyType(property_type))
        if (bhk := args.get("bhk")) is not None:
            query = query.where(Listing.bhk == bhk)
        if (budget_min := _decimal(args.get("budget_min"))) is not None:
            query = query.where(Listing.price >= budget_min)
        if (budget_max := _decimal(args.get("budget_max"))) is not None:
            query = query.where(Listing.price <= budget_max)

    result = await ctx.session.execute(query.order_by(Listing.price).limit(MAX_LISTING_RESULTS))
    listings = list(result.scalars().all())
    return {
        "count": len(listings),
        "listings": [_serialise_listing(listing) for listing in listings],
        "note": (
            "No matching properties in this agent's inventory."
            if not listings
            else "These are the only properties you may describe."
        ),
    }


async def _update_lead_profile(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    lead = ctx.lead
    updated: list[str] = []

    simple_fields = ("name", "bhk", "timeline_months", "loan_preapproved", "site_visit_willing")
    for key in simple_fields:
        value = args.get(key)
        if value is not None:
            setattr(lead, key, value)
            updated.append(key)

    for key in ("budget_min", "budget_max"):
        if (amount := _decimal(args.get(key))) is not None:
            setattr(lead, key, amount)
            updated.append(key)

    if locations := args.get("preferred_locations"):
        lead.preferred_locations = list(locations)
        updated.append("preferred_locations")

    if property_type := args.get("property_type"):
        lead.property_type = PropertyType(property_type)
        updated.append("property_type")

    if purpose := args.get("purpose"):
        lead.purpose = LeadPurpose(purpose)
        updated.append("purpose")

    if notes := args.get("notes"):
        lead.notes = f"{lead.notes}\n{notes}" if lead.notes else notes
        updated.append("notes")

    if lead.status is LeadStatus.NEW and updated:
        lead.status = LeadStatus.ENGAGED

    await ctx.session.flush()
    return {"updated_fields": updated, "lead_status": lead.status.value}


async def _score_lead(ctx: ToolContext, _args: dict[str, Any]) -> dict[str, Any]:
    result = await ctx.session.execute(select(Listing).where(Listing.agent_id == ctx.agent.id))
    prices = active_listing_prices(list(result.scalars().all()))

    lead = ctx.lead
    outcome = score_lead(
        ScoringInput(
            budget_min=lead.budget_min,
            budget_max=lead.budget_max,
            timeline_months=lead.timeline_months,
            loan_preapproved=lead.loan_preapproved,
            site_visit_willing=lead.site_visit_willing,
            inbound_message_count=ctx.inbound_message_count,
            status=lead.status,
        ),
        prices,
    )

    lead.score = outcome.score
    lead.temperature = outcome.temperature
    lead.score_reasons = outcome.reason_dicts
    lead.scored_at = ctx.now
    if outcome.temperature.value != "cold" and lead.status is LeadStatus.ENGAGED:
        lead.status = LeadStatus.QUALIFIED
    await ctx.session.flush()

    return {
        "score": outcome.score,
        "temperature": outcome.temperature.value,
        "reasons": outcome.reason_dicts,
    }


async def _check_availability(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    appointment_type = AppointmentType(args["appointment_type"])
    slots = await find_available_slots(
        ctx.session,
        ctx.agent,
        appointment_type=appointment_type,
        search_days=args.get("search_days") or 7,
        now=ctx.now,
        calendar=ctx.calendar,
    )
    tz = resolve_timezone(ctx.agent.timezone)
    return {
        "timezone": ctx.agent.timezone,
        "slots": [
            {"starts_at": slot.starts_at.isoformat(), "label": slot.label(tz)} for slot in slots
        ],
        "note": (
            "No open slots in this window — offer to have the agent call back instead."
            if not slots
            else "Offer two or three of these. Use starts_at verbatim when booking."
        ),
    }


async def _book_appointment(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        starts_at = datetime.fromisoformat(args["starts_at"])
    except ValueError as exc:
        raise ToolError(
            f"{args['starts_at']!r} is not a valid ISO-8601 timestamp; "
            "use a starts_at from check_availability"
        ) from exc
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    if starts_at <= ctx.now:
        raise ToolError("That slot is in the past. Call check_availability for current slots.")

    appointment_type = AppointmentType(args["appointment_type"])

    listing_id: uuid.UUID | None = None
    if raw_listing_id := args.get("listing_id"):
        try:
            listing_id = uuid.UUID(str(raw_listing_id))
        except ValueError as exc:
            raise ToolError(f"{raw_listing_id!r} is not a valid listing id") from exc

    available = await find_available_slots(
        ctx.session,
        ctx.agent,
        appointment_type=appointment_type,
        search_days=30,
        limit=200,
        now=ctx.now,
        calendar=ctx.calendar,
    )
    if not any(slot.starts_at == starts_at for slot in available):
        raise ToolError(
            "That slot is no longer available. Call check_availability and offer a fresh time."
        )

    appointment = Appointment(
        lead_id=ctx.lead.id,
        agent_id=ctx.agent.id,
        listing_id=listing_id,
        appointment_type=appointment_type,
        status=AppointmentStatus.CONFIRMED,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=SLOT_MINUTES[appointment_type]),
        timezone=ctx.agent.timezone,
        notes=args.get("notes"),
    )
    ctx.session.add(appointment)
    ctx.lead.status = LeadStatus.BOOKED
    await ctx.session.flush()

    ctx.booked_appointment_id = appointment.id
    tz = resolve_timezone(ctx.agent.timezone)
    log.info(
        "booked %s for lead %s (%s)",
        appointment_type.value,
        ctx.lead.id,
        mask_phone(ctx.lead.phone),
    )

    listing = await ctx.session.get(Listing, listing_id) if listing_id else None
    sync = await sync_to_calendar(
        ctx.session, ctx.calendar, ctx.agent, ctx.lead, appointment, listing, now=ctx.now
    )
    if sync.needs_attention:
        # The lead is about to be told this is confirmed, so keep the booking and
        # make sure a human learns the calendar does not reflect it.
        ctx.calendar_sync_failed = sync.reason

    return {
        "appointment_id": str(appointment.id),
        "starts_at": starts_at.isoformat(),
        "local_time": starts_at.astimezone(tz).strftime("%a %d %b, %I:%M %p"),
        "appointment_type": appointment_type.value,
        "status": appointment.status.value,
        "calendar_synced": sync.synced,
        "note": (
            "Booked. Confirm the day and time back to the lead in your reply."
            if sync.synced or sync.reason is None
            else "Booked, but it is not on the agent's calendar — they have been alerted. "
            "Still confirm the time to the lead; do not mention the calendar problem."
        ),
    }


async def _escalate_to_human(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    reason = args["reason"]
    ctx.lead.status = LeadStatus.HANDED_OFF
    ctx.lead.handed_off_at = ctx.now
    ctx.lead.handoff_reason = reason[:255]
    ctx.conversation.status = ConversationStatus.HUMAN_TAKEOVER
    await ctx.session.flush()

    ctx.escalated = True
    ctx.escalation_reason = reason
    log.info("escalated lead %s to human: %s", ctx.lead.id, reason)
    return {
        "escalated": True,
        "urgency": args.get("urgency", "normal"),
        "note": (
            f"{ctx.agent.name} has been notified. Tell the lead they will hear from "
            "them shortly, and do not attempt to resolve the issue yourself."
        ),
    }


HANDLERS = {
    "get_listing_details": _get_listing_details,
    "update_lead_profile": _update_lead_profile,
    "score_lead": _score_lead,
    "check_availability": _check_availability,
    "book_appointment": _book_appointment,
    "escalate_to_human": _escalate_to_human,
}


async def dispatch(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool. Raises ToolError for failures the model should see."""
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool {name!r}")
    return await handler(ctx, args)
