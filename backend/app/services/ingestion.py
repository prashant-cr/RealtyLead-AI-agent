"""Turning an inbound channel message into a conversation turn.

Split into two phases on purpose:

* `claim_inbound` is fast and synchronous — resolve the agent and lead, create
  the conversation, and insert the inbound Message row. The unique constraint on
  `(channel, external_id)` makes this the deduplication point, so a Meta retry is
  rejected here rather than producing a second reply.
* `process_claimed` is slow — it runs the model turn and sends the reply. The
  webhook schedules it after acknowledging, because Meta retries any delivery we
  do not acknowledge within a few seconds and a model turn takes longer than that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.engine import ConversationEngine, EngineResult
from app.agent.llm import LLMClient
from app.channels.base import ChannelAdapter, InboundMessage, OutboundMessage
from app.channels.whatsapp import within_service_window
from app.channels.whatsapp_payload import StatusUpdate
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, mask_phone
from app.models import Agent, Conversation, Lead, Message
from app.models.enums import (
    Channel,
    ConsentStatus,
    Language,
    MessageDirection,
    MessageRole,
    MessageStatus,
)
from app.services.google_calendar import GoogleCalendarClient

log = get_logger(__name__)


class DeliveryRejectedError(RuntimeError):
    """The channel refused to send a reply we had already composed.

    Transient by assumption — an expired token, a provider outage, a rate limit.
    The inbound worker turns this into a retry rather than acknowledging the
    message, so the lead is answered once the cause clears.
    """


class UnknownAgentError(LookupError):
    """The delivery was for a phone number we do not have an agent for."""


@dataclass(frozen=True)
class Claim:
    """A successfully deduplicated inbound message, ready to process."""

    lead_id: uuid.UUID
    agent_id: uuid.UUID
    message_id: uuid.UUID
    text: str
    channel: Channel


async def resolve_agent(session: AsyncSession, phone_number_id: str | None) -> Agent:
    """Find the agent this delivery belongs to.

    Multi-tenant safety: an unmapped `phone_number_id` must not silently fall back
    to some other agent's inventory and calendar, so this raises instead.
    """
    if not phone_number_id:
        raise UnknownAgentError("delivery carried no phone_number_id")
    agent = (
        await session.execute(
            select(Agent).where(
                Agent.whatsapp_phone_number_id == phone_number_id,
                Agent.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise UnknownAgentError(f"no active agent registered for phone_number_id {phone_number_id}")
    return agent


async def get_or_create_lead(
    session: AsyncSession,
    agent: Agent,
    phone: str,
    *,
    source: str,
    profile_name: str | None = None,
    language: Language = Language.ENGLISH,
) -> Lead:
    lead = (
        await session.execute(select(Lead).where(Lead.agent_id == agent.id, Lead.phone == phone))
    ).scalar_one_or_none()

    if lead is None:
        lead = Lead(
            agent_id=agent.id,
            phone=phone,
            name=profile_name,
            source=source,
            language=language,
            timezone=agent.timezone,
        )
        session.add(lead)
        await session.flush()
        log.info("new lead %s from %s (%s)", lead.id, source, mask_phone(phone))
    elif profile_name and not lead.name:
        lead.name = profile_name

    return lead


async def get_or_create_conversation(
    session: AsyncSession, lead: Lead, channel: Channel
) -> Conversation:
    from app.models.enums import ConversationStatus

    conversation = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.lead_id == lead.id,
                Conversation.channel == channel,
                Conversation.status != ConversationStatus.CLOSED,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if conversation is None:
        conversation = Conversation(lead_id=lead.id, channel=channel)
        session.add(conversation)
        await session.flush()
    return conversation


async def claim_inbound(
    session: AsyncSession, inbound: InboundMessage, *, source: str = "whatsapp"
) -> Claim | None:
    """Record the message, or return None if we have already seen it.

    Two layers of deduplication: a cheap existence check, and the unique
    constraint on `(channel, external_id)` for the case where two deliveries of
    the same message race each other.
    """
    agent = await resolve_agent(session, inbound.recipient)

    if inbound.external_id:
        already = (
            await session.execute(
                select(Message.id).where(
                    Message.channel == inbound.channel,
                    Message.external_id == inbound.external_id,
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            log.info("ignoring duplicate delivery of %s", inbound.external_id)
            return None

    lead = await get_or_create_lead(
        session,
        agent,
        inbound.sender,
        source=source,
        profile_name=inbound.raw.get("profile_name"),
    )
    conversation = await get_or_create_conversation(session, lead, inbound.channel)

    message = Message(
        conversation_id=conversation.id,
        role=MessageRole.LEAD,
        direction=MessageDirection.INBOUND,
        channel=inbound.channel,
        status=MessageStatus.RECEIVED,
        content=inbound.text,
        external_id=inbound.external_id,
        media_urls=list(inbound.media_urls),
        meta={k: v for k, v in inbound.raw.items() if v is not None},
        sent_at=inbound.received_at or datetime.now(UTC),
    )
    session.add(message)
    conversation.last_message_at = message.sent_at

    # A lead messaging us first is the opt-in, and it is a fact about *their*
    # action — record it here rather than during the model turn, so an engine
    # outage cannot leave a genuine opt-in unrecorded.
    if lead.consent_status is ConsentStatus.UNKNOWN:
        lead.consent_status = ConsentStatus.OPTED_IN

    try:
        await session.flush()
    except IntegrityError:
        # Lost a race with a concurrent delivery of the same message.
        await session.rollback()
        log.info("concurrent duplicate delivery of %s ignored", inbound.external_id)
        return None

    return Claim(
        lead_id=lead.id,
        agent_id=agent.id,
        message_id=message.id,
        text=inbound.text,
        channel=inbound.channel,
    )


async def process_claimed(
    session: AsyncSession,
    llm: LLMClient,
    adapter: ChannelAdapter,
    claim: Claim,
    settings: Settings | None = None,
    now: datetime | None = None,
    calendar: GoogleCalendarClient | None = None,
) -> EngineResult:
    """Run the model turn for a claimed message and send the reply."""
    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    lead = await session.get_one(Lead, claim.lead_id)
    agent = await session.get_one(Agent, claim.agent_id)
    recorded = await session.get_one(Message, claim.message_id)

    engine = ConversationEngine(session, llm, settings, calendar=calendar)
    result = await engine.handle_inbound(
        lead=lead,
        agent=agent,
        text=claim.text,
        channel=claim.channel,
        now=now,
        recorded_inbound=recorded,
    )

    if result.reply:
        await deliver(session, adapter, lead, result, claim.channel, settings, now)

    return result


async def deliver(
    session: AsyncSession,
    adapter: ChannelAdapter,
    lead: Lead,
    result: EngineResult,
    channel: Channel,
    settings: Settings,
    now: datetime,
) -> None:
    """Send the engine's reply and record what the provider said about it."""
    outbound_row = (
        await session.execute(
            select(Message)
            .where(
                Message.conversation_id == result.conversation.id,
                Message.direction == MessageDirection.OUTBOUND,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    free_form = within_service_window(
        lead.last_inbound_at, now, settings.whatsapp_service_window_hours
    )
    if not free_form:
        # Replies are always inside the window in practice; this guards the case
        # where processing was delayed past it. M5 owns approved templates.
        log.warning(
            "skipping reply to lead %s — outside the %sh service window",
            lead.id,
            settings.whatsapp_service_window_hours,
        )
        if outbound_row is not None:
            outbound_row.status = MessageStatus.FAILED
        return

    delivery = await adapter.send(
        OutboundMessage(channel=channel, recipient=lead.phone, text=result.reply, lead_id=lead.id)
    )

    if outbound_row is not None:
        outbound_row.external_id = delivery.external_id
        outbound_row.status = MessageStatus.SENT if delivery.accepted else MessageStatus.FAILED
    if delivery.accepted:
        lead.last_outbound_at = now
        return

    log.error("delivery to lead %s failed: %s", lead.id, delivery.error)
    # Raise so the caller can retry. Before M8 this only logged, which meant a
    # provider outage silently produced leads who were qualified but never
    # answered — the same loss the queue exists to prevent, one step further
    # along. The row is left marked FAILED first so the state is right for any
    # caller that chooses to commit rather than retry.
    raise DeliveryRejectedError(delivery.error or "the channel rejected the message")


async def apply_status_updates(session: AsyncSession, updates: list[StatusUpdate]) -> int:
    """Record delivery receipts against the messages we sent."""
    applied = 0
    for update in updates:
        message = (
            await session.execute(
                select(Message).where(
                    Message.channel == Channel.WHATSAPP,
                    Message.external_id == update.external_id,
                )
            )
        ).scalar_one_or_none()
        if message is None:
            continue
        # Receipts can arrive out of order; never regress a message's status.
        if _rank(update.status) > _rank(message.status):
            message.status = update.status
            applied += 1
        if update.error:
            message.meta = {**message.meta, "delivery_error": update.error}
            log.warning("whatsapp delivery error for %s: %s", update.external_id, update.error)
    return applied


_STATUS_ORDER = {
    MessageStatus.PENDING: 0,
    MessageStatus.SENT: 1,
    MessageStatus.DELIVERED: 2,
    MessageStatus.READ: 3,
    MessageStatus.RECEIVED: 3,
    MessageStatus.FAILED: 4,
}


def _rank(status: MessageStatus) -> int:
    return _STATUS_ORDER.get(status, 0)


def session_factory() -> async_sessionmaker[AsyncSession]:
    from app.core.db import get_sessionmaker

    return get_sessionmaker()
