"""Test fixtures.

Tests run against an in-memory SQLite database so `pytest` needs no services.
The models deliberately use portable column types (JSON, Uuid, VARCHAR-backed
enums) to keep this true; migrations are still verified against Postgres in
`test_migrations.py`, which skips when no Postgres is reachable.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("ENVIRONMENT", "test")

from app.core.config import Settings, get_settings  # noqa: E402
from app.core.db import get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base  # noqa: E402

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> AsyncIterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        TEST_DATABASE_URL, poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with maker() as s:
        yield s

    await engine.dispose()


@pytest.fixture
async def client_factory(
    session: AsyncSession,
) -> AsyncIterator[Callable[..., Awaitable[AsyncClient]]]:
    """Build API clients, optionally with explicit Settings.

    Settings are injected rather than set through the environment: pydantic reads
    the developer's `.env` too, so an env-var-based test would pass or fail
    depending on what happens to be configured locally.
    """
    async with AsyncExitStack() as stack:

        async def _make(settings: Settings | None = None) -> AsyncClient:
            app = create_app()

            async def _override_session() -> AsyncIterator[AsyncSession]:
                yield session

            app.dependency_overrides[get_session] = _override_session
            if settings is not None:
                app.dependency_overrides[get_settings] = lambda: settings
            return await stack.enter_async_context(
                AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            )

        yield _make


@pytest.fixture
async def client(
    client_factory: Callable[..., Awaitable[AsyncClient]],
) -> AsyncClient:
    return await client_factory()
