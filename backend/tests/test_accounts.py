"""Signup, sign-in, sessions and password handling."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Agent, AgentSession
from app.services import sessions as session_service
from app.services.passwords import (
    WeakPasswordError,
    hash_password,
    needs_rehash,
    verify_password,
)
from tests.factories import make_agent

ClientFactory = Callable[..., Awaitable[AsyncClient]]
PASSWORD = "correct-horse-battery-staple"


def app_settings() -> Settings:
    return Settings(whatsapp_access_token="tok")


@pytest.fixture
async def api(client_factory: ClientFactory) -> AsyncClient:
    return await client_factory(app_settings())


def signup_body(**overrides: object) -> dict[str, object]:
    return {
        "name": "Neha Joshi",
        "email": "neha@bluekey.example",
        "password": PASSWORD,
        "phone": "+919876500010",
        "brokerage_name": "Blue Key Realty",
        **overrides,
    }


# ------------------------------------------------------------ password hashing


def test_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)

    assert first != second  # per-password salt
    assert PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert verify_password(PASSWORD, second)
    assert not verify_password("wrong", first)


def test_verify_is_safe_against_junk() -> None:
    assert not verify_password(PASSWORD, None)
    assert not verify_password(PASSWORD, "")
    assert not verify_password(PASSWORD, "not-a-hash")
    assert not verify_password(PASSWORD, "scrypt$bad$params$x$y$z")


def test_short_passwords_are_refused() -> None:
    with pytest.raises(WeakPasswordError):
        hash_password("short")


def test_absurdly_long_passwords_are_refused() -> None:
    """Bounds the work an unauthenticated caller can force us to do."""
    with pytest.raises(WeakPasswordError):
        hash_password("x" * 2000)


def test_weaker_parameters_are_flagged_for_rehash() -> None:
    weak = hash_password(PASSWORD, n=2**10)

    assert needs_rehash(weak)
    assert not needs_rehash(hash_password(PASSWORD))
    assert needs_rehash(None)


# -------------------------------------------------------------------- signup


async def test_signup_creates_an_agent_and_returns_a_session(
    api: AsyncClient, session: AsyncSession
) -> None:
    response = await api.post("/auth/signup", json=signup_body())

    assert response.status_code == 201
    body = response.json()
    assert body["token"].startswith("rls_")
    assert body["onboarding_complete"] is False

    agent = (
        await session.execute(select(Agent).where(Agent.email == "neha@bluekey.example"))
    ).scalar_one()
    assert agent.name == "Neha Joshi"
    assert agent.working_hours  # sensible defaults, not an empty week
    assert agent.password_hash is not None
    assert PASSWORD not in (agent.password_hash or "")


async def test_signup_normalises_the_email(api: AsyncClient, session: AsyncSession) -> None:
    await api.post("/auth/signup", json=signup_body(email="  NEHA@BlueKey.example "))

    agent = (await session.execute(select(Agent))).scalar_one()
    assert agent.email == "neha@bluekey.example"


async def test_duplicate_signup_does_not_reveal_the_account(api: AsyncClient) -> None:
    await api.post("/auth/signup", json=signup_body())

    response = await api.post("/auth/signup", json=signup_body(name="Impostor"))

    assert response.status_code == 409
    # No "already registered" — that would confirm the address has an account.
    assert "already" not in response.json()["detail"].lower()


async def test_signup_rejects_a_weak_password(api: AsyncClient) -> None:
    response = await api.post("/auth/signup", json=signup_body(password="short"))

    assert response.status_code == 422


@pytest.mark.parametrize("email", ["not-an-email", "no@domain", "@nope.com", "a b@c.com"])
async def test_signup_rejects_malformed_emails(api: AsyncClient, email: str) -> None:
    response = await api.post("/auth/signup", json=signup_body(email=email))

    assert response.status_code == 422


async def test_the_signup_token_works_immediately(api: AsyncClient) -> None:
    token = (await api.post("/auth/signup", json=signup_body())).json()["token"]

    response = await api.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["name"] == "Neha Joshi"


# --------------------------------------------------------------------- login


async def test_login_returns_a_working_session(api: AsyncClient) -> None:
    await api.post("/auth/signup", json=signup_body())

    response = await api.post(
        "/auth/login", json={"email": "neha@bluekey.example", "password": PASSWORD}
    )

    assert response.status_code == 200
    token = response.json()["token"]
    assert (
        await api.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 200


async def test_login_is_case_insensitive_on_email(api: AsyncClient) -> None:
    await api.post("/auth/signup", json=signup_body())

    response = await api.post(
        "/auth/login", json={"email": "NEHA@BLUEKEY.EXAMPLE", "password": PASSWORD}
    )

    assert response.status_code == 200


async def test_wrong_password_is_rejected(api: AsyncClient) -> None:
    await api.post("/auth/signup", json=signup_body())

    response = await api.post(
        "/auth/login", json={"email": "neha@bluekey.example", "password": "not-the-password"}
    )

    assert response.status_code == 401


async def test_unknown_email_gives_the_same_error_as_a_wrong_password(
    api: AsyncClient,
) -> None:
    await api.post("/auth/signup", json=signup_body())

    unknown = await api.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    wrong = await api.post(
        "/auth/login", json={"email": "neha@bluekey.example", "password": "wrong-password"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_deactivated_agent_cannot_log_in(api: AsyncClient, session: AsyncSession) -> None:
    await api.post("/auth/signup", json=signup_body())
    agent = (await session.execute(select(Agent))).scalar_one()
    agent.is_active = False
    await session.flush()

    response = await api.post(
        "/auth/login", json={"email": "neha@bluekey.example", "password": PASSWORD}
    )

    assert response.status_code == 401


async def test_an_agent_without_a_password_cannot_log_in(
    api: AsyncClient, session: AsyncSession
) -> None:
    """Seeded/API-token-only agents have no password; login must not accept a blank."""
    session.add(make_agent(email="seeded@example.com", password_hash=None))
    await session.flush()

    response = await api.post("/auth/login", json={"email": "seeded@example.com", "password": ""})

    assert response.status_code in (401, 422)


# ------------------------------------------------------------------ sessions


async def test_logout_revokes_the_session(api: AsyncClient) -> None:
    token = (await api.post("/auth/signup", json=signup_body())).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert (await api.post("/auth/logout", headers=headers)).status_code == 204

    assert (await api.get("/api/me", headers=headers)).status_code == 401


async def test_expired_sessions_are_rejected(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()
    record, token = await session_service.create_session(session, agent, ttl=timedelta(seconds=1))

    later = datetime.now(UTC) + timedelta(minutes=5)
    assert await session_service.resolve_session(session, token, now=later) is None
    assert record.is_valid(later) is False


async def test_revoked_sessions_are_rejected(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()
    record, token = await session_service.create_session(session, agent)
    await session_service.revoke(session, record)

    assert await session_service.resolve_session(session, token) is None


async def test_changing_the_password_signs_out_other_devices(
    api: AsyncClient, session: AsyncSession
) -> None:
    first = (await api.post("/auth/signup", json=signup_body())).json()["token"]
    second = (
        await api.post("/auth/login", json={"email": "neha@bluekey.example", "password": PASSWORD})
    ).json()["token"]

    response = await api.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
        headers={"Authorization": f"Bearer {first}"},
    )

    assert response.status_code == 204
    # Both sessions are gone — the whole point of "sign out everywhere".
    for token in (first, second):
        got = await api.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert got.status_code == 401

    agent = (await session.execute(select(Agent))).scalar_one()
    assert verify_password("a-brand-new-password", agent.password_hash)


async def test_password_change_needs_the_current_password(api: AsyncClient) -> None:
    token = (await api.post("/auth/signup", json=signup_body())).json()["token"]

    response = await api.post(
        "/auth/password",
        json={"current_password": "guessing", "new_password": "another-good-password"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


async def test_session_tokens_are_stored_only_as_hashes(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()
    _, token = await session_service.create_session(session, agent)

    record = (await session.execute(select(AgentSession))).scalar_one()
    assert record.token_hash != token
    assert len(record.token_hash) == 64


async def test_api_tokens_still_work_alongside_sessions(
    api: AsyncClient, session: AsyncSession
) -> None:
    """Scripts and the CLI use a long-lived token; the dashboard uses sessions."""
    from app.api.auth import generate_token, hash_token

    token = generate_token()
    session.add(make_agent(email="script@example.com", api_token_hash=hash_token(token)))
    await session.flush()

    response = await api.get("/api/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
