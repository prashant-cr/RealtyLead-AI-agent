"""Scheduling and eligibility for follow-up nudges.

The cadence is day 1, 3, 7, 14, then roughly monthly, measured from the lead's
*last message* — so a lead who replies resets the clock rather than accumulating
nudges.

Eligibility is deliberately conservative and checked twice: once when a task is
scheduled and again immediately before it is sent. A task can sit in the queue
for days, and almost anything that makes a nudge inappropriate (they replied,
they booked, they opted out, a human took over) happens in that gap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models import Conversation, FollowUpTask, Lead
from app.models.enums import (
    Channel,
    ConsentStatus,
    ConversationStatus,
    FollowUpStatus,
    LeadStatus,
)
from app.models.followup import FOLLOW_UP_CADENCE_DAYS
from app.services.quiet_hours import is_quiet_hour, next_send_time

log = get_logger(__name__)

# Statuses where a nudge would be wrong: the lead is done with us, or a human owns them.
TERMINAL_LEAD_STATUSES = frozenset({LeadStatus.OPTED_OUT, LeadStatus.BOOKED, LeadStatus.HANDED_OFF})


class SkipReason(StrEnum):
    OPTED_OUT = "lead opted out"
    TERMINAL_STATUS = "lead is booked, handed off or closed"
    HUMAN_TAKEOVER = "a human has taken over the conversation"
    LEAD_REPLIED = "lead replied after this nudge was scheduled"
    CAP_REACHED = "follow-up cap reached"
    NO_TEMPLATE = "no approved template for this attempt"
    NOT_CONTACTABLE = "no consent to message this lead"


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reason: SkipReason | None = None


def max_attempts(settings: Settings) -> int:
    """Hard cap — never more nudges than we have cadence steps or config allows."""
    return min(settings.max_follow_ups, len(FOLLOW_UP_CADENCE_DAYS))


def next_attempt_time(baseline: datetime, attempt: int) -> datetime | None:
    """When attempt `attempt` (1-based) is due, measured from the lead's last message."""
    if attempt < 1 or attempt > len(FOLLOW_UP_CADENCE_DAYS):
        return None
    return baseline + timedelta(days=FOLLOW_UP_CADENCE_DAYS[attempt - 1])


async def check_eligibility(
    session: AsyncSession,
    lead: Lead,
    task: FollowUpTask | None,
    settings: Settings,
) -> Eligibility:
    """Whether it is appropriate to nudge this lead right now."""
    if lead.consent_status is ConsentStatus.OPTED_OUT:
        return Eligibility(False, SkipReason.OPTED_OUT)
    if not lead.is_contactable:
        return Eligibility(False, SkipReason.NOT_CONTACTABLE)
    if lead.status in TERMINAL_LEAD_STATUSES:
        return Eligibility(False, SkipReason.TERMINAL_STATUS)
    if lead.follow_up_count >= max_attempts(settings):
        return Eligibility(False, SkipReason.CAP_REACHED)

    # A reply after the baseline means the conversation moved on. Normally the
    # engine has already cancelled and replaced the task; this catches the race
    # where the worker claimed it first.
    baseline = task.baseline_at if task is not None else None
    if (
        baseline is not None
        and lead.last_inbound_at is not None
        and lead.last_inbound_at > baseline
    ):
        return Eligibility(False, SkipReason.LEAD_REPLIED)

    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.lead_id == lead.id)
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if conversation is not None and conversation.status is ConversationStatus.HUMAN_TAKEOVER:
        return Eligibility(False, SkipReason.HUMAN_TAKEOVER)

    return Eligibility(True)


async def cancel_pending(session: AsyncSession, lead_id: uuid.UUID, reason: str) -> int:
    """Drop any scheduled nudges for a lead. Called whenever the situation changes."""
    result = await session.execute(
        select(FollowUpTask).where(
            FollowUpTask.lead_id == lead_id,
            FollowUpTask.status == FollowUpStatus.SCHEDULED,
        )
    )
    tasks = list(result.scalars().all())
    for task in tasks:
        task.status = FollowUpStatus.CANCELLED
        task.outcome_reason = reason[:255]
    if tasks:
        await session.flush()
    return len(tasks)


async def schedule_next(
    session: AsyncSession,
    lead: Lead,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    channel: Channel = Channel.WHATSAPP,
) -> FollowUpTask | None:
    """Queue the lead's next nudge, replacing any already scheduled.

    Returns None when no further nudge is appropriate — a cap reached, an opted-out
    lead or a booked one. Callers treat None as "nothing more to do", not an error.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    await cancel_pending(session, lead.id, "superseded")

    eligibility = await check_eligibility(session, lead, None, settings)
    if not eligibility.ok:
        return None

    attempt = lead.follow_up_count + 1
    baseline = lead.last_inbound_at or now
    due = next_attempt_time(baseline, attempt)
    if due is None:
        return None

    # A nudge that would land at 3am gets pushed to the morning.
    if is_quiet_hour(due, lead.timezone, settings.quiet_hours_start, settings.quiet_hours_end):
        due = next_send_time(
            due, lead.timezone, settings.quiet_hours_start, settings.quiet_hours_end
        )

    task = FollowUpTask(
        lead_id=lead.id,
        attempt_number=attempt,
        scheduled_for=due,
        baseline_at=baseline,
        channel=channel,
        status=FollowUpStatus.SCHEDULED,
    )
    session.add(task)
    await session.flush()
    log.info(
        "scheduled follow-up %s (attempt %s) for lead %s at %s",
        task.id,
        attempt,
        lead.id,
        due.isoformat(),
    )
    return task


async def due_tasks(session: AsyncSession, now: datetime, limit: int = 50) -> list[FollowUpTask]:
    """Claim scheduled tasks that are due.

    On Postgres the rows are locked with SKIP LOCKED so several worker replicas
    can run concurrently without sending the same nudge twice. SQLite (tests) has
    no such clause and runs single-threaded anyway.
    """
    query = (
        select(FollowUpTask)
        .where(
            FollowUpTask.status == FollowUpStatus.SCHEDULED,
            FollowUpTask.scheduled_for <= now,
        )
        .order_by(FollowUpTask.scheduled_for)
        .limit(limit)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)

    return list((await session.execute(query)).scalars().all())
