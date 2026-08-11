"""Issuing and resolving dashboard login sessions."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import Agent, AgentSession

log = get_logger(__name__)

SESSION_PREFIX = "rls_"
SESSION_BYTES = 32
DEFAULT_TTL = timedelta(days=14)
# Only bump `last_used_at` occasionally — a write on every request is pointless load.
TOUCH_INTERVAL = timedelta(minutes=15)


def generate_session_token() -> str:
    return f"{SESSION_PREFIX}{secrets.token_urlsafe(SESSION_BYTES)}"


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(
    session: AsyncSession,
    agent: Agent,
    *,
    user_agent: str | None = None,
    ttl: timedelta = DEFAULT_TTL,
    now: datetime | None = None,
) -> tuple[AgentSession, str]:
    """Return the persisted session and the raw token (shown to the caller once)."""
    now = now or datetime.now(UTC)
    token = generate_session_token()
    record = AgentSession(
        agent_id=agent.id,
        token_hash=hash_session_token(token),
        expires_at=now + ttl,
        user_agent=(user_agent or "")[:255] or None,
    )
    session.add(record)
    await session.flush()
    log.info("issued dashboard session %s for agent %s", record.id, agent.id)
    return record, token


async def resolve_session(
    session: AsyncSession, token: str, now: datetime | None = None
) -> AgentSession | None:
    """Look up a live session, refreshing `last_used_at` at most every 15 minutes."""
    now = now or datetime.now(UTC)
    record = (
        await session.execute(
            select(AgentSession).where(AgentSession.token_hash == hash_session_token(token))
        )
    ).scalar_one_or_none()

    if record is None or not record.is_valid(now):
        return None

    if record.last_used_at is None or now - record.last_used_at > TOUCH_INTERVAL:
        record.last_used_at = now

    return record


async def revoke(session: AsyncSession, record: AgentSession, now: datetime | None = None) -> None:
    record.revoked_at = now or datetime.now(UTC)
    await session.flush()


async def revoke_all(
    session: AsyncSession, agent_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Sign out everywhere — used after a password change."""
    now = now or datetime.now(UTC)
    result = await session.execute(
        update(AgentSession)
        .where(AgentSession.agent_id == agent_id, AgentSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


async def purge_expired(session: AsyncSession, now: datetime | None = None) -> int:
    """Housekeeping for the worker; expired rows are already unusable."""
    now = now or datetime.now(UTC)
    result = await session.execute(
        update(AgentSession)
        .where(AgentSession.expires_at < now, AgentSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return int(getattr(result, "rowcount", 0) or 0)
