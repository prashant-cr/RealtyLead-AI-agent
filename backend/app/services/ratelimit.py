"""Rate limiting on Redis.

Three things needed a cap and did not have one:

* **Inbound model calls per lead.** Every WhatsApp message a lead sends costs an
  Anthropic call. Nothing stopped one sender — bored, buggy or hostile — from
  driving that in a loop, and the bill lands on the agent.
* **Outbound nudges per agent.** A brokerage importing 500 stale leads would
  have sent 500 template messages in a single worker pass, which is both a large
  spend and a fast route to a WhatsApp quality-rating problem.
* **Login and signup attempts.** Password guessing was bounded only by scrypt's
  cost, which is a CPU cost we pay, not one the attacker does.

Fixed windows rather than token buckets or sliding logs. A fixed window is one
`INCR` on the hot path and is trivially explainable to an agent asking why a
message was held — a sliding log needs a sorted set and a range delete per call,
and a token bucket needs either Lua or a read-modify-write race. The known cost
is burstiness at the boundary: a lead can spend a full window's budget at the end
of one window and again at the start of the next. For "stop runaway spend" that
is entirely acceptable; it would not be if these were billing quotas.

**These limiters fail open.** If Redis is unreachable, every check is allowed and
logs a warning. A Redis outage should not stop a real buyer from reaching an
agent, and it must not lock every agent out of the dashboard. The trade is
explicit: during a Redis outage there is no rate limiting at all.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisLike, get_redis

log = get_logger(__name__)

KEY_PREFIX = "realtylead:rl"


@dataclass(frozen=True)
class Limit:
    """A budget of `max_events` per `window_seconds`, per key."""

    name: str
    max_events: int
    window_seconds: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: Limit
    remaining: int
    retry_after_seconds: int

    def __bool__(self) -> bool:
        return self.allowed


def _key(limit: Limit, subject: str) -> str:
    """Namespaced, and hashed so PII never becomes a Redis key.

    Subjects include phone numbers and email addresses. Redis keys turn up in
    `MONITOR`, in slow logs and in any metrics exporter, none of which should
    carry a lead's phone number.
    """
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]
    return f"{KEY_PREFIX}:{limit.name}:{digest}"


async def check(
    limit: Limit,
    subject: str,
    *,
    client: RedisLike | None = None,
) -> Decision:
    """Count one event against `limit` for `subject` and say whether it may proceed.

    Consumes budget whether or not the caller goes on to act, so a rejected
    request cannot be retried for free.
    """
    redis = client or get_redis()
    key = _key(limit, subject)

    try:
        count = int(await redis.incr(key))

        if count == 1:
            # First event in this window — start the clock.
            await redis.expire(key, limit.window_seconds)
            return Decision(True, limit, limit.max_events - 1, 0)

        if count <= limit.max_events:
            return Decision(True, limit, limit.max_events - count, 0)

        ttl = int(await redis.ttl(key))
        if ttl < 0:
            # No expiry: something died between the INCR and the EXPIRE above.
            # Without this the key would never clear and the subject would be
            # blocked permanently.
            await redis.expire(key, limit.window_seconds)
            ttl = limit.window_seconds

        return Decision(False, limit, 0, ttl)

    except Exception as exc:
        # See the module docstring: availability beats enforcement here.
        log.warning("rate limit %s unavailable, allowing: %s", limit.name, exc)
        return Decision(True, limit, limit.max_events, 0)


async def reset(limit: Limit, subject: str, *, client: RedisLike | None = None) -> None:
    """Clear a subject's budget. Used after a successful login, so that a user
    who was merely forgetful is not left throttled by their own typos."""
    redis = client or get_redis()
    try:
        await redis.delete(_key(limit, subject))
    except Exception as exc:
        log.warning("could not reset rate limit %s: %s", limit.name, exc)


def peek_key(limit: Limit, subject: str) -> str:
    """The Redis key a subject maps to. Exposed for tests and for operators
    debugging a throttle without having to re-derive the hash."""
    return _key(limit, subject)


# --- the concrete limits, built from settings ---
#
# Defined as functions rather than constants so the values stay configurable per
# deployment; a brokerage running a campaign has different needs from a solo
# agent, and neither should have to edit code.


def inbound_limit(settings: Settings) -> Limit:
    """Model calls triggered by one lead's inbound messages."""
    return Limit(
        name="inbound",
        max_events=settings.inbound_messages_per_lead,
        window_seconds=settings.inbound_window_seconds,
    )


def follow_up_limit(settings: Settings) -> Limit:
    """Template nudges sent on behalf of one agent."""
    return Limit(
        name="followup",
        max_events=settings.follow_ups_per_agent,
        window_seconds=settings.follow_up_window_seconds,
    )


def login_limit(settings: Settings) -> Limit:
    """Failed authentication attempts, per email and per client address."""
    return Limit(
        name="login",
        max_events=settings.login_attempts_per_window,
        window_seconds=settings.login_window_seconds,
    )
