"""Redis connection management.

Redis has been in the stack since M1 and unused until M8. It now backs two
things: the durable inbound queue (`app.services.inbound_queue`) and rate
limiting (`app.services.ratelimit`).

Everything here is written against `RedisLike` rather than `redis.asyncio.Redis`
directly. The protocol lists only the commands this project actually issues,
which keeps the test doubles small and honest — a fake that satisfies it cannot
drift into supporting commands the real code never uses. It also means tests do
not need a Redis server or a `fakeredis` dependency.

The pool is process-global and lazily built, mirroring `app.core.db`: the API and
each worker are separate processes, and each wants exactly one pool.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any, Protocol, cast, runtime_checkable

from redis.asyncio import Redis, from_url

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


@runtime_checkable
class RedisLike(Protocol):
    """The subset of Redis this project uses.

    Signatures follow redis-py's shape, but the real client is cast to this in
    `get_redis` rather than matching it structurally — see the comment there.
    Return types are deliberately loose (`Any`) where redis-py returns deeply
    nested tuples; the queue module parses those into real types at its own
    boundary rather than spreading the shape through the codebase.

    Members are declared as plain `def` returning `Awaitable`, not `async def`.
    redis-py types its commands as synchronous methods returning
    `T | Awaitable[T]` so that one class can serve both its sync and async
    clients; an `async def` protocol member returns a `Coroutine` and will not
    match that. An `async def` in a fake still satisfies this, because a
    coroutine is an `Awaitable`.
    """

    def ping(self) -> Awaitable[Any]: ...

    # --- streams (inbound queue) ---
    def xadd(
        self, name: str, fields: Mapping[str, Any], maxlen: int | None = ...
    ) -> Awaitable[Any]: ...

    def xgroup_create(
        self, name: str, groupname: str, id: str = ..., mkstream: bool = ...
    ) -> Awaitable[Any]: ...

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: Mapping[str, str],
        count: int | None = ...,
        block: int | None = ...,
    ) -> Awaitable[Any]: ...

    def xack(self, name: str, groupname: str, *ids: str) -> Awaitable[Any]: ...

    def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str = ...,
        count: int | None = ...,
    ) -> Awaitable[Any]: ...

    def xlen(self, name: str) -> Awaitable[Any]: ...

    def xpending(self, name: str, groupname: str) -> Awaitable[Any]: ...

    # --- counters (rate limiting) ---
    def incr(self, name: str) -> Awaitable[Any]: ...

    def expire(self, name: str, time: int) -> Awaitable[Any]: ...

    def ttl(self, name: str) -> Awaitable[Any]: ...

    def delete(self, *names: str) -> Awaitable[Any]: ...

    def aclose(self) -> Awaitable[Any]: ...


_client: RedisLike | None = None

# How long a socket read may take before redis-py gives up.
#
# This exists because of a sharp edge in blocking reads. `XREADGROUP ... BLOCK n`
# holds the socket open for up to `n` milliseconds, and redis-py applies its own
# read timeout on top. Without an explicit `socket_timeout` the two collide:
# `BLOCK 5000` raises `TimeoutError: Timeout reading from redis:6379` on every
# single call, which looks exactly like Redis being unreachable and is not — it
# was found by running the worker, not by any test. Setting this well above the
# longest block we issue keeps the server's timeout the one that fires.
SOCKET_TIMEOUT_SECONDS = 30

# The longest `BLOCK` any caller may ask for, with headroom under the socket
# timeout. `app.services.inbound_queue.consume` clamps to this.
MAX_BLOCK_MS = (SOCKET_TIMEOUT_SECONDS - 10) * 1000


def get_redis(settings: Settings | None = None) -> RedisLike:
    """The process-wide Redis client, built on first use.

    `decode_responses=True` so stream fields come back as `str`. Everything we
    store is JSON or an integer counter, never binary, and decoding at the client
    keeps the queue and limiter free of `.decode()` noise.
    """
    global _client
    if _client is None:
        settings = settings or get_settings()
        # Cast rather than declare `Redis` here. The real client implements every
        # command in RedisLike, but redis-py types its parameters extremely
        # widely (`KeyT = bytes | str | memoryview`, invariant `dict`s) so a
        # narrow protocol cannot match it structurally. Widening the protocol to
        # suit would make it useless as a description of what we actually call,
        # and would push those unions into every test fake.
        _client = cast(
            RedisLike,
            from_url(
                settings.redis_url,
                decode_responses=True,
                # Must exceed the longest BLOCK we issue — see above.
                socket_timeout=SOCKET_TIMEOUT_SECONDS,
                # A worker blocked on XREADGROUP holds an idle socket for the
                # length of its block; without a keepalive some networks drop it
                # silently.
                socket_keepalive=True,
                health_check_interval=30,
            ),
        )
    return _client


def set_redis(client: RedisLike | None) -> None:
    """Swap the process-wide client. Tests use this to install a fake."""
    global _client
    _client = client


async def close_redis() -> None:
    """Release the pool. Called on API shutdown and when a worker exits."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def redis_available(client: RedisLike | None = None) -> bool:
    """Whether Redis is reachable right now.

    Used by the callers that degrade rather than fail — the webhook falls back to
    in-process handling, and the rate limiter fails open. Both would rather serve
    a lead than be correct about Redis being down.
    """
    try:
        await (client or get_redis()).ping()
    except Exception as exc:  # redis-py raises a wide family of connection errors
        log.warning("redis is unreachable: %s", exc)
        return False
    return True


__all__ = [
    "Redis",
    "RedisLike",
    "close_redis",
    "get_redis",
    "redis_available",
    "set_redis",
]
