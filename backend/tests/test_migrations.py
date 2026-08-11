"""Guards against migrations drifting away from the models.

Runs against Postgres when ``TEST_POSTGRES_URL`` points at a reachable server
(``docker compose up -d postgres``), otherwise against a temporary SQLite file so
the check still runs in a bare checkout — the schema is dialect-neutral by design.
"""

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POSTGRES_URL = "postgresql+asyncpg://realtylead:realtylead@localhost:5432/realtylead"


def _alembic_config(url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _with_connection(url: str, fn: object) -> object:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(fn)  # type: ignore[arg-type]
    finally:
        await engine.dispose()


async def _reset_schema(url: str) -> None:
    """Drop this app's tables only — never the whole schema, which we may not own."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        await engine.dispose()


def _reachable_postgres_url() -> str | None:
    url = os.getenv("TEST_POSTGRES_URL", DEFAULT_POSTGRES_URL)
    try:
        asyncio.run(_with_connection(url, lambda conn: conn.execute(text("SELECT 1"))))
    except Exception:
        return None
    return url


@pytest.fixture
def migration_url(tmp_path: Path) -> str:
    """Postgres if one is running, otherwise a throwaway SQLite file."""
    url = _reachable_postgres_url()
    if url is None:
        return f"sqlite+aiosqlite:///{tmp_path / 'migrations.db'}"

    asyncio.run(_reset_schema(url))
    return url


def _detect_drift(conn: Connection) -> list[object]:
    context = MigrationContext.configure(
        conn, opts={"compare_type": True, "compare_server_default": False}
    )
    return list(compare_metadata(context, Base.metadata))


def test_migrations_match_models(migration_url: str) -> None:
    command.upgrade(_alembic_config(migration_url), "head")

    drift = asyncio.run(_with_connection(migration_url, _detect_drift))

    assert drift == [], f"models and migrations have drifted: {drift}"


def _remaining_tables(conn: Connection) -> list[str]:
    from sqlalchemy import inspect

    return [t for t in inspect(conn).get_table_names() if t != "alembic_version"]


def test_downgrade_to_base_drops_everything(migration_url: str) -> None:
    config = _alembic_config(migration_url)
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    assert asyncio.run(_with_connection(migration_url, _remaining_tables)) == []
