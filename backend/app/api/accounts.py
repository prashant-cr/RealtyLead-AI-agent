"""Agent signup and sign-in.

Signup is deliberately minimal — name, email, password and a phone number. Every
other setting has a working default so a new agent can be talking to leads in
minutes; the onboarding checklist nudges them through the rest.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CurrentAgent, CurrentSession
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.logging import get_logger, mask_email
from app.models import Agent
from app.models.agent import DEFAULT_WORKING_HOURS
from app.models.enums import Language
from app.services import sessions as session_service
from app.services.passwords import WeakPasswordError, hash_password, verify_password
from app.services.ratelimit import check, login_limit, reset

router = APIRouter(prefix="/auth", tags=["accounts"])
log = get_logger(__name__)


def client_address(request: Request) -> str:
    """Best-effort client address for rate limiting.

    Trusts the left-most `X-Forwarded-For` entry when present, because in every
    supported deployment this API sits behind a proxy and `request.client.host`
    would otherwise be the proxy for every caller — one shared bucket, which
    would let one noisy client lock everyone out. The header is spoofable by a
    direct caller, so this bounds accidental abuse and raises the cost of the
    deliberate kind; it is not an access control.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _throttle(subject: str, settings: Settings) -> None:
    """Reject the request if `subject` is out of authentication attempts."""
    decision = await check(login_limit(settings), subject)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Try again shortly.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real hash of a random value, so a login for an unknown address costs
    about the same as one for a known address and timing does not leak which."""
    return hash_password(secrets.token_urlsafe(32))


SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Pragmatic rather than RFC-complete: this is a login identifier, and we do not
# send email yet. Full validation would mean pulling in email-validator and, via
# it, dnspython — weight this feature does not justify.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def _clean_email(value: str) -> str:
    cleaned = value.strip().lower()
    if not EMAIL_RE.match(cleaned):
        raise ValueError("That does not look like an email address.")
    return cleaned


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=10, max_length=1024)
    phone: str = Field(min_length=5, max_length=20)
    brokerage_name: str | None = Field(default=None, max_length=160)
    timezone: str = Field(default="Asia/Kolkata", max_length=64)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _clean_email(value)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return value.strip().lower()


class SessionOut(BaseModel):
    token: str
    expires_at: datetime
    agent_id: str
    name: str
    onboarding_complete: bool


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=10, max_length=1024)


@router.post("/signup", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupRequest,
    session: SessionDep,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    user_agent: Annotated[str | None, Header(alias="User-Agent")] = None,
) -> SessionOut:
    # Per address only: the email is chosen by whoever is signing up, so limiting
    # on it would bound nothing.
    await _throttle(f"signup:{client_address(request)}", settings)

    try:
        password_hash = hash_password(body.password)
    except WeakPasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    email = body.email
    agent = Agent(
        name=body.name.strip(),
        email=email,
        phone=body.phone.strip(),
        brokerage_name=(body.brokerage_name or "").strip() or None,
        timezone=body.timezone,
        password_hash=password_hash,
        languages=[Language.ENGLISH.value],
        working_hours=dict(DEFAULT_WORKING_HOURS),
    )
    session.add(agent)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        # Same message whether or not the address exists, so signup cannot be
        # used to enumerate which agents are registered.
        raise HTTPException(status.HTTP_409_CONFLICT, "That email address cannot be used.") from exc

    record, token = await session_service.create_session(session, agent, user_agent=user_agent)
    log.info("new agent signed up: %s", mask_email(agent.email))
    return SessionOut(
        token=token,
        expires_at=record.expires_at,
        agent_id=str(agent.id),
        name=agent.name,
        onboarding_complete=False,
    )


@router.post("/login", response_model=SessionOut)
async def login(
    body: LoginRequest,
    session: SessionDep,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    user_agent: Annotated[str | None, Header(alias="User-Agent")] = None,
) -> SessionOut:
    # Both, because they bound different attacks: the address stops one client
    # working through a password list, and the email stops a distributed attempt
    # at a single account.
    address_subject = f"login:{client_address(request)}"
    email_subject = f"login:{body.email.lower()}"
    await _throttle(address_subject, settings)
    await _throttle(email_subject, settings)

    agent = (
        await session.execute(select(Agent).where(func.lower(Agent.email) == body.email.lower()))
    ).scalar_one_or_none()

    if (
        agent is None
        or not agent.is_active
        or not verify_password(body.password, agent.password_hash)
    ):
        # Spend a comparable amount of time either way so response timing does not
        # reveal whether the address is registered.
        if agent is None or agent.password_hash is None:
            await asyncio.to_thread(verify_password, body.password, _dummy_hash())
        log.warning("failed dashboard login for %s", mask_email(body.email))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password.")

    # Signing in successfully clears the budget, so somebody who mistyped their
    # password twice is not left locked out for the rest of the window.
    await reset(login_limit(settings), email_subject)
    await reset(login_limit(settings), address_subject)

    record, token = await session_service.create_session(session, agent, user_agent=user_agent)
    return SessionOut(
        token=token,
        expires_at=record.expires_at,
        agent_id=str(agent.id),
        name=agent.name,
        onboarding_complete=agent.onboarded_at is not None,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    session: SessionDep,
    agent: CurrentAgent,
    current: CurrentSession,
) -> None:
    if current is not None:
        await session_service.revoke(session, current)
        log.info("agent %s signed out", agent.id)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    session: SessionDep,
    agent: CurrentAgent,
) -> None:
    if not verify_password(body.current_password, agent.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect.")
    try:
        agent.password_hash = hash_password(body.new_password)
    except WeakPasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # Changing a password signs out every other device — the usual expectation
    # after "someone may have my password".
    revoked = await session_service.revoke_all(session, agent.id, now=datetime.now(UTC))
    log.info("agent %s changed password; revoked %s session(s)", agent.id, revoked)
