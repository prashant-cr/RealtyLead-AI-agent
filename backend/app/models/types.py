"""Reusable column types."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Dialect
from sqlalchemy import Enum as SAEnum
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware DateTime that always hands back UTC.

    Postgres returns aware datetimes; SQLite drops the offset and returns naive
    ones. Without this, the same code compares fine against Postgres and raises
    "can't compare offset-naive and offset-aware datetimes" against SQLite — and
    a naive datetime written by mistake would be silently read back as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime written to a timestamptz column; pass tz-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        moment: datetime = value
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def enum_column(enum_cls: type[StrEnum], *, length: int = 32) -> SAEnum:
    """Store a StrEnum by *value* in a VARCHAR + CHECK constraint (portable, easy to extend)."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda e: [member.value for member in e],
        name=f"{enum_cls.__name__.lower()}_enum",
        validate_strings=True,
    )
