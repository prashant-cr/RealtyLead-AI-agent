"""Run one inbound turn end to end.

Extracted in M8 because two callers now need it: the inbound worker draining the
Redis queue, and the webhook's in-process fallback for when Redis is down. It
owns the things a turn needs but a claim does not carry — the model client, the
agent's WhatsApp adapter, a calendar client — and it is the one place the
per-lead inbound rate limit is applied.

The outcome distinction matters more than it looks. The queue retries anything
that is not acknowledged, so a failure that will never succeed (an agent with no
WhatsApp number, a missing API key) must be reported as terminal rather than
transient — otherwise it burns its whole retry budget and lands in the
dead-letter stream, making a configuration mistake look like an outage.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm import AnthropicLLM, LLMError
from app.channels.whatsapp import WhatsAppChannel, WhatsAppError
from app.core.config import Settings
from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.core.redis import RedisLike
from app.models import Agent
from app.services.google_calendar import GoogleCalendarClient
from app.services.ingestion import Claim, DeliveryRejectedError, process_claimed
from app.services.ratelimit import check, inbound_limit

log = get_logger(__name__)


class TurnOutcome(StrEnum):
    """What happened, and — implicitly — whether it is worth trying again."""

    COMPLETED = "completed"
    # The lead is sending faster than their budget allows. Their message is
    # already recorded; we simply do not spend a model call on it.
    RATE_LIMITED = "rate_limited"
    # Terminal: retrying cannot help until a human changes something.
    NOT_CONFIGURED = "not_configured"
    # Transient: the model or WhatsApp failed in a way that may not recur.
    FAILED = "failed"

    @property
    def should_retry(self) -> bool:
        return self is TurnOutcome.FAILED


async def run_claim(
    claim: Claim,
    settings: Settings,
    *,
    session: AsyncSession | None = None,
    redis: RedisLike | None = None,
) -> TurnOutcome:
    """Process one claimed inbound message: model turn, tools, reply.

    `session` is injectable so tests can drive this on their own transaction;
    when it is omitted a session is opened and committed here.
    """
    if session is not None:
        return await _run(claim, settings, session, redis)

    async with get_sessionmaker()() as owned:
        outcome = await _run(claim, settings, owned, redis)
        if outcome is TurnOutcome.COMPLETED:
            await owned.commit()
        else:
            await owned.rollback()
        return outcome


async def _run(
    claim: Claim,
    settings: Settings,
    session: AsyncSession,
    redis: RedisLike | None,
) -> TurnOutcome:
    decision = await check(inbound_limit(settings), str(claim.lead_id), client=redis)
    if not decision.allowed:
        log.warning(
            "lead %s is over the inbound limit (%s per %ss); not spending a model call",
            claim.lead_id,
            decision.limit.max_events,
            decision.limit.window_seconds,
        )
        return TurnOutcome.RATE_LIMITED

    try:
        llm = AnthropicLLM(settings)
    except LLMError as exc:
        log.error("cannot process claim %s: %s", claim.message_id, exc)
        return TurnOutcome.NOT_CONFIGURED

    adapter: WhatsAppChannel | None = None
    calendar: GoogleCalendarClient | None = None
    try:
        agent = await session.get_one(Agent, claim.agent_id)
        if not agent.whatsapp_phone_number_id:
            log.error("agent %s has no whatsapp_phone_number_id", agent.id)
            return TurnOutcome.NOT_CONFIGURED

        adapter = WhatsAppChannel(agent.whatsapp_phone_number_id, settings)
        if agent.google_refresh_token:
            calendar = GoogleCalendarClient(settings)

        await process_claimed(session, llm, adapter, claim, settings, calendar=calendar)
        return TurnOutcome.COMPLETED

    except DeliveryRejectedError as exc:
        # The turn is rolled back by the caller and retried whole, rather than
        # committing the reply and retrying only the send. That costs another
        # model call, but it keeps the transcript honest: one attempt, one
        # assistant message, and the lead sees exactly one reply when it lands.
        log.error("reply to lead %s was not delivered: %s", claim.lead_id, exc)
        return TurnOutcome.FAILED
    except (WhatsAppError, LLMError) as exc:
        log.error("failed to process claim %s: %s", claim.message_id, exc)
        return TurnOutcome.FAILED
    except Exception:
        log.exception("unexpected failure processing claim %s", claim.message_id)
        return TurnOutcome.FAILED
    finally:
        if adapter is not None:
            await adapter.close()
        if calendar is not None:
            await calendar.close()
