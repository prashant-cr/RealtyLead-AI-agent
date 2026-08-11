"""How one inbound turn reports its outcome.

This is the seam where a real bug lived: `deliver` logged a rejected send and
returned normally, so `run_claim` reported COMPLETED and the worker acknowledged
a message the lead never received. It was found by running the stack against a
bad WhatsApp token, not by any test — hence these.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.enums import Channel
from app.services import turn as turn_module
from app.services.ingestion import Claim, DeliveryRejectedError
from app.services.turn import TurnOutcome, run_claim
from tests.factories import make_agent
from tests.fakes import FakeRedis


def settings() -> Settings:
    return Settings(anthropic_api_key="sk-test", whatsapp_access_token="tok")


class _Stub:
    """Stands in for AnthropicLLM / WhatsAppChannel / GoogleCalendarClient."""

    def __init__(self, *args: object, **kwargs: object) -> None: ...

    async def close(self) -> None: ...


@pytest.fixture(autouse=True)
def _stub_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_claim` builds its own clients, so they are patched at its module."""
    monkeypatch.setattr(turn_module, "AnthropicLLM", _Stub)
    monkeypatch.setattr(turn_module, "WhatsAppChannel", _Stub)


async def _claim_for(session: AsyncSession, **agent_kwargs: Any) -> Claim:
    agent = make_agent(**agent_kwargs)
    session.add(agent)
    await session.flush()
    return Claim(
        lead_id=uuid.uuid4(),
        agent_id=agent.id,
        message_id=uuid.uuid4(),
        text="is it still available?",
        channel=Channel.WHATSAPP,
    )


async def test_a_rejected_reply_is_retryable_not_complete(
    session: AsyncSession, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: an undelivered reply used to be reported as a completed turn, so
    the worker acknowledged it and the lead was never answered."""

    async def _reject(*_args: object, **_kwargs: object) -> None:
        raise DeliveryRejectedError("Invalid OAuth access token")

    monkeypatch.setattr(turn_module, "process_claimed", _reject)
    claim = await _claim_for(session, whatsapp_phone_number_id="PNID")

    outcome = await run_claim(claim, settings(), session=session, redis=fake_redis)

    assert outcome is TurnOutcome.FAILED
    assert outcome.should_retry


async def test_a_delivered_reply_completes(
    session: AsyncSession, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(turn_module, "process_claimed", _ok)
    claim = await _claim_for(session, whatsapp_phone_number_id="PNID")

    outcome = await run_claim(claim, settings(), session=session, redis=fake_redis)

    assert outcome is TurnOutcome.COMPLETED
    assert not outcome.should_retry


async def test_an_agent_without_a_number_is_terminal_not_retryable(
    session: AsyncSession, fake_redis: FakeRedis
) -> None:
    """Retrying cannot help, and burning the retry budget first would make a
    configuration mistake look like an outage."""
    claim = await _claim_for(session, whatsapp_phone_number_id=None)

    outcome = await run_claim(claim, settings(), session=session, redis=fake_redis)

    assert outcome is TurnOutcome.NOT_CONFIGURED
    assert not outcome.should_retry


async def test_a_lead_over_their_limit_does_not_reach_the_model(
    session: AsyncSession, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the inbound limit is to stop spending on Anthropic calls, so
    the turn has to stop before the model, not after."""
    called = False

    async def _spy(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(turn_module, "process_claimed", _spy)
    config = Settings(
        anthropic_api_key="sk-test", whatsapp_access_token="tok", inbound_messages_per_lead=2
    )
    claim = await _claim_for(session, whatsapp_phone_number_id="PNID")

    for _ in range(2):
        await run_claim(claim, config, session=session, redis=fake_redis)
    called = False
    outcome = await run_claim(claim, config, session=session, redis=fake_redis)

    assert outcome is TurnOutcome.RATE_LIMITED
    assert not outcome.should_retry
    assert not called
