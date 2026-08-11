"""Authentication for the dashboard API.

These endpoints expose lead phone numbers, budgets and full conversation
transcripts, so every one of them is authenticated and scoped to the agent the
credential belongs to. An agent can only ever see their own leads — the scoping
is applied in the query, not checked afterwards, so there is no path that returns
another agent's data and then filters it.

Two credential types, both presented as `Authorization: Bearer …`:

* **Session tokens** (`rls_…`) — issued by signing in, expire after 14 days, and
  can be revoked individually. This is what the dashboard uses.
* **API tokens** (`rl_…`) — long-lived, one per agent, issued with
  `make token`. For scripts and CI, where a login flow makes no sense.

Both are stored only as a SHA-256 hash. They are high-entropy random strings
rather than user-chosen passwords, so there is no dictionary to attack and a fast
hash is the right choice — see `app/services/passwords.py` for the contrast.
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
from app.models import Agent, AgentSession
from app.services.sessions import SESSION_PREFIX, resolve_session

log = get_logger(__name__)

TOKEN_BYTES = 32
TOKEN_PREFIX = "rl_"

bearer_scheme = HTTPBearer(auto_error=False, description="Session or API token")


def generate_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def current_agent(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Agent:
    """Resolve the agent behind the credential, or reject the request."""
    if credentials is None or not credentials.credentials:
        raise _unauthorized("missing bearer token")

    token = credentials.credentials
    agent: Agent | None = None

    if token.startswith(SESSION_PREFIX):
        record = await resolve_session(session, token)
        if record is not None:
            agent = await session.get(Agent, record.agent_id)
    else:
        agent = (
            await session.execute(
                select(Agent).where(
                    Agent.api_token_hash == hash_token(token), Agent.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()

    if agent is None or not agent.is_active:
        # Deliberately vague: an invalid token, an expired session and a
        # deactivated agent are indistinguishable to the caller.
        log.warning("rejected dashboard request with an invalid credential")
        raise _unauthorized("invalid or expired credentials")

    return agent


async def current_session(
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AgentSession | None:
    """The session record behind this request, when a session token was used."""
    if credentials is None or not credentials.credentials.startswith(SESSION_PREFIX):
        return None
    return await resolve_session(session, credentials.credentials)


CurrentAgent = Annotated[Agent, Depends(current_agent)]
CurrentSession = Annotated["AgentSession | None", Depends(current_session)]
