"""The worker that actually sends the nudges."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import DeliveryResult, OutboundMessage
from app.channels.memory import InMemoryChannel
from app.channels.templates import first_name, follow_up_template, render
from app.core.config import Settings
from app.models import Conversation, FollowUpTask, Message
from app.models.enums import (
    Channel,
    ConsentStatus,
    ConversationStatus,
    FollowUpStatus,
    Language,
    LeadStatus,
    MessageDirection,
)
from app.services.followups import schedule_next
from app.workers.followup_worker import run_once
from tests.factories import make_agent, make_lead

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=IST).astimezone(UTC)
DUE = NOW + timedelta(days=1, minutes=1)


def settings(**overrides: object) -> Settings:
    return Settings(
        **{"max_follow_ups": 6, "whatsapp_access_token": "tok", **overrides}  # type: ignore[arg-type]
    )


class RejectingChannel(InMemoryChannel):
    async def send(self, message: OutboundMessage) -> DeliveryResult:
        return DeliveryResult(external_id=None, accepted=False, error="template not approved")


async def make_due_task(session: AsyncSession, **lead_overrides: object):
    agent = make_agent(whatsapp_phone_number_id="PNID1")
    session.add(agent)
    await session.flush()
    defaults: dict[str, object] = {
        "name": "Priya Shah",
        "last_inbound_at": NOW,
        "consent_status": ConsentStatus.OPTED_IN,
        "status": LeadStatus.ENGAGED,
        "timezone": "Asia/Kolkata",
    }
    lead = make_lead(agent, **{**defaults, **lead_overrides})
    session.add(lead)
    await session.flush()
    task = await schedule_next(session, lead, settings=settings(), now=NOW)
    return agent, lead, task


# ------------------------------------------------------------------- sending


async def test_due_nudge_is_sent_as_an_approved_template(session: AsyncSession) -> None:
    agent, lead, task = await make_due_task(session)
    adapter = InMemoryChannel(Channel.WHATSAPP)

    report = await run_once(settings(), now=DUE, session=session, adapter=adapter)

    assert report.sent == 1
    sent = adapter.outbox[0]
    assert sent.recipient == lead.phone
    # Outside the 24h window Meta only delivers pre-approved templates.
    assert sent.template_name == "followup_day_1"
    assert sent.template_variables["name"] == "Priya"
    assert sent.template_variables["agent"] == agent.name


async def test_sending_advances_the_task_and_the_lead(session: AsyncSession) -> None:
    _, lead, task = await make_due_task(session)
    assert task is not None

    await run_once(settings(), now=DUE, session=session, adapter=InMemoryChannel(Channel.WHATSAPP))

    await session.refresh(task)
    assert task.status is FollowUpStatus.SENT
    assert task.sent_at == DUE
    assert task.template_name == "followup_day_1"
    assert lead.follow_up_count == 1
    assert lead.last_outbound_at == DUE


async def test_the_next_nudge_is_queued_after_a_send(session: AsyncSession) -> None:
    _, lead, _ = await make_due_task(session)

    await run_once(settings(), now=DUE, session=session, adapter=InMemoryChannel(Channel.WHATSAPP))

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
    assert scheduled[0].attempt_number == 2


async def test_the_nudge_appears_in_the_transcript(session: AsyncSession) -> None:
    """The human agent should see what we sent on their behalf."""
    _, lead, _ = await make_due_task(session)

    await run_once(settings(), now=DUE, session=session, adapter=InMemoryChannel(Channel.WHATSAPP))

    message = (
        await session.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND))
    ).scalar_one()
    assert "checking in" in message.content
    assert message.meta["follow_up_attempt"] == 1
    assert message.external_id == "mem-1"


async def test_lead_without_a_name_gets_a_neutral_greeting(session: AsyncSession) -> None:
    _, lead, _ = await make_due_task(session, name=None)
    adapter = InMemoryChannel(Channel.WHATSAPP)

    await run_once(settings(), now=DUE, session=session, adapter=adapter)

    assert adapter.outbox[0].template_variables["name"] == "there"


async def test_hindi_lead_gets_the_hindi_template(session: AsyncSession) -> None:
    _, lead, _ = await make_due_task(session, language=Language.HINDI, name=None)
    adapter = InMemoryChannel(Channel.WHATSAPP)

    await run_once(settings(), now=DUE, session=session, adapter=adapter)

    assert adapter.outbox[0].template_name == "followup_day_1_hi"
    assert adapter.outbox[0].template_variables["language"] == "hi"


# ------------------------------------------------------------------ skipping


async def test_opted_out_lead_is_never_messaged(session: AsyncSession) -> None:
    _, lead, task = await make_due_task(session)
    lead.consent_status = ConsentStatus.OPTED_OUT
    lead.status = LeadStatus.OPTED_OUT
    await session.flush()
    adapter = InMemoryChannel(Channel.WHATSAPP)

    report = await run_once(settings(), now=DUE, session=session, adapter=adapter)

    assert adapter.outbox == []
    assert report.sent == 0
    assert report.skipped == 1
    assert task is not None
    await session.refresh(task)
    assert task.status is FollowUpStatus.CANCELLED


async def test_lead_who_booked_between_scheduling_and_sending_is_skipped(
    session: AsyncSession,
) -> None:
    _, lead, _ = await make_due_task(session)
    lead.status = LeadStatus.BOOKED
    await session.flush()
    adapter = InMemoryChannel(Channel.WHATSAPP)

    report = await run_once(settings(), now=DUE, session=session, adapter=adapter)

    assert adapter.outbox == []
    assert report.skipped == 1


async def test_human_takeover_between_scheduling_and_sending_is_skipped(
    session: AsyncSession,
) -> None:
    _, lead, _ = await make_due_task(session)
    session.add(
        Conversation(
            lead_id=lead.id,
            channel=Channel.WHATSAPP,
            status=ConversationStatus.HUMAN_TAKEOVER,
        )
    )
    await session.flush()
    adapter = InMemoryChannel(Channel.WHATSAPP)

    report = await run_once(settings(), now=DUE, session=session, adapter=adapter)

    assert adapter.outbox == []
    assert report.skipped == 1


async def test_lead_who_replied_is_skipped_and_rescheduled(session: AsyncSession) -> None:
    _, lead, task = await make_due_task(session)
    assert task is not None
    lead.last_inbound_at = NOW + timedelta(hours=1)  # replied after we scheduled
    await session.flush()
    adapter = InMemoryChannel(Channel.WHATSAPP)

    report = await run_once(settings(), now=DUE, session=session, adapter=adapter)

    assert adapter.outbox == []
    assert report.skipped == 1
    # A fresh nudge is queued from the new baseline rather than being dropped.
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


async def test_cap_reached_stops_the_cadence(session: AsyncSession) -> None:
    _, lead, task = await make_due_task(session)
    lead.follow_up_count = 6
    await session.flush()
    adapter = InMemoryChannel(Channel.WHATSAPP)

    report = await run_once(settings(), now=DUE, session=session, adapter=adapter)

    assert adapter.outbox == []
    assert report.skipped == 1
    assert task is not None
    await session.refresh(task)
    assert task.status is FollowUpStatus.SKIPPED


# ------------------------------------------------------------- quiet hours


async def test_nudge_due_in_quiet_hours_is_deferred_not_sent(session: AsyncSession) -> None:
    _, lead, task = await make_due_task(session)
    assert task is not None
    # Force it due at 2am the lead's time.
    small_hours = datetime(2026, 8, 14, 2, 0, tzinfo=IST).astimezone(UTC)
    task.scheduled_for = small_hours - timedelta(minutes=5)
    await session.flush()
    adapter = InMemoryChannel(Channel.WHATSAPP)

    report = await run_once(settings(), now=small_hours, session=session, adapter=adapter)

    assert adapter.outbox == []
    assert report.deferred == 1
    await session.refresh(task)
    assert task.status is FollowUpStatus.SCHEDULED
    assert task.scheduled_for.astimezone(IST).hour == 9


async def test_deferred_nudge_sends_once_the_window_opens(session: AsyncSession) -> None:
    _, lead, task = await make_due_task(session)
    assert task is not None
    small_hours = datetime(2026, 8, 14, 2, 0, tzinfo=IST).astimezone(UTC)
    task.scheduled_for = small_hours - timedelta(minutes=5)
    await session.flush()
    adapter = InMemoryChannel(Channel.WHATSAPP)
    await run_once(settings(), now=small_hours, session=session, adapter=adapter)

    morning = datetime(2026, 8, 14, 9, 30, tzinfo=IST).astimezone(UTC)
    report = await run_once(settings(), now=morning, session=session, adapter=adapter)

    assert report.sent == 1
    assert len(adapter.outbox) == 1


# ------------------------------------------------------------------ failures


async def test_rejected_delivery_marks_the_task_failed(session: AsyncSession) -> None:
    _, lead, task = await make_due_task(session)

    report = await run_once(
        settings(), now=DUE, session=session, adapter=RejectingChannel(Channel.WHATSAPP)
    )

    assert report.failed == 1
    assert task is not None
    await session.refresh(task)
    assert task.status is FollowUpStatus.FAILED
    assert "not approved" in (task.outcome_reason or "")
    # A failed send must not consume one of the lead's allowed nudges.
    assert lead.follow_up_count == 0


async def test_empty_queue_is_a_no_op(session: AsyncSession) -> None:
    report = await run_once(
        settings(), now=DUE, session=session, adapter=InMemoryChannel(Channel.WHATSAPP)
    )

    assert report.processed == 0


async def test_a_second_pass_does_not_resend(session: AsyncSession) -> None:
    await make_due_task(session)
    adapter = InMemoryChannel(Channel.WHATSAPP)
    await run_once(settings(), now=DUE, session=session, adapter=adapter)

    report = await run_once(settings(), now=DUE, session=session, adapter=adapter)

    assert report.sent == 0
    assert len(adapter.outbox) == 1


# ------------------------------------------------------------------ templates


def test_every_cadence_step_has_a_template_in_every_language() -> None:
    for attempt in range(1, 7):
        for language in Language:
            template = follow_up_template(attempt, language)
            assert template is not None, (attempt, language)
            assert "{{1}}" in template.body
            assert "{{2}}" in template.body


def test_templates_offer_an_opt_out() -> None:
    """TRAI and WhatsApp policy both expect an obvious way to stop."""
    for attempt in range(1, 6):  # the final message says it is the last one instead
        body = follow_up_template(attempt, Language.ENGLISH)
        assert body is not None
        assert "STOP" in body.body


def test_rendering_fills_both_variables() -> None:
    template = follow_up_template(1, Language.ENGLISH)
    assert template is not None

    text = render(template, first_name("Priya Shah", Language.ENGLISH), "Rohan Mehta")

    assert "Priya" in text
    assert "Rohan Mehta" in text
    assert "{{" not in text
