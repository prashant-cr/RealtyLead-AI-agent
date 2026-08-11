"""Declarative base + shared column mixins."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.models.types import UtcDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


# Explicit naming convention so Alembic autogenerate produces stable constraint names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    # Python-side defaults, not just server_default: both Postgres `now()` and SQLite
    # CURRENT_TIMESTAMP are transaction-scoped, so every row written in one transaction
    # would share a timestamp and `ORDER BY created_at` would be non-deterministic —
    # which silently scrambles message order in a conversation transcript.
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
