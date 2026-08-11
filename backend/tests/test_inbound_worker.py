"""The inbound worker's acknowledgement decisions.

What the worker does to the *queue* matters as much as what it does to the lead:
acknowledge too eagerly and a failed turn is lost, acknowledge too little and a
permanent failure is retried until it is dead-lettered. Each test below pins one
of those decisions.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import Settings
from app.models.enums import Channel
from app.services.inbound_queue import (
    DEAD_LETTER_STREAM,
    GROUP,
    STREAM,
    consume,
    enqueue,
    ensure_group,
)
from app.services.ingestion import Claim
from app.services.turn import TurnOutcome
from app.workers import inbound_worker
from app.workers.inbound_worker import RunReport, handle
from tests.fakes import FakeRedis


def a_claim() -> Claim:
    return Claim(
        lead_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        text="is it still available?",
        channel=Channel.WHATSAPP,
    )


async def queued_entry(client: FakeRedis) -> object:
    await ensure_group(client)
    await enqueue(client, a_claim())
    [queued] = await consume(client, "worker-1")
    return queued


def pending_count(client: FakeRedis) -> int:
    return len(client.groups[(STREAM, GROUP)]["pending"])


@pytest.fixture
def settings() -> Settings:
    return Settings()


async def test_a_completed_turn_is_acknowledged(
    fake_redis: FakeRedis, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inbound_worker, "run_claim", _returning(TurnOutcome.COMPLETED))
    queued = await queued_entry(fake_redis)
    report = RunReport()

    await handle(queued, settings, fake_redis, report)  # type: ignore[arg-type]

    assert report.completed == 1
    assert pending_count(fake_redis) == 0


async def test_a_transient_failure_is_left_for_retry(
    fake_redis: FakeRedis, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the whole point of M8: a failed turn stays recoverable."""
    monkeypatch.setattr(inbound_worker, "run_claim", _returning(TurnOutcome.FAILED))
    queued = await queued_entry(fake_redis)
    report = RunReport()

    await handle(queued, settings, fake_redis, report)  # type: ignore[arg-type]

    assert report.failed == 1
    assert pending_count(fake_redis) == 1
    assert await fake_redis.xlen(DEAD_LETTER_STREAM) == 0


async def test_a_rate_limited_turn_is_acknowledged_not_retried(
    fake_redis: FakeRedis, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying would just spend the next window's budget on the same message."""
    monkeypatch.setattr(inbound_worker, "run_claim", _returning(TurnOutcome.RATE_LIMITED))
    queued = await queued_entry(fake_redis)
    report = RunReport()

    await handle(queued, settings, fake_redis, report)  # type: ignore[arg-type]

    assert report.rate_limited == 1
    assert pending_count(fake_redis) == 0
    assert await fake_redis.xlen(DEAD_LETTER_STREAM) == 0


async def test_a_misconfigured_agent_is_dead_lettered_immediately(
    fake_redis: FakeRedis, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying cannot help until a human fixes the agent, and burning the retry
    budget first would make a configuration mistake look like an outage."""
    monkeypatch.setattr(inbound_worker, "run_claim", _returning(TurnOutcome.NOT_CONFIGURED))
    queued = await queued_entry(fake_redis)
    report = RunReport()

    await handle(queued, settings, fake_redis, report)  # type: ignore[arg-type]

    assert report.not_configured == 1
    assert pending_count(fake_redis) == 0
    assert await fake_redis.xlen(DEAD_LETTER_STREAM) == 1


async def test_a_failure_on_the_final_attempt_is_dead_lettered(
    fake_redis: FakeRedis, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inbound_worker, "run_claim", _returning(TurnOutcome.FAILED))
    await ensure_group(fake_redis)
    # Hand-place an entry that has already used its budget.
    from app.services.inbound_queue import MAX_ATTEMPTS, _encode

    await fake_redis.xadd(STREAM, _encode(a_claim(), attempts=MAX_ATTEMPTS))
    [queued] = await consume(fake_redis, "worker-1")
    report = RunReport()

    await handle(queued, settings, fake_redis, report)  # type: ignore[arg-type]

    assert pending_count(fake_redis) == 0
    assert await fake_redis.xlen(DEAD_LETTER_STREAM) == 1


async def test_consumer_names_differ_between_processes() -> None:
    """Two workers sharing a consumer name would share a pending list, so one
    could reclaim work the other is actively doing."""
    assert inbound_worker.consumer_name() == inbound_worker.consumer_name()
    assert str(__import__("os").getpid()) in inbound_worker.consumer_name()


def _returning(outcome: TurnOutcome) -> object:
    async def _run(*_args: object, **_kwargs: object) -> TurnOutcome:
        return outcome

    return _run
