"""The durable inbound queue.

These tests are the evidence for M8's central claim: a message that has been
acknowledged to Meta cannot be lost by this service crashing. Each one describes
a way the old in-process path failed.
"""

from __future__ import annotations

import uuid

from app.core.redis import MAX_BLOCK_MS, SOCKET_TIMEOUT_SECONDS
from app.models.enums import Channel
from app.services.inbound_queue import (
    DEAD_LETTER_STREAM,
    GROUP,
    MAX_ATTEMPTS,
    STREAM,
    ack,
    consume,
    dead_letter,
    depth,
    enqueue,
    ensure_group,
    reclaim_stale,
)
from app.services.ingestion import Claim
from tests.fakes import FakeRedis


def a_claim(text: str = "is the 3 BHK still available?") -> Claim:
    return Claim(
        lead_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        text=text,
        channel=Channel.WHATSAPP,
    )


async def test_a_claim_survives_a_round_trip(fake_redis: FakeRedis) -> None:
    await ensure_group(fake_redis)
    claim = a_claim("budget is 85-90 lakhs")

    await enqueue(fake_redis, claim)
    [queued] = await consume(fake_redis, "worker-1")

    assert queued.claim == claim
    assert queued.attempts == 1


async def test_creating_the_group_twice_is_not_an_error(fake_redis: FakeRedis) -> None:
    """Every worker calls this on startup, so the second one always races."""
    await ensure_group(fake_redis)
    await ensure_group(fake_redis)


async def test_two_workers_never_get_the_same_message(fake_redis: FakeRedis) -> None:
    await ensure_group(fake_redis)
    await enqueue(fake_redis, a_claim())

    first = await consume(fake_redis, "worker-1")
    second = await consume(fake_redis, "worker-2")

    assert len(first) == 1
    assert second == []


async def test_an_unacknowledged_message_is_redelivered(fake_redis: FakeRedis) -> None:
    """The crash case. A worker takes a message, dies before replying, and the
    message must come back rather than disappear."""
    await ensure_group(fake_redis)
    await enqueue(fake_redis, a_claim())
    [taken] = await consume(fake_redis, "doomed-worker")

    fake_redis.advance(300)  # the worker is long gone
    report = await reclaim_stale(fake_redis, "worker-2", min_idle_ms=120_000)

    assert report.retried == 1
    [again] = await consume(fake_redis, "worker-2")
    assert again.claim == taken.claim
    assert again.attempts == 2


async def test_an_acknowledged_message_is_not_redelivered(fake_redis: FakeRedis) -> None:
    await ensure_group(fake_redis)
    await enqueue(fake_redis, a_claim())
    [queued] = await consume(fake_redis, "worker-1")

    await ack(fake_redis, queued)
    fake_redis.advance(300)
    report = await reclaim_stale(fake_redis, "worker-2", min_idle_ms=120_000)

    assert report.total == 0


async def test_a_message_is_not_reclaimed_before_it_is_stale(fake_redis: FakeRedis) -> None:
    """A slow-but-healthy worker must not be raced by the reclaimer."""
    await ensure_group(fake_redis)
    await enqueue(fake_redis, a_claim())
    await consume(fake_redis, "slow-worker")

    fake_redis.advance(30)  # still well inside the idle threshold
    report = await reclaim_stale(fake_redis, "worker-2", min_idle_ms=120_000)

    assert report.total == 0


async def test_a_message_is_dead_lettered_once_attempts_run_out(fake_redis: FakeRedis) -> None:
    await ensure_group(fake_redis)
    await enqueue(fake_redis, a_claim())

    # Each cycle: take it, abandon it, let the reclaimer retry.
    for _ in range(MAX_ATTEMPTS + 1):
        await consume(fake_redis, "worker-1")
        fake_redis.advance(300)
        await reclaim_stale(fake_redis, "worker-1", min_idle_ms=120_000)

    assert await fake_redis.xlen(DEAD_LETTER_STREAM) == 1
    assert await consume(fake_redis, "worker-2") == []


async def test_dead_lettering_keeps_the_message_and_the_reason(fake_redis: FakeRedis) -> None:
    """A lost message must be explainable, not merely gone."""
    await ensure_group(fake_redis)
    claim = a_claim("can you do 78 lakhs?")
    await enqueue(fake_redis, claim)
    [queued] = await consume(fake_redis, "worker-1")

    await dead_letter(fake_redis, queued, reason="agent not configured for messaging")

    [(_id, fields)] = fake_redis.streams[DEAD_LETTER_STREAM]
    assert "can you do 78 lakhs?" in fields["claim"]
    assert fields["reason"] == "agent not configured for messaging"
    assert fields["dead_lettered_at"]


async def test_a_malformed_entry_cannot_stall_the_queue(fake_redis: FakeRedis) -> None:
    """A bad entry must not be redelivered forever ahead of real work."""
    await ensure_group(fake_redis)
    await fake_redis.xadd(STREAM, {"claim": "not json", "attempts": "1"})
    await enqueue(fake_redis, a_claim("a real message"))

    delivered = await consume(fake_redis, "worker-1")

    assert len(delivered) == 1
    assert delivered[0].claim.text == "a real message"

    fake_redis.advance(300)
    report = await reclaim_stale(fake_redis, "worker-1", min_idle_ms=120_000)
    assert report.discarded == 1


async def test_depth_counts_outstanding_work_not_retained_entries(
    fake_redis: FakeRedis,
) -> None:
    """XLEN would report a busy queue on an idle system, because entries are
    retained after being acknowledged. `in_flight` has to come from XPENDING."""
    await ensure_group(fake_redis)
    await enqueue(fake_redis, a_claim())
    await enqueue(fake_redis, a_claim())
    first, second = await consume(fake_redis, "worker-1")

    await ack(fake_redis, first)
    await dead_letter(fake_redis, second, reason="testing")

    assert await depth(fake_redis) == {
        "in_flight": 0,  # both resolved, even though both entries are retained
        "retained": 2,
        "dead_lettered": 1,
    }


async def test_the_queue_survives_the_consumer_group_being_recreated(
    fake_redis: FakeRedis,
) -> None:
    """Restarting every worker must not orphan queued work."""
    await ensure_group(fake_redis)
    await enqueue(fake_redis, a_claim("still here?"))

    await ensure_group(fake_redis)  # a second worker starting up

    [queued] = await consume(fake_redis, "worker-1")
    assert queued.claim.text == "still here?"
    assert (STREAM, GROUP) in fake_redis.groups


async def test_a_long_block_is_clamped_below_the_socket_timeout(fake_redis: FakeRedis) -> None:
    """Regression: `BLOCK` at or above redis-py's socket timeout makes every call
    raise TimeoutError instead of returning empty, which reads as Redis being
    down. Found by running the worker — no unit test caught it."""
    await ensure_group(fake_redis)
    seen: dict[str, object] = {}
    original = fake_redis.xreadgroup

    async def spy(*args: object, **kwargs: object) -> object:
        seen["block"] = kwargs.get("block")
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    fake_redis.xreadgroup = spy  # type: ignore[method-assign]
    await consume(fake_redis, "worker-1", block_ms=999_000)

    assert seen["block"] == MAX_BLOCK_MS
    assert MAX_BLOCK_MS < SOCKET_TIMEOUT_SECONDS * 1000
