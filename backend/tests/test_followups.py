"""Follow-up scheduling, eligibility and cadence."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Conversation, FollowUpTask
from app.models.enums import (
    Channel,
    ConsentStatus,
    ConversationStatus,
    FollowUpStatus,
    LeadStatus,
)
from app.models.followup import FOLLOW_UP_CADENCE_DAYS
from app.services.followups import (
    SkipReason,
    cancel_pending,
    check_eligibility,
    due_tasks,
    max_attempts,
    next_attempt_time,
    schedule_next,
)
from tests.factories import make_agent, make_lead

IST = ZoneInfo("Asia/Kolkata")
# Midday IST so scheduled nudges land in waking hours by default.
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=IST).astimezone(UTC)


def settings(**overrides: object) -> Settings:
    return Settings(**{"max_follow_ups": 6, **overrides})  # type: ignore[arg-type]


async def make_lead_row(session: AsyncSession, **overrides: object):
    agent = make_agent(whatsapp_phone_number_id="PNID1")
    session.add(agent)
    await session.flush()
    defaults: dict[str, object] = {
        "last_inbound_at": NOW,
        "consent_status": ConsentStatus.OPTED_IN,
        "status": LeadStatus.ENGAGED,
        "timezone": "Asia/Kolkata",
    }
    lead = make_lead(agent, **{**defaults, **overrides})
    session.add(lead)
    await session.flush()
    return agent, lead


# ------------------------------------------------------------------- cadence


def test_cadence_matches_the_spec() -> None:
    """CLAUDE.md: day 1, 3, 7, 14 — then monthly."""
    assert FOLLOW_UP_CADENCE_DAYS[:4] == (1, 3, 7, 14)
    monthly_gaps = [
        FOLLOW_UP_CADENCE_DAYS[i] - FOLLOW_UP_CADENCE_DAYS[i - 1]
        for i in range(4, len(FOLLOW_UP_CADENCE_DAYS))
    ]
    assert all(gap == 30 for gap in monthly_gaps)


def test_attempt_times_step_through_the_cadence() -> None:
    assert next_attempt_time(NOW, 1) == NOW + timedelta(days=1)
    assert next_attempt_time(NOW, 2) == NOW + timedelta(days=3)
    assert next_attempt_time(NOW, 4) == NOW + timedelta(days=14)


def test_attempts_beyond_the_cadence_have_no_time() -> None:
    assert next_attempt_time(NOW, len(FOLLOW_UP_CADENCE_DAYS) + 1) is None
    assert next_attempt_time(NOW, 0) is None


def test_cap_is_the_smaller_of_config_and_cadence() -> None:
    assert max_attempts(settings(max_follow_ups=99)) == len(FOLLOW_UP_CADENCE_DAYS)
    assert max_attempts(settings(max_follow_ups=2)) == 2


# ---------------------------------------------------------------- scheduling


async def test_first_nudge_is_scheduled_a_day_after_the_lead_wrote(
    session: AsyncSession,
) -> None:
    _, lead = await make_lead_row(session)

    task = await schedule_next(session, lead, settings=settings(), now=NOW)

    assert task is not None
    assert task.attempt_number == 1
    assert task.scheduled_for == NOW + timedelta(days=1)
    assert task.status is FollowUpStatus.SCHEDULED


async def test_scheduling_is_measured_from_the_last_inbound_not_now(
    session: AsyncSession,
) -> None:
    """A lead who went quiet three days ago is already overdue, not due tomorrow."""
    _, lead = await make_lead_row(session, last_inbound_at=NOW - timedelta(days=3))

    task = await schedule_next(session, lead, settings=settings(), now=NOW)

    assert task is not None
    assert task.scheduled_for == NOW - timedelta(days=2)


async def test_scheduling_replaces_any_pending_nudge(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    first = await schedule_next(session, lead, settings=settings(), now=NOW)

    second = await schedule_next(session, lead, settings=settings(), now=NOW)

    assert first is not None and second is not None
    assert first.id != second.id
    await session.refresh(first)
    assert first.status is FollowUpStatus.CANCELLED
    scheduled = (
        (
            await session.execute(
                select(FollowUpTask).where(FollowUpTask.status == FollowUpStatus.SCHEDULED)
            )
        )
        .scalars()
        .all()
    )
    assert len(scheduled) == 1


async def test_nudge_landing_in_quiet_hours_is_pushed_to_the_morning(
    session: AsyncSession,
) -> None:
    # Lead wrote at 11pm IST, so day+1 would also be 11pm.
    late = datetime(2026, 8, 12, 23, 0, tzinfo=IST).astimezone(UTC)
    _, lead = await make_lead_row(session, last_inbound_at=late)

    task = await schedule_next(session, lead, settings=settings(), now=late)

    assert task is not None
    local = task.scheduled_for.astimezone(IST)
    assert local.hour == 9
    assert local.day == 14  # the morning after the 11pm+1day slot


async def test_second_nudge_follows_the_first(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    lead.follow_up_count = 1

    task = await schedule_next(session, lead, settings=settings(), now=NOW)

    assert task is not None
    assert task.attempt_number == 2
    assert task.scheduled_for == NOW + timedelta(days=3)


# --------------------------------------------------------------- eligibility


async def test_opted_out_lead_is_never_scheduled(session: AsyncSession) -> None:
    _, lead = await make_lead_row(
        session, consent_status=ConsentStatus.OPTED_OUT, status=LeadStatus.OPTED_OUT
    )

    assert await schedule_next(session, lead, settings=settings(), now=NOW) is None


async def test_booked_lead_is_not_nudged(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session, status=LeadStatus.BOOKED)

    assert await schedule_next(session, lead, settings=settings(), now=NOW) is None


async def test_handed_off_lead_is_not_nudged(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session, status=LeadStatus.HANDED_OFF)

    assert await schedule_next(session, lead, settings=settings(), now=NOW) is None


async def test_cap_stops_further_nudges(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    lead.follow_up_count = 6

    assert await schedule_next(session, lead, settings=settings(), now=NOW) is None
    result = await check_eligibility(session, lead, None, settings())
    assert result.reason is SkipReason.CAP_REACHED


async def test_lower_configured_cap_is_respected(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    lead.follow_up_count = 2

    assert await schedule_next(session, lead, settings=settings(max_follow_ups=2), now=NOW) is None


async def test_human_takeover_blocks_nudges(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    session.add(
        Conversation(
            lead_id=lead.id,
            channel=Channel.WHATSAPP,
            status=ConversationStatus.HUMAN_TAKEOVER,
        )
    )
    await session.flush()

    result = await check_eligibility(session, lead, None, settings())

    assert result.ok is False
    assert result.reason is SkipReason.HUMAN_TAKEOVER


async def test_reply_after_scheduling_makes_the_task_stale(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    task = await schedule_next(session, lead, settings=settings(), now=NOW)
    assert task is not None
    assert task.baseline_at == NOW  # scheduled from the lead's last message
    lead.last_inbound_at = NOW + timedelta(minutes=5)  # they wrote again

    result = await check_eligibility(session, lead, task, settings())

    assert result.ok is False
    assert result.reason is SkipReason.LEAD_REPLIED


# ---------------------------------------------------------------- cancelling


async def test_cancel_pending_marks_scheduled_tasks(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    await schedule_next(session, lead, settings=settings(), now=NOW)

    cancelled = await cancel_pending(session, lead.id, "lead opted out")

    assert cancelled == 1
    task = (await session.execute(select(FollowUpTask))).scalar_one()
    assert task.status is FollowUpStatus.CANCELLED
    assert task.outcome_reason == "lead opted out"


async def test_cancel_pending_leaves_sent_tasks_alone(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    task = await schedule_next(session, lead, settings=settings(), now=NOW)
    assert task is not None
    task.status = FollowUpStatus.SENT
    await session.flush()

    assert await cancel_pending(session, lead.id, "whatever") == 0
    await session.refresh(task)
    assert task.status is FollowUpStatus.SENT


# ------------------------------------------------------------------ due queue


async def test_only_due_tasks_are_claimed(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    await schedule_next(session, lead, settings=settings(), now=NOW)

    assert await due_tasks(session, NOW) == []
    due = await due_tasks(session, NOW + timedelta(days=1, minutes=1))
    assert len(due) == 1


async def test_cancelled_tasks_are_never_claimed(session: AsyncSession) -> None:
    _, lead = await make_lead_row(session)
    await schedule_next(session, lead, settings=settings(), now=NOW)
    await cancel_pending(session, lead.id, "opted out")

    assert await due_tasks(session, NOW + timedelta(days=30)) == []


async def test_due_tasks_come_back_oldest_first(session: AsyncSession) -> None:
    agent = make_agent(whatsapp_phone_number_id="PNID1")
    session.add(agent)
    await session.flush()
    for index, days_ago in enumerate((5, 1, 3)):
        lead = make_lead(
            agent,
            phone=f"+9198000000{index}",
            last_inbound_at=NOW - timedelta(days=days_ago),
            consent_status=ConsentStatus.OPTED_IN,
        )
        session.add(lead)
        await session.flush()
        await schedule_next(session, lead, settings=settings(), now=NOW)

    due = await due_tasks(session, NOW + timedelta(days=1))

    assert [t.scheduled_for for t in due] == sorted(t.scheduled_for for t in due)
