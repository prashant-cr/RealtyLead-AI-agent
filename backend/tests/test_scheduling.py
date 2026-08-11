from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment
from app.models.enums import AppointmentStatus, AppointmentType
from app.services.scheduling import SLOT_MINUTES, find_available_slots, resolve_timezone
from tests.factories import make_agent, make_lead

IST = ZoneInfo("Asia/Kolkata")
# Wednesday 12 Aug 2026, 06:00 IST — before the working day starts.
NOW = datetime(2026, 8, 12, 6, 0, tzinfo=IST).astimezone(UTC)


async def _agent(session: AsyncSession, **overrides: object):
    agent = make_agent(**overrides)
    session.add(agent)
    await session.flush()
    return agent


async def test_slots_fall_inside_working_hours(session: AsyncSession) -> None:
    agent = await _agent(session)

    slots = await find_available_slots(session, agent, now=NOW)

    assert slots
    for slot in slots:
        local = slot.starts_at.astimezone(IST)
        assert local.hour >= 9
        assert local.hour < 19


async def test_no_slots_on_a_closed_day(session: AsyncSession) -> None:
    # Sunday is closed in the default working hours; search only that day.
    agent = await _agent(session)
    sunday = datetime(2026, 8, 16, 6, 0, tzinfo=IST).astimezone(UTC)

    slots = await find_available_slots(session, agent, search_days=1, now=sunday)

    assert slots == []


async def test_minimum_lead_time_is_respected(session: AsyncSession) -> None:
    agent = await _agent(session)
    midday = datetime(2026, 8, 12, 12, 0, tzinfo=IST).astimezone(UTC)

    slots = await find_available_slots(session, agent, search_days=1, now=midday)

    assert slots
    assert all(slot.starts_at >= midday + timedelta(hours=2) for slot in slots)


async def test_booked_appointments_are_excluded(session: AsyncSession) -> None:
    agent = await _agent(session)
    lead = make_lead(agent)
    session.add(lead)
    await session.flush()

    baseline = await find_available_slots(session, agent, search_days=1, now=NOW)
    taken = baseline[0]
    session.add(
        Appointment(
            lead_id=lead.id,
            agent_id=agent.id,
            appointment_type=AppointmentType.CALL,
            status=AppointmentStatus.CONFIRMED,
            starts_at=taken.starts_at,
            ends_at=taken.ends_at,
        )
    )
    await session.flush()

    after = await find_available_slots(session, agent, search_days=1, now=NOW)

    assert taken.starts_at not in [slot.starts_at for slot in after]
    # The result set stays full — the next free slot backfills the booked one.
    assert len(after) == len(baseline)
    assert after[-1].starts_at > baseline[-1].starts_at


async def test_cancelled_appointments_free_the_slot(session: AsyncSession) -> None:
    agent = await _agent(session)
    lead = make_lead(agent)
    session.add(lead)
    await session.flush()
    baseline = await find_available_slots(session, agent, search_days=1, now=NOW)
    taken = baseline[0]
    session.add(
        Appointment(
            lead_id=lead.id,
            agent_id=agent.id,
            appointment_type=AppointmentType.CALL,
            status=AppointmentStatus.CANCELLED,
            starts_at=taken.starts_at,
            ends_at=taken.ends_at,
        )
    )
    await session.flush()

    after = await find_available_slots(session, agent, search_days=1, now=NOW)

    assert taken.starts_at in [slot.starts_at for slot in after]


async def test_site_visits_are_longer_than_calls(session: AsyncSession) -> None:
    agent = await _agent(session)

    calls = await find_available_slots(
        session, agent, appointment_type=AppointmentType.CALL, now=NOW
    )
    visits = await find_available_slots(
        session, agent, appointment_type=AppointmentType.SITE_VISIT, now=NOW
    )

    assert (calls[0].ends_at - calls[0].starts_at).seconds // 60 == SLOT_MINUTES[
        AppointmentType.CALL
    ]
    assert (visits[0].ends_at - visits[0].starts_at).seconds // 60 == SLOT_MINUTES[
        AppointmentType.SITE_VISIT
    ]


async def test_slot_labels_render_in_the_agents_timezone(session: AsyncSession) -> None:
    agent = await _agent(session)

    slots = await find_available_slots(session, agent, search_days=1, now=NOW)
    label = slots[0].label(resolve_timezone(agent.timezone))

    assert "Wed 12 Aug" in label
    assert "AM" in label or "PM" in label


def test_unknown_timezone_falls_back_to_default() -> None:
    assert resolve_timezone("Not/AZone") == IST
    assert resolve_timezone(None) == IST
