"""A durable queue for inbound messages, on Redis Streams.

Until M8 the webhook handed each claimed message to a FastAPI `BackgroundTasks`
callback. That is in-process: a crash, an OOM kill or a redeploy between the 200
we send Meta and the reply we send the lead lost that turn silently, with no
retry and no record. Since Meta considers a 200 final, the lead simply never
heard back. This module replaces that path.

Why streams rather than a list or a second Postgres table:

* A list (`LPUSH`/`BRPOP`) pops entries off the queue before they are processed,
  so a worker that dies mid-turn loses the message — the same failure we are
  fixing, moved one process along. Streams keep a per-consumer-group pending
  list, so an unacknowledged entry is recoverable.
* Postgres would work and `follow_up_tasks` already does exactly that for
  nudges. The difference is latency: a follow-up is due at a date and a minute of
  polling delay is invisible, whereas a lead waiting on a reply notices seconds.
  Streams let a worker block on `XREADGROUP` and wake the instant work arrives.

Retry strategy, and why failures do nothing:

A failed turn is simply *not acknowledged*. Redis keeps it in the group's pending
list, and `reclaim_stale` picks it up once it has been idle long enough. This
means the idle threshold *is* the backoff — there is no sleeping consumer, no
delay queue and no re-enqueue on the hot path. The reclaimer is also the only
place that counts attempts, so the retry budget survives a worker dying
mid-turn, which a counter held in the worker would not.

Entries that exhaust their attempts move to a dead-letter stream rather than
being dropped. A lead's message is not something to discard quietly.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.core.redis import MAX_BLOCK_MS, RedisLike
from app.models.enums import Channel
from app.services.ingestion import Claim

log = get_logger(__name__)

STREAM = "realtylead:inbound"
DEAD_LETTER_STREAM = "realtylead:inbound:dead"
GROUP = "inbound-workers"

# Ceiling on the stream's length. Entries are acknowledged and trimmed in normal
# operation; this only bounds the damage if every worker is down for a long time,
# so that Redis does not fill up. Approximate trimming (`~`) is much cheaper and
# the exact bound does not matter.
MAX_STREAM_LENGTH = 10_000

# How long an unacknowledged entry must sit before another worker may take it.
# Also the retry backoff — see the module docstring. Comfortably longer than a
# slow model turn plus a WhatsApp send, so a healthy-but-slow worker is never
# raced by the reclaimer.
DEFAULT_MIN_IDLE_MS = 120_000

# Total attempts before an entry is dead-lettered, including the first.
MAX_ATTEMPTS = 4


@dataclass(frozen=True)
class QueuedClaim:
    """A claim read off the stream, with the bookkeeping needed to ack it."""

    entry_id: str
    claim: Claim
    attempts: int
    enqueued_at: str

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS


def _encode(claim: Claim, attempts: int, enqueued_at: str | None = None) -> dict[str, str]:
    return {
        "claim": json.dumps(
            {
                "lead_id": str(claim.lead_id),
                "agent_id": str(claim.agent_id),
                "message_id": str(claim.message_id),
                "text": claim.text,
                "channel": claim.channel.value,
            }
        ),
        "attempts": str(attempts),
        "enqueued_at": enqueued_at or datetime.now(UTC).isoformat(),
    }


def _decode(entry_id: str, fields: dict[str, Any]) -> QueuedClaim | None:
    """Parse one stream entry, or return None if it is unusable.

    A malformed entry must not be able to stall the queue: it is logged and
    treated as unrecoverable by the caller, which acknowledges it rather than
    letting it be redelivered forever.
    """
    try:
        raw = json.loads(fields["claim"])
        claim = Claim(
            lead_id=uuid.UUID(raw["lead_id"]),
            agent_id=uuid.UUID(raw["agent_id"]),
            message_id=uuid.UUID(raw["message_id"]),
            text=raw["text"],
            channel=Channel(raw["channel"]),
        )
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.error("discarding malformed queue entry %s: %s", entry_id, exc)
        return None

    return QueuedClaim(
        entry_id=entry_id,
        claim=claim,
        attempts=int(fields.get("attempts", 1)),
        enqueued_at=fields.get("enqueued_at", ""),
    )


async def ensure_group(client: RedisLike) -> None:
    """Create the consumer group, and the stream with it, if absent.

    `BUSYGROUP` just means another process won the race, which is the expected
    outcome whenever more than one worker starts at once.
    """
    try:
        await client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    return None


async def enqueue(client: RedisLike, claim: Claim) -> str:
    """Put a claimed message on the queue. Returns the stream entry id."""
    entry_id = await client.xadd(STREAM, _encode(claim, attempts=1), maxlen=MAX_STREAM_LENGTH)
    log.info("queued inbound message %s for lead %s", claim.message_id, claim.lead_id)
    return str(entry_id)


async def consume(
    client: RedisLike,
    consumer: str,
    *,
    count: int = 10,
    block_ms: int = 5_000,
) -> list[QueuedClaim]:
    """Claim up to `count` new entries for this consumer.

    Blocks up to `block_ms` waiting for work, so an idle worker costs one held
    connection rather than a polling loop.

    `block_ms` is clamped to `MAX_BLOCK_MS`: a block at or above the client's
    socket timeout makes redis-py raise `TimeoutError` on every call rather than
    returning empty, which presents as Redis being down. See `app.core.redis`.
    """
    response = await client.xreadgroup(
        GROUP, consumer, {STREAM: ">"}, count=count, block=min(block_ms, MAX_BLOCK_MS)
    )
    return _flatten(response)


@dataclass
class ReclaimReport:
    """What one recovery pass did. Nothing is returned to be processed —
    retried entries go back on the stream and arrive through `consume`."""

    retried: int = 0
    dead_lettered: int = 0
    discarded: int = 0

    @property
    def total(self) -> int:
        return self.retried + self.dead_lettered + self.discarded

    def __str__(self) -> str:
        return (
            f"retried={self.retried} dead_lettered={self.dead_lettered} discarded={self.discarded}"
        )


async def reclaim_stale(
    client: RedisLike,
    consumer: str,
    *,
    min_idle_ms: int = DEFAULT_MIN_IDLE_MS,
    count: int = 10,
) -> ReclaimReport:
    """Take back entries a previous consumer claimed but never acknowledged.

    This is the recovery path for a worker that died mid-turn, and the retry path
    for a turn that failed. Entries that have used their whole attempt budget are
    dead-lettered here rather than being retried forever.
    """
    response = await client.xautoclaim(
        STREAM, GROUP, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=count
    )
    # XAUTOCLAIM returns (next_cursor, entries, deleted_ids); older servers omit
    # the third element.
    entries = response[1] if isinstance(response, (list, tuple)) and len(response) > 1 else []

    report = ReclaimReport()
    for entry_id, fields in entries or []:
        queued = _decode(str(entry_id), dict(fields))
        if queued is None:
            await client.xack(STREAM, GROUP, str(entry_id))
            report.discarded += 1
            continue

        if queued.is_last_attempt:
            await dead_letter(client, queued, reason="attempts exhausted")
            report.dead_lettered += 1
            continue

        # Re-add with the attempt count bumped, then release the original. The
        # fresh entry is picked up by the ordinary consume() path.
        await client.xadd(
            STREAM,
            _encode(queued.claim, attempts=queued.attempts + 1, enqueued_at=queued.enqueued_at),
            maxlen=MAX_STREAM_LENGTH,
        )
        await client.xack(STREAM, GROUP, queued.entry_id)
        report.retried += 1
        log.warning(
            "retrying inbound message %s (attempt %s of %s)",
            queued.claim.message_id,
            queued.attempts + 1,
            MAX_ATTEMPTS,
        )
    return report


async def ack(client: RedisLike, queued: QueuedClaim) -> None:
    """Mark an entry done. Until this is called the entry is recoverable."""
    await client.xack(STREAM, GROUP, queued.entry_id)


async def dead_letter(client: RedisLike, queued: QueuedClaim, reason: str) -> None:
    """Move an entry to the dead-letter stream and release it from the group.

    Nothing consumes this stream — it exists so a lost message can be found and
    explained, which is the difference between "we dropped it" and "we dropped it
    and cannot tell you why".
    """
    fields = _encode(queued.claim, attempts=queued.attempts, enqueued_at=queued.enqueued_at)
    fields["reason"] = reason
    fields["dead_lettered_at"] = datetime.now(UTC).isoformat()
    await client.xadd(DEAD_LETTER_STREAM, fields, maxlen=MAX_STREAM_LENGTH)
    await client.xack(STREAM, GROUP, queued.entry_id)
    log.error(
        "dead-lettered inbound message %s after %s attempts: %s",
        queued.claim.message_id,
        queued.attempts,
        reason,
    )


async def depth(client: RedisLike) -> dict[str, int]:
    """How much work is actually outstanding, for probes and for operators.

    `in_flight` comes from XPENDING, not XLEN. XLEN counts every entry the stream
    still retains, including ones long since handled — it only falls when the
    `maxlen` trim kicks in, so it would report a busy queue on an idle system and
    is useless as a backlog signal. XPENDING counts entries delivered to a
    consumer and not yet acknowledged, which is the number that should worry
    someone. The dead-letter stream is different: nothing consumes it, so its
    length is exactly its contents.
    """
    pending = await client.xpending(STREAM, GROUP)
    in_flight = int(pending.get("pending", 0)) if isinstance(pending, dict) else int(pending or 0)
    return {
        "in_flight": in_flight,
        "retained": int(await client.xlen(STREAM)),
        "dead_lettered": int(await client.xlen(DEAD_LETTER_STREAM)),
    }


def _flatten(response: Any) -> list[QueuedClaim]:
    """Turn redis-py's [(stream, [(id, {fields}), ...])] into QueuedClaims.

    Entries that fail to decode are dropped here and never acknowledged, so they
    stay pending and are discarded by the next `reclaim_stale` pass. That keeps
    this helper synchronous and puts every ack decision in one place.
    """
    queued: list[QueuedClaim] = []
    for _stream, entries in response or []:
        for entry_id, fields in entries:
            parsed = _decode(str(entry_id), dict(fields))
            if parsed is not None:
                queued.append(parsed)
    return queued
