"""Google Calendar over the REST API.

Implemented directly on httpx rather than `google-api-python-client`: that
library is synchronous and large, and everything we need is four endpoints. It
also keeps the OAuth flow explicit, which matters because per-agent refresh
tokens are the sensitive part of this feature.

Access tokens are cached in-process and refreshed on demand; only the refresh
token is persisted. A revoked refresh token raises `GoogleAuthRevokedError` so
the caller can clear it and tell the agent to reconnect.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, mask_email

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# Refresh a little early so a token cannot expire mid-request.
TOKEN_EXPIRY_MARGIN = timedelta(seconds=60)


class GoogleCalendarError(RuntimeError):
    """A Calendar API call failed."""


class GoogleAuthRevokedError(GoogleCalendarError):
    """The agent's refresh token is no longer valid — they must reconnect."""


class GoogleNotConnectedError(GoogleCalendarError):
    """The agent has not connected a Google account."""


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    expires_at: datetime
    refresh_token: str | None = None


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    html_link: str | None = None


class _TokenCache:
    """Process-local access tokens, keyed by agent."""

    def __init__(self) -> None:
        self._tokens: dict[uuid.UUID, OAuthTokens] = {}

    def get(self, agent_id: uuid.UUID, now: datetime) -> OAuthTokens | None:
        token = self._tokens.get(agent_id)
        if token is None or token.expires_at - TOKEN_EXPIRY_MARGIN <= now:
            return None
        return token

    def put(self, agent_id: uuid.UUID, token: OAuthTokens) -> None:
        self._tokens[agent_id] = token

    def drop(self, agent_id: uuid.UUID) -> None:
        self._tokens.pop(agent_id, None)


TOKEN_CACHE = _TokenCache()


def authorization_url(settings: Settings, state: str) -> str:
    """Where to send an agent to grant calendar access.

    `access_type=offline` plus `prompt=consent` is what makes Google return a
    refresh token — without both, a re-authorising agent gets an access token
    only and the connection silently stops working an hour later.
    """
    if not settings.google_client_id:
        raise GoogleNotConnectedError("GOOGLE_CLIENT_ID is not set")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": settings.google_oauth_scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{settings.google_oauth_auth_url}?{urlencode(params)}"


class GoogleCalendarClient:
    def __init__(
        self, settings: Settings | None = None, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.http_timeout_seconds)
        )

    # ---------------------------------------------------------------- plumbing

    async def _request(
        self, method: str, url: str, *, token: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        attempts = self._settings.http_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.RequestError as exc:
                last_error = exc
                log.warning(
                    "google %s failed (attempt %s/%s): %s",
                    method,
                    attempt + 1,
                    attempts,
                    type(exc).__name__,
                )
            else:
                if response.status_code < 400:
                    return response
                # The token endpoint reports a revoked grant as 400 `invalid_grant`,
                # not 401 — checking only 401/403 would misreport it as a transient
                # failure and never tell the agent to reconnect.
                if response.status_code in (400, 401, 403) and _is_revoked(response):
                    raise GoogleAuthRevokedError(
                        "Google rejected the stored credentials; the agent must reconnect"
                    )
                if response.status_code not in RETRYABLE_STATUS:
                    raise GoogleCalendarError(
                        f"Google Calendar rejected the request "
                        f"({response.status_code}): {_error_detail(response)}"
                    )
                last_error = GoogleCalendarError(f"transient Google error {response.status_code}")
                log.warning(
                    "google %s returned %s (attempt %s/%s)",
                    method,
                    response.status_code,
                    attempt + 1,
                    attempts,
                )

            if attempt < attempts - 1:
                backoff = self._settings.http_backoff_base_seconds * (2**attempt)
                await asyncio.sleep(backoff + random.uniform(0, backoff / 2))

        raise GoogleCalendarError(
            f"Google Calendar unreachable after {attempts} attempts"
        ) from last_error

    # -------------------------------------------------------------------- oauth

    async def exchange_code(self, code: str) -> OAuthTokens:
        """Trade an authorization code for tokens. Only happens once per agent."""
        if not (self._settings.google_client_id and self._settings.google_client_secret):
            raise GoogleNotConnectedError("Google OAuth client credentials are not configured")

        response = await self._request(
            "POST",
            self._settings.google_oauth_token_url,
            data={
                "code": code,
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "redirect_uri": self._settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        body = response.json()
        if not body.get("refresh_token"):
            raise GoogleCalendarError(
                "Google did not return a refresh token — the agent must revoke access "
                "and reconnect so consent is requested again"
            )
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in", 3600))),
        )

    async def access_token_for(
        self, agent_id: uuid.UUID, refresh_token: str | None, now: datetime | None = None
    ) -> str:
        now = now or datetime.now(UTC)
        if not refresh_token:
            raise GoogleNotConnectedError("agent has not connected a Google account")

        if cached := TOKEN_CACHE.get(agent_id, now):
            return cached.access_token

        response = await self._request(
            "POST",
            self._settings.google_oauth_token_url,
            data={
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        body = response.json()
        token = OAuthTokens(
            access_token=body["access_token"],
            expires_at=now + timedelta(seconds=int(body.get("expires_in", 3600))),
        )
        TOKEN_CACHE.put(agent_id, token)
        return token.access_token

    # ----------------------------------------------------------------- calendar

    async def free_busy(
        self,
        agent_id: uuid.UUID,
        refresh_token: str | None,
        calendar_id: str,
        window_start: datetime,
        window_end: datetime,
        now: datetime | None = None,
    ) -> list[tuple[datetime, datetime]]:
        """Busy intervals on the agent's calendar."""
        token = await self.access_token_for(agent_id, refresh_token, now)
        response = await self._request(
            "POST",
            f"{self._settings.google_api_base}/freeBusy",
            token=token,
            json={
                "timeMin": window_start.astimezone(UTC).isoformat(),
                "timeMax": window_end.astimezone(UTC).isoformat(),
                "items": [{"id": calendar_id}],
            },
        )
        calendars: dict[str, Any] = response.json().get("calendars", {})
        entry: dict[str, Any] = calendars.get(calendar_id) or next(iter(calendars.values()), {})

        if errors := entry.get("errors"):
            raise GoogleCalendarError(f"free/busy lookup failed: {errors[0].get('reason')}")

        intervals: list[tuple[datetime, datetime]] = []
        for slot in entry.get("busy", []):
            try:
                intervals.append((_parse_dt(slot["start"]), _parse_dt(slot["end"])))
            except (KeyError, ValueError):
                log.warning("skipping unparseable busy interval from Google")
        return intervals

    async def create_event(
        self,
        agent_id: uuid.UUID,
        refresh_token: str | None,
        calendar_id: str,
        *,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        timezone_name: str,
        attendee_email: str | None = None,
        location: str | None = None,
        now: datetime | None = None,
    ) -> CalendarEvent:
        token = await self.access_token_for(agent_id, refresh_token, now)
        body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.astimezone(UTC).isoformat(), "timeZone": timezone_name},
            "end": {"dateTime": end.astimezone(UTC).isoformat(), "timeZone": timezone_name},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 60},
                    {"method": "popup", "minutes": 10},
                ],
            },
        }
        if location:
            body["location"] = location
        if attendee_email:
            body["attendees"] = [{"email": attendee_email}]
            log.info("inviting %s to calendar event", mask_email(attendee_email))

        response = await self._request(
            "POST",
            f"{self._settings.google_api_base}/calendars/{calendar_id}/events",
            token=token,
            params={"sendUpdates": "all" if attendee_email else "none"},
            json=body,
        )
        created = response.json()
        return CalendarEvent(event_id=created["id"], html_link=created.get("htmlLink"))

    async def cancel_event(
        self,
        agent_id: uuid.UUID,
        refresh_token: str | None,
        calendar_id: str,
        event_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Delete an event. Returns False if it was already gone."""
        token = await self.access_token_for(agent_id, refresh_token, now)
        try:
            await self._request(
                "DELETE",
                f"{self._settings.google_api_base}/calendars/{calendar_id}/events/{event_id}",
                token=token,
                params={"sendUpdates": "all"},
            )
        except GoogleCalendarError as exc:
            if "404" in str(exc) or "410" in str(exc):
                return False
            raise
        return True

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_dt(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _error_detail(response: httpx.Response) -> str:
    """Google reports errors two ways: `{"error": "invalid_grant"}` from the token
    endpoint, `{"error": {"message": ...}}` from the Calendar API."""
    try:
        error = response.json().get("error")
    except ValueError:
        return response.text[:200]
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        return str(error.get("message", response.text[:200]))
    return response.text[:200]


def _is_revoked(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error")
    if isinstance(error, str):
        return error in {"invalid_grant", "unauthorized_client"}
    reason = (error or {}).get("errors", [{}])[0].get("reason", "")
    return reason in {"authError", "invalidCredentials"}
