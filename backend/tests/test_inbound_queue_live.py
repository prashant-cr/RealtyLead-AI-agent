"""The inbound queue against a real Redis server.

The rest of the queue suite runs against `tests.fakes.FakeRedis`, which is fast
and hermetic but is also a thing this project wrote — so it proves the code
behaves as intended against our *model* of Redis Streams, not against Redis. The
durability guarantee depends on real `XREADGROUP` / `XAUTOCLAIM` semantics, so it
is worth confirming at least once on the real thing.

Opt in, mirroring `test_migrations.py`:

    REDIS_TEST_URL=redis://localhost:6380/9 pytest tests/test_inbound_queue_live.py

Use a database you do not mind losing — the fixture flushes the keys it uses.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest
from redis.asyncio import from_url

from app.core.redis import RedisLike
from app.models.enums import Channel
from app.services.inbound_queue import (
    DEAD_LETTER_STREAM,
    STREAM,
    ack,
    consume,
    enqueue,
    ensure_group,
    reclaim_stale,
)
from app.services.ingestion import Claim

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL")

pytestmark = pytest.mark.skipif(
    not REDIS_TEST_URL, reason="set REDIS_TEST_URL to run against a real Redis"
)


@pytest.fixture
async def live_redis() -> AsyncIterator[RedisLike]:
    client = from_url(REDIS_TEST_URL or "", decode_responses=True)
    await client.delete(STREAM, DEAD_LETTER_STREAM)
    yield cast(RedisLike, client)
    await client.delete(STREAM, DEAD_LETTER_STREAM)
    await client.aclose()


def a_claim(text: str) -> Claim:
    return Claim(
        lead_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        text=text,
        channel=Channel.WHATSAPP,
    )


async def test_round_trip_against_real_redis(live_redis: RedisLike) -> None:
    await ensure_group(live_redis)
    claim = a_claim("budget is 85-90 lakhs")

    await enqueue(live_redis, claim)
    [queued] = await consume(live_redis, "worker-1", block_ms=100)

    assert queued.claim == claim
    await ack(live_redis, queued)


async def test_busygroup_is_tolerated_by_real_redis(live_redis: RedisLike) -> None:
    """`ensure_group` swallows BUSYGROUP by matching on the error text, which is
    exactly the kind of thing a fake can get wrong."""
    await ensure_group(live_redis)
    await ensure_group(live_redis)


async def test_real_redis_gives_a_message_to_only_one_consumer(live_redis: RedisLike) -> None:
    await ensure_group(live_redis)
    await enqueue(live_redis, a_claim("only once please"))

    first = await consume(live_redis, "worker-1", block_ms=100)
    second = await consume(live_redis, "worker-2", block_ms=100)

    assert len(first) == 1
    assert second == []


async def test_real_redis_redelivers_an_unacknowledged_message(live_redis: RedisLike) -> None:
    """The crash guarantee, on real XAUTOCLAIM. `min_idle_ms=0` stands in for
    time passing so the test does not have to sleep for the real threshold."""
    await ensure_group(live_redis)
    await enqueue(live_redis, a_claim("still waiting"))
    [taken] = await consume(live_redis, "doomed-worker", block_ms=100)

    report = await reclaim_stale(live_redis, "worker-2", min_idle_ms=0)

    assert report.retried == 1
    [again] = await consume(live_redis, "worker-2", block_ms=100)
    assert again.claim.text == taken.claim.text
    assert again.attempts == 2


async def test_real_redis_does_not_redeliver_after_ack(live_redis: RedisLike) -> None:
    await ensure_group(live_redis)
    await enqueue(live_redis, a_claim("handled"))
    [queued] = await consume(live_redis, "worker-1", block_ms=100)

    await ack(live_redis, queued)
    report = await reclaim_stale(live_redis, "worker-2", min_idle_ms=0)

    assert report.total == 0
