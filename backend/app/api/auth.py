"""Bearer-token authentication for the dashboard API.

These endpoints expose lead phone numbers, budgets and full conversation
transcripts, so every one of them is authenticated and scoped to the agent the
token belongs to. An agent can only ever see their own leads — the scoping is
applied in the query, not checked afterwards, so there is no path that returns
another agent's data and then filters it.

Tokens are issued out of band (`python -m app.scripts.issue_token`) and stored as
a SHA-256 hash. They are high-entropy random strings rather than user-chosen
passwords, so there is no dictionary to attack and a fast hash is the right
choice. M7 replaces this with real agent accounts.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.logging import get_logger
from app.models import Agent

log = get_logger(__name__)

TOKEN_BYTES = 32
TOKEN_PREFIX = "rl_"

bearer_scheme = HTTPBearer(auto_error=False, description="Dashboard API token")


def generate_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def current_agent(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Agent:
    """Resolve the agent behind the bearer token, or reject the request."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    agent = (
        await session.execute(
            select(Agent).where(
                Agent.api_token_hash == hash_token(credentials.credentials),
                Agent.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()

    if agent is None:
        # Deliberately vague: an invalid token and a deactivated agent look the same.
        log.warning("rejected dashboard request with an invalid token from %s", request.client)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return agent


CurrentAgent = Annotated[Agent, Depends(current_agent)]
