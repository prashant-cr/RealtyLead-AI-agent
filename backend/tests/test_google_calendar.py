import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.services.google_calendar import (
    TOKEN_CACHE,
    GoogleAuthRevokedError,
    GoogleCalendarClient,
    GoogleCalendarError,
    GoogleNotConnectedError,
    authorization_url,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
AGENT_ID = uuid.uuid4()
REFRESH = "refresh-token-123"


def settings(**overrides: object) -> Settings:
    return Settings(
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_oauth_redirect_uri="https://app.example/auth/google/callback",
        http_max_retries=2,
        http_backoff_base_seconds=0.0,
        **overrides,  # type: ignore[arg-type]
    )


def calendar(handler: object, **overrides: object) -> GoogleCalendarClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return GoogleCalendarClient(settings(**overrides), client=client)


@pytest.fixture(autouse=True)
def _clear_token_cache() -> None:
    TOKEN_CACHE.drop(AGENT_ID)


def token_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "access-abc", "expires_in": 3600})


# ------------------------------------------------------------- authorize URL


def test_authorization_url_requests_offline_access() -> None:
    url = authorization_url(settings(), state="signed-state")

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    # Without both of these Google returns no refresh token and the connection
    # silently dies an hour later.
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=signed-state" in url
    assert "calendar.events" in url


def test_authorization_url_without_a_client_id_fails_loudly() -> None:
    with pytest.raises(GoogleNotConnectedError):
        authorization_url(Settings(google_client_id=None), state="s")


# -------------------------------------------------------------- code exchange


async def test_code_exchange_returns_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
        )

    tokens = await calendar(handler).exchange_code("auth-code")

    assert tokens.refresh_token == "r"
    assert tokens.access_token == "a"
    assert tokens.expires_at > datetime.now(UTC)


async def test_code_exchange_without_a_refresh_token_is_an_error() -> None:
    """A grant with no refresh token would work for an hour and then break."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})

    with pytest.raises(GoogleCalendarError, match="refresh token"):
        await calendar(handler).exchange_code("auth-code")


# ------------------------------------------------------------- access tokens


async def test_access_token_is_cached_between_calls() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return token_response(request)

    client = calendar(handler)
    first = await client.access_token_for(AGENT_ID, REFRESH, now=NOW)
    second = await client.access_token_for(AGENT_ID, REFRESH, now=NOW + timedelta(minutes=5))

    assert first == second == "access-abc"
    assert calls["n"] == 1


async def test_expired_access_token_is_refreshed() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return token_response(request)

    client = calendar(handler)
    await client.access_token_for(AGENT_ID, REFRESH, now=NOW)
    await client.access_token_for(AGENT_ID, REFRESH, now=NOW + timedelta(hours=2))

    assert calls["n"] == 2


async def test_missing_refresh_token_raises_not_connected() -> None:
    with pytest.raises(GoogleNotConnectedError):
        await calendar(token_response).access_token_for(AGENT_ID, None, now=NOW)


async def test_revoked_refresh_token_is_reported_distinctly() -> None:
    """Callers need to tell "reconnect your calendar" apart from "Google is down"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(GoogleAuthRevokedError):
        await calendar(handler).access_token_for(AGENT_ID, REFRESH, now=NOW)


# ------------------------------------------------------------------ free/busy


async def test_free_busy_returns_intervals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(request)
        return httpx.Response(
            200,
            json={
                "calendars": {
                    "primary": {
                        "busy": [
                            {"start": "2026-08-12T09:00:00Z", "end": "2026-08-12T10:00:00Z"},
                            {"start": "2026-08-12T14:00:00Z", "end": "2026-08-12T15:30:00Z"},
                        ]
                    }
                }
            },
        )

    busy = await calendar(handler).free_busy(
        AGENT_ID, REFRESH, "primary", NOW, NOW + timedelta(days=1), now=NOW
    )

    assert len(busy) == 2
    assert busy[0][0] == datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
    assert busy[1][1] == datetime(2026, 8, 12, 15, 30, tzinfo=UTC)


async def test_free_busy_surfaces_calendar_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(request)
        return httpx.Response(
            200, json={"calendars": {"primary": {"errors": [{"reason": "notFound"}]}}}
        )

    with pytest.raises(GoogleCalendarError, match="notFound"):
        await calendar(handler).free_busy(
            AGENT_ID, REFRESH, "primary", NOW, NOW + timedelta(days=1), now=NOW
        )


async def test_unparseable_busy_interval_is_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(request)
        return httpx.Response(
            200,
            json={
                "calendars": {
                    "primary": {
                        "busy": [
                            {"start": "not-a-date", "end": "also-not"},
                            {"start": "2026-08-12T09:00:00Z", "end": "2026-08-12T10:00:00Z"},
                        ]
                    }
                }
            },
        )

    busy = await calendar(handler).free_busy(
        AGENT_ID, REFRESH, "primary", NOW, NOW + timedelta(days=1), now=NOW
    )

    assert len(busy) == 1


# ---------------------------------------------------------------- create event


async def test_create_event_sends_the_expected_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(request)
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200, json={"id": "evt_123", "htmlLink": "https://calendar.google.com/evt_123"}
        )

    event = await calendar(handler).create_event(
        AGENT_ID,
        REFRESH,
        "primary",
        summary="Site visit: Priya Shah",
        description="Budget: INR 90,00,000",
        start=NOW,
        end=NOW + timedelta(hours=1),
        timezone_name="Asia/Kolkata",
        attendee_email="priya@example.com",
        location="Bopal, Ahmedabad",
        now=NOW,
    )

    assert event.event_id == "evt_123"
    body = captured["body"]
    assert body["summary"] == "Site visit: Priya Shah"  # type: ignore[index]
    assert body["start"]["timeZone"] == "Asia/Kolkata"  # type: ignore[index]
    assert body["attendees"] == [{"email": "priya@example.com"}]  # type: ignore[index]
    assert body["location"] == "Bopal, Ahmedabad"  # type: ignore[index]
    assert "sendUpdates=all" in str(captured["url"])
    assert captured["auth"] == "Bearer access-abc"


async def test_create_event_without_an_email_does_not_invite() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(request)
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "evt_456"})

    await calendar(handler).create_event(
        AGENT_ID,
        REFRESH,
        "primary",
        summary="Call",
        description="",
        start=NOW,
        end=NOW + timedelta(minutes=30),
        timezone_name="Asia/Kolkata",
        now=NOW,
    )

    assert "attendees" not in captured["body"]  # type: ignore[operator]
    assert "sendUpdates=none" in str(captured["url"])


# ---------------------------------------------------------------- cancel event


async def test_cancel_event_deletes() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(request)
        seen["method"] = request.method
        return httpx.Response(204)

    assert await calendar(handler).cancel_event(AGENT_ID, REFRESH, "primary", "evt_1", now=NOW)
    assert seen["method"] == "DELETE"


async def test_cancelling_a_missing_event_is_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(request)
        return httpx.Response(404, json={"error": {"message": "Not Found"}})

    assert (
        await calendar(handler).cancel_event(AGENT_ID, REFRESH, "primary", "gone", now=NOW)
    ) is False


# ------------------------------------------------------------- retry behaviour


async def test_transient_error_is_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(request)
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"error": {"message": "backend error"}})
        return httpx.Response(200, json={"calendars": {"primary": {"busy": []}}})

    busy = await calendar(handler).free_busy(
        AGENT_ID, REFRESH, "primary", NOW, NOW + timedelta(days=1), now=NOW
    )

    assert attempts["n"] == 3
    assert busy == []


async def test_client_error_is_not_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return token_response(request)
        attempts["n"] += 1
        return httpx.Response(400, json={"error": {"message": "Invalid timeMin"}})

    with pytest.raises(GoogleCalendarError, match="Invalid timeMin"):
        await calendar(handler).free_busy(
            AGENT_ID, REFRESH, "primary", NOW, NOW + timedelta(days=1), now=NOW
        )

    assert attempts["n"] == 1
