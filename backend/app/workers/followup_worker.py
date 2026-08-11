"""The follow-up worker: send due nudges, then queue the next one.

Run it alongside the API:

    make worker                       # or: python -m app.workers.followup_worker
    python -m app.workers.followup_worker --once   # single pass, for cron

The queue is the `follow_up_tasks` table rather than Redis. The schedule has to
survive restarts and be visible to the dashboard, and Postgres gives us both plus
`SELECT ... FOR UPDATE SKIP LOCKED` for safe concurrency — a second store would
add a way for the two to disagree. Redis stays in the stack for rate limiting and
the webhook queue.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import ChannelAdapter, OutboundMessage
from app.channels.templates import first_name, follow_up_template, render
from app.channels.whatsapp import WhatsAppChannel, WhatsAppError
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.core.logging import configure_logging, get_logger, mask_phone
from app.models import Agent, FollowUpTask, Lead
from app.models.conversation import Conversation, Message
from app.models.enums import (
    FollowUpStatus,
    MessageDirection,
    MessageRole,
    MessageStatus,
)
from app.services.followups import (
    SkipReason,
    check_eligibility,
    due_tasks,
    schedule_next,
)
from app.services.quiet_hours import is_quiet_hour, next_send_time
from app.services.ratelimit import check, follow_up_limit

log = get_logger(__name__)

DEFAULT_POLL_SECONDS = 60
DEFAULT_BATCH_SIZE = 50


@dataclass
class RunReport:
    sent: int = 0
    skipped: int = 0
    deferred: int = 0
    failed: int = 0
    skip_reasons: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.sent + self.skipped + self.deferred + self.failed

    def __str__(self) -> str:
        return (
            f"sent={self.sent} skipped={self.skipped} deferred={self.deferred} failed={self.failed}"
        )


async def _adapter_for(agent: Agent, settings: Settings) -> ChannelAdapter | None:
    if not agent.whatsapp_phone_number_id:
        log.warning("agent %s has no WhatsApp number; cannot send follow-ups", agent.id)
        return None
    try:
        return WhatsAppChannel(agent.whatsapp_phone_number_id, settings)
    except WhatsAppError as exc:
        log.error("cannot build WhatsApp adapter for agent %s: %s", agent.id, exc)
        return None


async def process_task(
    session: AsyncSession,
    task: FollowUpTask,
    adapter: ChannelAdapter,
    settings: Settings,
    now: datetime,
    report: RunReport,
) -> None:
    """Send one nudge, or record why it was not sent."""
    lead = await session.get_one(Lead, task.lead_id)
    agent = await session.get_one(Agent, lead.agent_id)

    # Re-check now, not just when it was scheduled — days may have passed.
    eligibility = await check_eligibility(session, lead, task, settings)
    if not eligibility.ok:
        reason = eligibility.reason or SkipReason.TERMINAL_STATUS
        task.status = (
            FollowUpStatus.CANCELLED
            if reason is not SkipReason.CAP_REACHED
            else FollowUpStatus.SKIPPED
        )
        task.outcome_reason = str(reason)
        report.skipped += 1
        report.skip_reasons.append(str(reason))
        log.info("skipping follow-up %s: %s", task.id, reason)
        # A lead who replied gets a fresh nudge scheduled from their new baseline.
        if reason is SkipReason.LEAD_REPLIED:
            await schedule_next(session, lead, settings=settings, now=now)
        return

    # Quiet hours are the lead's, not ours. Defer rather than cancel.
    if is_quiet_hour(now, lead.timezone, settings.quiet_hours_start, settings.quiet_hours_end):
        task.scheduled_for = next_send_time(
            now, lead.timezone, settings.quiet_hours_start, settings.quiet_hours_end
        )
        report.deferred += 1
        log.info("deferring follow-up %s to %s (quiet hours)", task.id, task.scheduled_for)
        return

    # Bounds a bulk import turning into a flood of template messages. Deferred
    # rather than cancelled: the lead still deserves the nudge, just not in this
    # pass. Checked here — after eligibility and quiet hours — so budget is only
    # spent on nudges that were actually about to be sent.
    allowance = await check(follow_up_limit(settings), str(agent.id))
    if not allowance.allowed:
        task.scheduled_for = now + timedelta(seconds=allowance.retry_after_seconds)
        report.deferred += 1
        log.warning(
            "agent %s is over the follow-up limit (%s per %ss); deferring %s by %ss",
            agent.id,
            allowance.limit.max_events,
            allowance.limit.window_seconds,
            task.id,
            allowance.retry_after_seconds,
        )
        return

    template = follow_up_template(task.attempt_number, lead.language)
    if template is None:
        task.status = FollowUpStatus.SKIPPED
        task.outcome_reason = str(SkipReason.NO_TEMPLATE)
        report.skipped += 1
        report.skip_reasons.append(str(SkipReason.NO_TEMPLATE))
        return

    greeting = first_name(lead.name, lead.language)
    delivery = await adapter.send(
        OutboundMessage(
            channel=task.channel,
            recipient=lead.phone,
            text=render(template, greeting, agent.name),
            lead_id=lead.id,
            # Business-initiated and outside the 24h window: template is mandatory.
            template_name=template.name,
            template_variables={
                "language": template.language.value,
                "name": greeting,
                "agent": agent.name,
            },
        )
    )

    if not delivery.accepted:
        task.status = FollowUpStatus.FAILED
        task.outcome_reason = (delivery.error or "delivery rejected")[:255]
        report.failed += 1
        log.error("follow-up %s to %s failed: %s", task.id, mask_phone(lead.phone), delivery.error)
        return

    task.status = FollowUpStatus.SENT
    task.sent_at = now
    task.template_name = template.name
    lead.follow_up_count += 1
    lead.last_outbound_at = now
    await _record_outbound(session, lead, task, render(template, greeting, agent.name), delivery)
    report.sent += 1
    log.info(
        "sent follow-up %s (attempt %s) to %s",
        task.id,
        task.attempt_number,
        mask_phone(lead.phone),
    )

    await schedule_next(session, lead, settings=settings, now=now, channel=task.channel)


async def _record_outbound(
    session: AsyncSession, lead: Lead, task: FollowUpTask, text: str, delivery: object
) -> None:
    """Follow-ups belong in the transcript the human agent reads."""
    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.lead_id == lead.id, Conversation.channel == task.channel)
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if conversation is None:
        conversation = Conversation(lead_id=lead.id, channel=task.channel)
        session.add(conversation)
        await session.flush()

    session.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            direction=MessageDirection.OUTBOUND,
            channel=task.channel,
            status=MessageStatus.SENT,
            content=text,
            external_id=getattr(delivery, "external_id", None),
            meta={"follow_up_attempt": task.attempt_number, "template": task.template_name},
            sent_at=task.sent_at,
        )
    )
    conversation.last_message_at = task.sent_at
    await session.flush()


async def run_once(
    settings: Settings | None = None,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    session: AsyncSession | None = None,
    adapter: ChannelAdapter | None = None,
) -> RunReport:
    """One pass over the due queue.

    `session` and `adapter` are injectable so tests can drive this without a
    database engine or a WhatsApp account.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    report = RunReport()

    if session is not None:
        await _process_batch(session, settings, now, batch_size, report, adapter)
        return report

    async with get_sessionmaker()() as owned_session:
        try:
            await _process_batch(owned_session, settings, now, batch_size, report, adapter)
            await owned_session.commit()
        except Exception:
            await owned_session.rollback()
            log.exception("follow-up batch failed; no tasks marked sent")
            raise
    return report


async def _process_batch(
    session: AsyncSession,
    settings: Settings,
    now: datetime,
    batch_size: int,
    report: RunReport,
    adapter: ChannelAdapter | None,
) -> None:
    tasks = await due_tasks(session, now, limit=batch_size)
    if not tasks:
        return

    log.info("processing %s due follow-up(s)", len(tasks))
    adapters: dict[str, ChannelAdapter] = {}
    owned: list[ChannelAdapter] = []

    try:
        for task in tasks:
            lead = await session.get_one(Lead, task.lead_id)
            agent = await session.get_one(Agent, lead.agent_id)

            if adapter is not None:
                task_adapter: ChannelAdapter | None = adapter
            elif str(agent.id) in adapters:
                task_adapter = adapters[str(agent.id)]
            else:
                task_adapter = await _adapter_for(agent, settings)
                if task_adapter is not None:
                    adapters[str(agent.id)] = task_adapter
                    owned.append(task_adapter)

            if task_adapter is None:
                task.status = FollowUpStatus.FAILED
                task.outcome_reason = "no messaging channel configured for this agent"
                report.failed += 1
                continue

            await process_task(session, task, task_adapter, settings, now, report)
        await session.flush()
    finally:
        for created in owned:
            await created.close()


async def run_forever(
    poll_seconds: int = DEFAULT_POLL_SECONDS, settings: Settings | None = None
) -> None:
    settings = settings or get_settings()
    stopping = asyncio.Event()

    def _stop() -> None:
        log.info("stop requested; finishing the current pass")
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with_suppress(loop, sig, _stop)

    log.info("follow-up worker started (polling every %ss)", poll_seconds)
    while not stopping.is_set():
        try:
            report = await run_once(settings)
            if report.processed:
                log.info("follow-up pass complete: %s", report)
        except Exception:
            # One bad pass must not kill the worker; the next poll retries.
            log.exception("follow-up pass failed")

        try:
            await asyncio.wait_for(stopping.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue

    log.info("follow-up worker stopped")


def with_suppress(loop: asyncio.AbstractEventLoop, sig: signal.Signals, handler: object) -> None:
    """Signal handlers are unavailable on some platforms/threads; degrade quietly."""
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(sig, handler)  # type: ignore[arg-type]


def main() -> None:
    parser = argparse.ArgumentParser(description="Send scheduled follow-up nudges.")
    parser.add_argument("--once", action="store_true", help="Single pass, then exit (cron).")
    parser.add_argument(
        "--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS, help="Seconds between passes."
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)

    async def _run() -> None:
        try:
            if args.once:
                report = await run_once(settings, batch_size=args.batch_size)
                log.info("follow-up pass complete: %s", report)
            else:
                await run_forever(args.poll_seconds, settings)
        finally:
            await dispose_engine()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
