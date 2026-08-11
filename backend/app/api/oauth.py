"""Google Calendar connect flow for an agent.

Two endpoints: one to start the consent flow, one for Google to redirect back to.
The agent id travels in a signed `state` so the callback cannot be used to attach
someone else's calendar to an agent record.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.logging import get_logger, mask_email
from app.models import Agent
from app.services import oauth_state
from app.services.google_calendar import (
    TOKEN_CACHE,
    GoogleCalendarClient,
    GoogleCalendarError,
    GoogleNotConnectedError,
    authorization_url,
)

router = APIRouter(prefix="/auth/google", tags=["oauth"])
log = get_logger(__name__)

DEFAULT_CALENDAR_ID = "primary"


def _require_state_secret(settings: Settings) -> str:
    secret = settings.oauth_state_secret
    if not secret:
        log.error("OAUTH_STATE_SECRET is not set; refusing to run the OAuth flow")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Google Calendar OAuth is not configured"
        )
    return secret


@router.get("/start")
async def start(
    agent_id: Annotated[uuid.UUID, Query(description="Agent connecting their calendar")],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    secret = _require_state_secret(settings)

    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")

    try:
        url = authorization_url(settings, oauth_state.issue(agent.id, secret))
    except GoogleNotConnectedError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    log.info("starting Google Calendar connect for agent %s", agent.id)
    return RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    secret = _require_state_secret(settings)

    if error:
        # The agent hit "Cancel" on Google's consent screen.
        log.info("agent declined Google Calendar access: %s", error)
        return _page("Calendar not connected", "You declined access. You can try again any time.")

    if not code or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing code or state")

    try:
        agent_id = oauth_state.verify(state, secret, settings.oauth_state_ttl_seconds)
    except oauth_state.InvalidStateError as exc:
        log.warning("rejected Google callback: %s", exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired state") from exc

    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")

    client = GoogleCalendarClient(settings)
    try:
        tokens = await client.exchange_code(code)
    except GoogleCalendarError as exc:
        log.error("Google token exchange failed for agent %s: %s", agent.id, exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "could not complete Google sign-in"
        ) from exc
    finally:
        await client.close()

    agent.google_refresh_token = tokens.refresh_token
    if not agent.google_calendar_id:
        agent.google_calendar_id = DEFAULT_CALENDAR_ID
    # A reconnect invalidates any cached access token from the previous grant.
    TOKEN_CACHE.drop(agent.id)
    await session.flush()

    log.info("connected Google Calendar for agent %s (%s)", agent.id, mask_email(agent.email))
    return _page(
        "Calendar connected",
        f"{agent.name}'s Google Calendar is connected. Bookings will now appear there "
        "automatically, and busy times will be respected when offering slots.",
    )


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto;
         padding: 0 1.5rem; line-height: 1.6; color: #1c1917; }}
  h1 {{ font-size: 1.3rem; }}
  p {{ color: #44403c; }}
</style></head>
<body><h1>{title}</h1><p>{body}</p></body></html>"""
    )
