"""Booking + calendar sync, including the failure paths that matter commercially."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.engine import ConversationEngine
from app.agent.tools import ToolContext, ToolError, dispatch
from app.core.config import Settings
from app.models import Appointment, Conversation, Listing
from app.models.enums import (
    AppointmentStatus,
    AppointmentType,
    Channel,
    LeadStatus,
    LeadTemperature,
)
from app.services.booking import event_description, event_summary, sync_to_calendar
from app.services.google_calendar import TOKEN_CACHE, GoogleCalendarClient
from app.services.scheduling import find_available_slots
from tests.factories import make_agent, make_lead, make_listing
from tests.fakes import FakeLLM, text_turn, tool_turn

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 12, 6, 0, tzinfo=IST).astimezone(UTC)


def google_settings() -> Settings:
    return Settings(
        google_client_id="cid",
        google_client_secret="csecret",
        http_max_retries=1,
        http_backoff_base_seconds=0.0,
        anthropic_api_key="test",
    )


def calendar_client(handler: object) -> GoogleCalendarClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return GoogleCalendarClient(google_settings(), client=client)


def token(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})


async def build(session: AsyncSession, *, connected: bool = True) -> ToolContext:
    agent = make_agent(
        google_refresh_token="refresh-1" if connected else None,
        google_calendar_id="primary" if connected else None,
    )
    session.add(agent)
    await session.flush()
    TOKEN_CACHE.drop(agent.id)
    listing = make_listing(agent, price=Decimal("8500000"), bhk=3, locality="Bopal")
    session.add(listing)
    lead = make_lead(agent, email="priya@example.com")
    session.add(lead)
    await session.flush()
    conversation = Conversation(lead_id=lead.id, channel=Channel.CLI)
    session.add(conversation)
    await session.flush()
    return ToolContext(session=session, agent=agent, lead=lead, conversation=conversation, now=NOW)


async def first_slot(ctx: ToolContext, calendar: GoogleCalendarClient | None = None) -> str:
    slots = await find_available_slots(
        ctx.session,
        ctx.agent,
        appointment_type=AppointmentType.SITE_VISIT,
        search_days=3,
        now=NOW,
        calendar=calendar,
    )
    return slots[0].starts_at.isoformat()


# ------------------------------------------------------- free/busy in slotting


async def test_google_busy_times_remove_slots(session: AsyncSession) -> None:
    ctx = await build(session)
    baseline = await find_available_slots(session, ctx.agent, search_days=1, now=NOW)
    blocked = baseline[0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token(request)
        return httpx.Response(
            200,
            json={
                "calendars": {
                    "primary": {
                        "busy": [
                            {
                                "start": blocked.starts_at.isoformat(),
                                "end": blocked.ends_at.isoformat(),
                            }
                        ]
                    }
                }
            },
        )

    slots = await find_available_slots(
        session, ctx.agent, search_days=1, now=NOW, calendar=calendar_client(handler)
    )

    assert blocked.starts_at not in [s.starts_at for s in slots]


async def test_google_outage_degrades_to_our_own_appointments(session: AsyncSession) -> None:
    """A Google outage must not stop the agent taking bookings."""
    ctx = await build(session)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token(request)
        return httpx.Response(503, json={"error": {"message": "backend error"}})

    slots = await find_available_slots(
        session, ctx.agent, search_days=1, now=NOW, calendar=calendar_client(handler)
    )

    assert slots  # still offering times


async def test_unconnected_agent_never_calls_google(session: AsyncSession) -> None:
    ctx = await build(session, connected=False)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    slots = await find_available_slots(
        session, ctx.agent, search_days=1, now=NOW, calendar=calendar_client(handler)
    )

    assert calls["n"] == 0
    assert slots


# ------------------------------------------------------------- event contents


async def test_event_summary_and_description_brief_the_agent(session: AsyncSession) -> None:
    ctx = await build(session)
    ctx.lead.name = "Priya Shah"
    ctx.lead.budget_min = Decimal("6000000")
    ctx.lead.budget_max = Decimal("9000000")
    ctx.lead.bhk = 3
    ctx.lead.timeline_months = 2
    ctx.lead.loan_preapproved = True
    ctx.lead.score = 80
    ctx.lead.temperature = LeadTemperature.HOT
    ctx.lead.score_reasons = [{"factor": "timeline", "points": 25, "detail": "Buying in 2 months"}]
    listing = (await session.execute(select(Listing))).scalars().first()
    appointment = Appointment(
        lead_id=ctx.lead.id,
        agent_id=ctx.agent.id,
        appointment_type=AppointmentType.SITE_VISIT,
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        notes="Wants an east-facing unit",
    )

    summary = event_summary(ctx.lead, appointment, listing)
    description = event_description(ctx.lead, appointment, listing)

    assert "Priya Shah" in summary
    assert listing is not None and listing.title in summary
    # The agent needs the lead's real number to call them — this is their own event.
    assert ctx.lead.phone in description
    assert "80/100 (hot)" in description
    assert "Buying in 2 months" in description
    assert "Wants an east-facing unit" in description
    assert "3 BHK" in description


# ------------------------------------------------------------------ sync paths


async def test_successful_sync_records_the_event_id(session: AsyncSession) -> None:
    ctx = await build(session)
    appointment = Appointment(
        lead_id=ctx.lead.id,
        agent_id=ctx.agent.id,
        appointment_type=AppointmentType.CALL,
        starts_at=NOW + timedelta(days=1),
        ends_at=NOW + timedelta(days=1, minutes=30),
    )
    session.add(appointment)
    await session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token(request)
        return httpx.Response(200, json={"id": "evt_abc", "htmlLink": "https://cal/evt_abc"})

    outcome = await sync_to_calendar(
        session, calendar_client(handler), ctx.agent, ctx.lead, appointment, None, now=NOW
    )

    assert outcome.synced is True
    assert appointment.google_event_id == "evt_abc"
    assert appointment.confirmation_sent_at == NOW


async def test_unconnected_agent_syncs_nothing_and_is_not_an_error(
    session: AsyncSession,
) -> None:
    ctx = await build(session, connected=False)
    appointment = Appointment(
        lead_id=ctx.lead.id,
        agent_id=ctx.agent.id,
        appointment_type=AppointmentType.CALL,
        starts_at=NOW + timedelta(days=1),
        ends_at=NOW + timedelta(days=1, minutes=30),
    )
    session.add(appointment)
    await session.flush()

    outcome = await sync_to_calendar(
        session, calendar_client(token), ctx.agent, ctx.lead, appointment, None, now=NOW
    )

    assert outcome.synced is False
    assert outcome.needs_attention is False  # nothing to alert anyone about


async def test_calendar_failure_keeps_the_booking_and_flags_it(session: AsyncSession) -> None:
    ctx = await build(session)
    appointment = Appointment(
        lead_id=ctx.lead.id,
        agent_id=ctx.agent.id,
        appointment_type=AppointmentType.CALL,
        starts_at=NOW + timedelta(days=1),
        ends_at=NOW + timedelta(days=1, minutes=30),
    )
    session.add(appointment)
    await session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token(request)
        return httpx.Response(503, json={"error": {"message": "down"}})

    outcome = await sync_to_calendar(
        session, calendar_client(handler), ctx.agent, ctx.lead, appointment, None, now=NOW
    )

    assert outcome.synced is False
    assert outcome.needs_attention is True
    assert appointment.google_event_id is None  # booking survives, event does not


async def test_revoked_access_is_flagged_for_reconnection(session: AsyncSession) -> None:
    ctx = await build(session)
    appointment = Appointment(
        lead_id=ctx.lead.id,
        agent_id=ctx.agent.id,
        appointment_type=AppointmentType.CALL,
        starts_at=NOW + timedelta(days=1),
        ends_at=NOW + timedelta(days=1, minutes=30),
    )
    session.add(appointment)
    await session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    outcome = await sync_to_calendar(
        session, calendar_client(handler), ctx.agent, ctx.lead, appointment, None, now=NOW
    )

    assert outcome.needs_attention is True
    assert "revoked" in (outcome.reason or "")


# ------------------------------------------------------- through the tool/engine


async def test_book_appointment_creates_the_calendar_event(session: AsyncSession) -> None:
    ctx = await build(session)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token(request)
        if request.url.path.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {"primary": {"busy": []}}})
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "evt_booked"})

    ctx.calendar = calendar_client(handler)
    starts_at = await first_slot(ctx, ctx.calendar)

    result = await dispatch(
        ctx,
        "book_appointment",
        {
            "starts_at": starts_at,
            "appointment_type": "site_visit",
            "listing_id": None,
            "notes": "Prefers morning",
        },
    )

    assert result["calendar_synced"] is True
    appointment = (await session.execute(select(Appointment))).scalar_one()
    assert appointment.google_event_id == "evt_booked"
    assert appointment.status is AppointmentStatus.CONFIRMED
    assert "Prefers morning" in str(captured["body"])


async def test_booking_escalates_when_the_calendar_write_fails(session: AsyncSession) -> None:
    """The lead is told it is confirmed, so a human must learn the calendar missed it."""
    ctx = await build(session)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token(request)
        if request.url.path.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {"primary": {"busy": []}}})
        return httpx.Response(503, json={"error": {"message": "down"}})

    calendar = calendar_client(handler)
    starts_at = await first_slot(ctx, calendar)
    llm = FakeLLM(
        tool_turn(
            (
                "book_appointment",
                {
                    "starts_at": starts_at,
                    "appointment_type": "site_visit",
                    "listing_id": None,
                    "notes": None,
                },
            )
        ),
        text_turn("Booked! See you Thursday at 11."),
    )
    engine = ConversationEngine(session, llm, google_settings(), calendar=calendar)

    result = await engine.handle_inbound(
        lead=ctx.lead, agent=ctx.agent, text="Thursday 11 works", now=NOW
    )

    assert result.escalated is True
    assert "Calendar" in (result.escalation_reason or "")
    assert ctx.lead.status is LeadStatus.HANDED_OFF
    # The booking itself survived.
    assert (await session.execute(select(Appointment))).scalar_one() is not None


async def test_booking_without_a_calendar_still_works(session: AsyncSession) -> None:
    ctx = await build(session, connected=False)
    starts_at = await first_slot(ctx)

    result = await dispatch(
        ctx,
        "book_appointment",
        {
            "starts_at": starts_at,
            "appointment_type": "site_visit",
            "listing_id": None,
            "notes": None,
        },
    )

    assert result["calendar_synced"] is False
    assert ctx.calendar_sync_failed is None  # nothing went wrong
    appointment = (await session.execute(select(Appointment))).scalar_one()
    assert appointment.google_event_id is None


async def test_slot_blocked_in_google_cannot_be_booked(session: AsyncSession) -> None:
    ctx = await build(session)
    baseline = await find_available_slots(
        session, ctx.agent, appointment_type=AppointmentType.CALL, search_days=1, now=NOW
    )
    blocked = baseline[0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token(request)
        if request.url.path.endswith("/freeBusy"):
            return httpx.Response(
                200,
                json={
                    "calendars": {
                        "primary": {
                            "busy": [
                                {
                                    "start": blocked.starts_at.isoformat(),
                                    "end": blocked.ends_at.isoformat(),
                                }
                            ]
                        }
                    }
                },
            )
        return httpx.Response(200, json={"id": "evt_should_not_happen"})

    ctx.calendar = calendar_client(handler)

    with pytest.raises(ToolError, match="no longer available"):
        await dispatch(
            ctx,
            "book_appointment",
            {
                "starts_at": blocked.starts_at.isoformat(),
                "appointment_type": "call",
                "listing_id": None,
                "notes": None,
            },
        )
