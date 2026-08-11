"""The inbound worker: drain the Redis queue of claimed messages and reply.

Run it alongside the API and the follow-up worker:

    make inbound-worker              # or: python -m app.workers.inbound_worker
    python -m app.workers.inbound_worker --once    # drain what is waiting, exit

Before M8 the webhook ran the model turn in a FastAPI background task, so a
crash between acknowledging Meta and replying to the lead lost that turn with no
retry and no trace. This worker replaces that: the webhook only enqueues, and
every turn is acknowledged in Redis exactly once, after it has actually been
handled.

Several of these can run at once. Redis assigns each stream entry to a single
consumer in the group, so two workers never process the same message; each just
needs its own consumer name, which defaults to the hostname and pid.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.core.logging import configure_logging, get_logger
from app.core.redis import RedisLike, close_redis, get_redis
from app.services.inbound_queue import (
    QueuedClaim,
    ReclaimReport,
    ack,
    consume,
    dead_letter,
    ensure_group,
    reclaim_stale,
)
from app.services.turn import TurnOutcome, run_claim
from app.workers.followup_worker import with_suppress

log = get_logger(__name__)

DEFAULT_BATCH_SIZE = 10
DEFAULT_BLOCK_MS = 5_000
# How often to sweep for turns abandoned by a dead worker. Cheap (one XAUTOCLAIM)
# and independent of the retry delay, which is the entry's idle time.
RECLAIM_EVERY_PASSES = 6


@dataclass
class RunReport:
    completed: int = 0
    rate_limited: int = 0
    not_configured: int = 0
    failed: int = 0
    reclaimed: ReclaimReport | None = None

    @property
    def processed(self) -> int:
        return self.completed + self.rate_limited + self.not_configured + self.failed

    def __str__(self) -> str:
        base = (
            f"completed={self.completed} rate_limited={self.rate_limited} "
            f"not_configured={self.not_configured} failed={self.failed}"
        )
        return f"{base} ({self.reclaimed})" if self.reclaimed else base


def consumer_name() -> str:
    """Unique per process, so two workers never share a pending list."""
    return f"{socket.gethostname()}-{os.getpid()}"


async def handle(
    queued: QueuedClaim,
    settings: Settings,
    client: RedisLike,
    report: RunReport,
) -> None:
    """Process one entry and decide its fate in the queue.

    Only a transient failure is left unacknowledged — that is what makes it
    eligible for redelivery. Everything else is acknowledged here, because
    retrying it would produce the same result and eventually dead-letter a
    message that was in fact handled correctly.
    """
    outcome = await run_claim(queued.claim, settings, redis=client)

    if outcome is TurnOutcome.COMPLETED:
        report.completed += 1
        await ack(client, queued)
        return

    if outcome is TurnOutcome.RATE_LIMITED:
        report.rate_limited += 1
        await ack(client, queued)
        return

    if outcome is TurnOutcome.NOT_CONFIGURED:
        report.not_configured += 1
        # Terminal, but worth keeping: this is a misconfigured agent, and the
        # dead-letter stream is where someone will look for the lead who never
        # got a reply.
        await dead_letter(client, queued, reason="agent not configured for messaging")
        return

    report.failed += 1
    if queued.is_last_attempt:
        await dead_letter(client, queued, reason="failed on the final attempt")
        return

    # Left unacknowledged on purpose: reclaim_stale will retry it once it has
    # been idle long enough. See app/services/inbound_queue.py.
    log.warning(
        "leaving message %s pending for retry (attempt %s)",
        queued.claim.message_id,
        queued.attempts,
    )


async def run_once(
    settings: Settings | None = None,
    *,
    client: RedisLike | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    block_ms: int = DEFAULT_BLOCK_MS,
    reclaim: bool = True,
) -> RunReport:
    """One pass: optionally recover stale entries, then drain a batch."""
    settings = settings or get_settings()
    client = client or get_redis(settings)
    report = RunReport()

    await ensure_group(client)

    if reclaim:
        report.reclaimed = await reclaim_stale(
            client,
            consumer_name(),
            min_idle_ms=settings.inbound_retry_after_seconds * 1000,
            count=batch_size,
        )

    for queued in await consume(client, consumer_name(), count=batch_size, block_ms=block_ms):
        await handle(queued, settings, client, report)

    return report


async def run_forever(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    client = get_redis(settings)
    stopping = asyncio.Event()

    def _stop() -> None:
        log.info("stop requested; finishing the current batch")
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with_suppress(loop, sig, _stop)

    log.info("inbound worker started as %s", consumer_name())
    passes = 0
    while not stopping.is_set():
        try:
            report = await run_once(
                settings,
                client=client,
                # Sweeping every pass would be wasted work; the retry delay is
                # measured in minutes and a pass in seconds.
                reclaim=passes % RECLAIM_EVERY_PASSES == 0,
            )
            if report.processed or (report.reclaimed and report.reclaimed.total):
                log.info("inbound pass complete: %s", report)
        except Exception:
            # One bad pass must not kill the worker. Nothing was acknowledged, so
            # whatever was in flight stays recoverable.
            log.exception("inbound pass failed")
            await asyncio.sleep(1)
        passes += 1

    log.info("inbound worker stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Process queued inbound messages.")
    parser.add_argument("--once", action="store_true", help="Drain what is waiting, then exit.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)

    async def _run() -> None:
        try:
            if args.once:
                report = await run_once(settings, batch_size=args.batch_size, block_ms=1_000)
                log.info("inbound pass complete: %s", report)
            else:
                await run_forever(settings)
        finally:
            await close_redis()
            await dispose_engine()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
