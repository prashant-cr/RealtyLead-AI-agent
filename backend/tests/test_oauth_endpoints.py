import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.services import oauth_state
from app.services.google_calendar import TOKEN_CACHE
from tests.factories import make_agent

STATE_SECRET = "state-secret"
ClientFactory = Callable[..., Awaitable[AsyncClient]]


def google_settings(**overrides: object) -> Settings:
    return Settings(
        **{
            "google_client_id": "client-id",
            "google_client_secret": "client-secret",
            "google_oauth_redirect_uri": "https://app.example/auth/google/callback",
            "oauth_state_secret": STATE_SECRET,
            "http_max_retries": 0,
            "http_backoff_base_seconds": 0.0,
            **overrides,
        }  # type: ignore[arg-type]
    )


@pytest.fixture
async def oauth_client(client_factory: ClientFactory) -> AsyncClient:
    return await client_factory(google_settings())


async def seed_agent(session: AsyncSession, **overrides: object):
    agent = make_agent(**overrides)
    session.add(agent)
    await session.flush()
    TOKEN_CACHE.drop(agent.id)
    return agent


# ------------------------------------------------------------------- /start


async def test_start_redirects_to_google_with_a_signed_state(
    oauth_client: AsyncClient, session: AsyncSession
) -> None:
    agent = await seed_agent(session)

    response = await oauth_client.get("/auth/google/start", params={"agent_id": str(agent.id)})

    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    query = parse_qs(urlparse(location).query)
    assert query["access_type"] == ["offline"]
    assert oauth_state.verify(query["state"][0], STATE_SECRET, 600) == agent.id


async def test_start_for_an_unknown_agent_is_404(oauth_client: AsyncClient) -> None:
    response = await oauth_client.get("/auth/google/start", params={"agent_id": str(uuid.uuid4())})

    assert response.status_code == 404


async def test_start_without_a_state_secret_is_unavailable(
    client_factory: ClientFactory, session: AsyncSession
) -> None:
    agent = await seed_agent(session)
    unconfigured = await client_factory(google_settings(oauth_state_secret=None))

    response = await unconfigured.get("/auth/google/start", params={"agent_id": str(agent.id)})

    assert response.status_code == 503


# ---------------------------------------------------------------- /callback


async def test_callback_stores_the_refresh_token(
    client_factory: ClientFactory, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await seed_agent(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "a", "refresh_token": "refresh-xyz", "expires_in": 3600}
        )

    _patch_google_transport(monkeypatch, handler)
    client = await client_factory(google_settings())
    state = oauth_state.issue(agent.id, STATE_SECRET)

    response = await client.get(
        "/auth/google/callback", params={"code": "auth-code", "state": state}
    )

    assert response.status_code == 200
    assert "connected" in response.text.lower()
    await session.refresh(agent)
    assert agent.google_refresh_token == "refresh-xyz"
    assert agent.google_calendar_id == "primary"


async def test_callback_rejects_a_forged_state(
    oauth_client: AsyncClient, session: AsyncSession
) -> None:
    """Otherwise anyone could attach their calendar to someone else's agent."""
    await seed_agent(session)
    forged = oauth_state.issue(uuid.uuid4(), "not-our-secret")

    response = await oauth_client.get(
        "/auth/google/callback", params={"code": "auth-code", "state": forged}
    )

    assert response.status_code == 400


async def test_callback_without_code_or_state_is_a_client_error(
    oauth_client: AsyncClient,
) -> None:
    response = await oauth_client.get("/auth/google/callback", params={"code": "only-code"})

    assert response.status_code == 400


async def test_declined_consent_renders_a_friendly_page(oauth_client: AsyncClient) -> None:
    response = await oauth_client.get("/auth/google/callback", params={"error": "access_denied"})

    assert response.status_code == 200
    assert "declined" in response.text.lower()


async def test_callback_reports_a_failed_token_exchange(
    client_factory: ClientFactory, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = await seed_agent(session)
    _patch_google_transport(
        monkeypatch,
        lambda request: httpx.Response(400, json={"error": "invalid_grant"}),
    )
    client = await client_factory(google_settings())
    state = oauth_state.issue(agent.id, STATE_SECRET)

    response = await client.get(
        "/auth/google/callback", params={"code": "stale-code", "state": state}
    )

    assert response.status_code == 502
    await session.refresh(agent)
    assert agent.google_refresh_token is None


def _patch_google_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Make GoogleCalendarClient build clients against a mock transport."""
    import app.api.oauth as oauth_module
    from app.services.google_calendar import GoogleCalendarClient

    def _factory(settings: Settings) -> GoogleCalendarClient:
        return GoogleCalendarClient(
            settings,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(oauth_module, "GoogleCalendarClient", _factory)
