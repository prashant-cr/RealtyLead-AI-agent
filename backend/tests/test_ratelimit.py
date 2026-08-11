"""Rate limiting behaviour, including what happens when Redis is not there."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.ratelimit import (
    Limit,
    check,
    follow_up_limit,
    inbound_limit,
    login_limit,
    peek_key,
    reset,
)
from tests.fakes import FakeRedis

SMALL = Limit(name="test", max_events=3, window_seconds=60)


async def test_allows_up_to_the_limit_then_rejects(fake_redis: FakeRedis) -> None:
    for expected_remaining in (2, 1, 0):
        decision = await check(SMALL, "someone")
        assert decision.allowed
        assert decision.remaining == expected_remaining

    blocked = await check(SMALL, "someone")
    assert not blocked.allowed
    assert blocked.retry_after_seconds == 60


async def test_subjects_have_separate_budgets(fake_redis: FakeRedis) -> None:
    for _ in range(4):
        await check(SMALL, "noisy")

    assert (await check(SMALL, "quiet")).allowed


async def test_budget_returns_when_the_window_expires(fake_redis: FakeRedis) -> None:
    for _ in range(4):
        await check(SMALL, "someone")
    assert not (await check(SMALL, "someone")).allowed

    fake_redis.advance(61)

    assert (await check(SMALL, "someone")).allowed


async def test_reset_clears_a_subject(fake_redis: FakeRedis) -> None:
    for _ in range(4):
        await check(SMALL, "someone")
    assert not (await check(SMALL, "someone")).allowed

    await reset(SMALL, "someone")

    assert (await check(SMALL, "someone")).allowed


async def test_a_rejected_attempt_still_costs_budget(fake_redis: FakeRedis) -> None:
    """Otherwise a caller could retry indefinitely at no cost once blocked."""
    for _ in range(3):
        await check(SMALL, "someone")

    first_rejection = await check(SMALL, "someone")
    await check(SMALL, "someone")

    assert not first_rejection.allowed
    assert fake_redis.counters[peek_key(SMALL, "someone")] == 5


async def test_fails_open_when_redis_is_down(fake_redis: FakeRedis) -> None:
    """A Redis outage must not stop leads reaching agents, or lock agents out of
    the dashboard. Documented trade-off: no limiting at all while it is down."""
    fake_redis.fail_with = ConnectionError("redis is gone")

    decision = await check(SMALL, "someone")

    assert decision.allowed
    assert decision.remaining == SMALL.max_events


async def test_an_orphaned_key_recovers_its_expiry(fake_redis: FakeRedis) -> None:
    """A crash between INCR and EXPIRE would otherwise leave a key with no TTL,
    blocking that subject permanently."""
    key = peek_key(SMALL, "someone")
    fake_redis.counters[key] = 99  # incremented, never given an expiry

    decision = await check(SMALL, "someone")

    assert not decision.allowed
    assert decision.retry_after_seconds == SMALL.window_seconds
    assert key in fake_redis.expiries


async def test_keys_do_not_contain_the_subject(fake_redis: FakeRedis) -> None:
    """Subjects are phone numbers and email addresses; Redis keys show up in
    MONITOR, slow logs and metrics exporters."""
    key = peek_key(SMALL, "+919000000777")

    assert "+919000000777" not in key
    assert "9000000777" not in key


@pytest.mark.parametrize(
    ("factory", "expected_name"),
    [(inbound_limit, "inbound"), (follow_up_limit, "followup"), (login_limit, "login")],
)
def test_limits_are_built_from_settings(factory: object, expected_name: str) -> None:
    settings = Settings(
        inbound_messages_per_lead=7,
        follow_ups_per_agent=8,
        login_attempts_per_window=9,
    )

    limit = factory(settings)  # type: ignore[operator]

    assert limit.name == expected_name
    assert limit.max_events in (7, 8, 9)
