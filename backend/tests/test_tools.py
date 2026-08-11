from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import TOOL_SCHEMAS, ToolContext, ToolError, dispatch
from app.models import Appointment, Conversation, Listing
from app.models.enums import (
    Channel,
    ConversationStatus,
    LeadStatus,
    LeadTemperature,
    ListingStatus,
    PropertyType,
)
from tests.factories import make_agent, make_lead, make_listing

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 12, 6, 0, tzinfo=IST).astimezone(UTC)


async def build_ctx(session: AsyncSession, with_listings: bool = True) -> ToolContext:
    agent = make_agent()
    session.add(agent)
    await session.flush()

    if with_listings:
        session.add_all(
            [
                make_listing(
                    agent,
                    title="2 BHK SG Highway",
                    price=Decimal("6200000"),
                    bhk=2,
                    locality="SG Highway",
                ),
                make_listing(
                    agent, title="3 BHK Bopal", price=Decimal("8500000"), bhk=3, locality="Bopal"
                ),
                make_listing(
                    agent,
                    title="Villa Shela",
                    price=Decimal("21500000"),
                    bhk=4,
                    locality="Shela",
                    property_type=PropertyType.VILLA,
                ),
                make_listing(
                    agent, title="Sold flat", price=Decimal("7000000"), status=ListingStatus.SOLD
                ),
            ]
        )

    lead = make_lead(agent)
    session.add(lead)
    await session.flush()
    conversation = Conversation(lead_id=lead.id, channel=Channel.CLI)
    session.add(conversation)
    await session.flush()

    return ToolContext(session=session, agent=agent, lead=lead, conversation=conversation, now=NOW)


# ------------------------------------------------------------------ schemas


def test_every_tool_from_the_spec_is_defined() -> None:
    names = {schema["name"] for schema in TOOL_SCHEMAS}

    assert {
        "check_availability",
        "book_appointment",
        "get_listing_details",
        "score_lead",
        "escalate_to_human",
    } <= names


def test_schemas_are_strict_and_closed() -> None:
    for schema in TOOL_SCHEMAS:
        assert schema["strict"] is True, schema["name"]
        assert schema["input_schema"]["additionalProperties"] is False, schema["name"]
        properties = schema["input_schema"].get("properties", {})
        # `required` must name real properties; optional ones are simply absent.
        assert set(schema["input_schema"].get("required", [])) <= set(properties), schema["name"]
        assert len(schema["description"]) > 40, schema["name"]


# --------------------------------------------------------- get_listing_details


async def test_listing_search_filters_by_budget_and_bhk(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    result = await dispatch(
        ctx,
        "get_listing_details",
        {
            "budget_max": 9000000,
            "bhk": 3,
            "listing_id": None,
            "city": None,
            "locality": None,
            "property_type": None,
            "budget_min": None,
        },
    )

    assert result["count"] == 1
    assert result["listings"][0]["title"] == "3 BHK Bopal"


async def test_listing_search_excludes_sold_inventory(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    result = await dispatch(
        ctx,
        "get_listing_details",
        dict.fromkeys(
            ["listing_id", "city", "locality", "property_type", "bhk", "budget_min", "budget_max"]
        ),
    )

    titles = [listing["title"] for listing in result["listings"]]
    assert "Sold flat" not in titles


async def test_no_matches_says_so_rather_than_returning_nothing(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    result = await dispatch(
        ctx,
        "get_listing_details",
        {
            "budget_max": 100000,
            "listing_id": None,
            "city": None,
            "locality": None,
            "property_type": None,
            "bhk": None,
            "budget_min": None,
        },
    )

    assert result["count"] == 0
    assert "No matching properties" in result["note"]


async def test_bad_listing_id_is_a_tool_error_not_a_crash(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    with pytest.raises(ToolError):
        await dispatch(
            ctx,
            "get_listing_details",
            {
                "listing_id": "not-a-uuid",
                "city": None,
                "locality": None,
                "property_type": None,
                "bhk": None,
                "budget_min": None,
                "budget_max": None,
            },
        )


# --------------------------------------------------------- update_lead_profile


async def test_profile_update_persists_and_engages_the_lead(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    result = await dispatch(
        ctx,
        "update_lead_profile",
        {
            "name": "Priya Shah",
            "budget_min": 6000000,
            "budget_max": 9000000,
            "preferred_locations": ["Bopal", "Shela"],
            "property_type": "flat",
            "bhk": 3,
            "timeline_months": 2,
            "loan_preapproved": True,
            "purpose": "self_use",
            "site_visit_willing": True,
            "notes": "Prefers east-facing",
        },
    )

    assert ctx.lead.budget_max == Decimal("9000000")
    assert ctx.lead.preferred_locations == ["Bopal", "Shela"]
    assert ctx.lead.property_type is PropertyType.FLAT
    assert ctx.lead.status is LeadStatus.ENGAGED
    assert result["lead_status"] == "engaged"


async def test_nulls_leave_existing_values_alone(session: AsyncSession) -> None:
    ctx = await build_ctx(session)
    ctx.lead.bhk = 3
    ctx.lead.timeline_months = 2

    await dispatch(
        ctx,
        "update_lead_profile",
        {
            **dict.fromkeys(
                [
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
                ]
            ),
            "loan_preapproved": True,
        },
    )

    assert ctx.lead.bhk == 3
    assert ctx.lead.timeline_months == 2
    assert ctx.lead.loan_preapproved is True


# ------------------------------------------------------------------ score_lead


async def test_score_lead_persists_score_and_reasons(session: AsyncSession) -> None:
    ctx = await build_ctx(session)
    ctx.lead.budget_min = Decimal("6000000")
    ctx.lead.budget_max = Decimal("9000000")
    ctx.lead.timeline_months = 2
    ctx.lead.loan_preapproved = True
    ctx.lead.status = LeadStatus.ENGAGED
    ctx.inbound_message_count = 4

    result = await dispatch(ctx, "score_lead", {})

    assert result["score"] == ctx.lead.score
    assert result["temperature"] == LeadTemperature.HOT.value
    assert ctx.lead.score_reasons == result["reasons"]
    assert ctx.lead.scored_at == NOW
    assert ctx.lead.status is LeadStatus.QUALIFIED


async def test_score_lead_only_counts_this_agents_inventory(session: AsyncSession) -> None:
    ctx = await build_ctx(session, with_listings=False)
    other = make_agent(email="other@example.com", phone="+919876500009")
    session.add(other)
    await session.flush()
    session.add(make_listing(other, price=Decimal("8000000")))
    await session.flush()
    ctx.lead.budget_max = Decimal("9000000")

    result = await dispatch(ctx, "score_lead", {})

    budget = next(r for r in result["reasons"] if r["factor"] == "budget_match")
    assert budget["points"] == 0


# ----------------------------------------------------- availability + booking


async def test_check_availability_returns_usable_slots(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    result = await dispatch(
        ctx, "check_availability", {"appointment_type": "call", "search_days": 3}
    )

    assert result["slots"]
    assert all("starts_at" in slot and "label" in slot for slot in result["slots"])


async def test_booking_a_returned_slot_creates_the_appointment(session: AsyncSession) -> None:
    ctx = await build_ctx(session)
    listing = (await session.execute(select(Listing).limit(1))).scalar_one()
    slots = await dispatch(
        ctx, "check_availability", {"appointment_type": "site_visit", "search_days": 3}
    )
    chosen = slots["slots"][0]["starts_at"]

    result = await dispatch(
        ctx,
        "book_appointment",
        {
            "starts_at": chosen,
            "appointment_type": "site_visit",
            "listing_id": str(listing.id),
            "notes": "Wants to see the balcony",
        },
    )

    appointment = (await session.execute(select(Appointment))).scalar_one()
    assert str(appointment.id) == result["appointment_id"]
    assert appointment.listing_id == listing.id
    assert ctx.lead.status is LeadStatus.BOOKED
    assert ctx.booked_appointment_id == appointment.id


async def test_booking_an_unoffered_slot_is_rejected(session: AsyncSession) -> None:
    ctx = await build_ctx(session)
    # 3am is outside working hours and was never offered.
    off_hours = datetime(2026, 8, 13, 3, 0, tzinfo=IST).astimezone(UTC)

    with pytest.raises(ToolError, match="no longer available"):
        await dispatch(
            ctx,
            "book_appointment",
            {
                "starts_at": off_hours.isoformat(),
                "appointment_type": "call",
                "listing_id": None,
                "notes": None,
            },
        )


async def test_booking_a_past_slot_is_rejected(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    with pytest.raises(ToolError, match="past"):
        await dispatch(
            ctx,
            "book_appointment",
            {
                "starts_at": (NOW - timedelta(days=1)).isoformat(),
                "appointment_type": "call",
                "listing_id": None,
                "notes": None,
            },
        )


async def test_double_booking_the_same_slot_is_rejected(session: AsyncSession) -> None:
    ctx = await build_ctx(session)
    slots = await dispatch(
        ctx, "check_availability", {"appointment_type": "call", "search_days": 3}
    )
    chosen = slots["slots"][0]["starts_at"]
    args = {"starts_at": chosen, "appointment_type": "call", "listing_id": None, "notes": None}
    await dispatch(ctx, "book_appointment", args)

    with pytest.raises(ToolError, match="no longer available"):
        await dispatch(ctx, "book_appointment", args)


async def test_garbage_timestamp_is_a_tool_error(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    with pytest.raises(ToolError, match="ISO-8601"):
        await dispatch(
            ctx,
            "book_appointment",
            {
                "starts_at": "next tuesday",
                "appointment_type": "call",
                "listing_id": None,
                "notes": None,
            },
        )


# ------------------------------------------------------------------ escalation


async def test_escalation_marks_lead_and_conversation(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    result = await dispatch(
        ctx, "escalate_to_human", {"reason": "Wants to negotiate price", "urgency": "high"}
    )

    assert result["escalated"] is True
    assert ctx.escalated is True
    assert ctx.lead.status is LeadStatus.HANDED_OFF
    assert ctx.lead.handoff_reason == "Wants to negotiate price"
    assert ctx.lead.handed_off_at == NOW
    assert ctx.conversation.status is ConversationStatus.HUMAN_TAKEOVER


async def test_unknown_tool_raises(session: AsyncSession) -> None:
    ctx = await build_ctx(session)

    with pytest.raises(ToolError, match="Unknown tool"):
        await dispatch(ctx, "delete_everything", {})


def test_no_schema_uses_a_type_array_union() -> None:
    """`{"type": ["string", "null"]}` is valid JSON Schema but the Messages API
    rejects it — and with an enum it fails outright. Use `nullable()` (anyOf).

    This only ever showed up on a live API call; every offline test passed.
    """
    offenders: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("type"), list):
                offenders.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    for schema in TOOL_SCHEMAS:
        walk(schema["input_schema"], schema["name"])

    assert offenders == [], f"use nullable() for these: {offenders}"


def test_union_typed_parameters_stay_under_the_api_limit() -> None:
    """The API caps union-typed parameters at 16 across all tools. Optional fields
    are expressed by omission from `required`, so this should be zero."""
    unions = 0
    for schema in TOOL_SCHEMAS:
        for prop in schema["input_schema"].get("properties", {}).values():
            if isinstance(prop.get("type"), list) or "anyOf" in prop:
                unions += 1

    assert unions <= 16, f"{unions} union-typed parameters; the API rejects more than 16"


def test_the_tools_that_need_arguments_require_them() -> None:
    required = {s["name"]: set(s["input_schema"].get("required", [])) for s in TOOL_SCHEMAS}

    assert required["book_appointment"] == {"starts_at", "appointment_type"}
    assert required["check_availability"] == {"appointment_type"}
    assert required["escalate_to_human"] == {"reason", "urgency"}
    # Search and profile updates are entirely optional by design.
    assert required["get_listing_details"] == set()
    assert required["update_lead_profile"] == set()
