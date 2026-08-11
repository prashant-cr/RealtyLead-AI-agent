"""The human realtor using the product."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Language

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.lead import Lead
    from app.models.listing import Listing

DEFAULT_WORKING_HOURS: dict[str, list[str]] = {
    "mon": ["09:30", "19:00"],
    "tue": ["09:30", "19:00"],
    "wed": ["09:30", "19:00"],
    "thu": ["09:30", "19:00"],
    "fri": ["09:30", "19:00"],
    "sat": ["10:00", "17:00"],
    "sun": [],
}


class Agent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    brokerage_name: Mapped[str | None] = mapped_column(String(160))

    # Conversation configuration
    languages: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: [Language.ENGLISH.value]
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    working_hours: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=lambda: dict(DEFAULT_WORKING_HOURS)
    )
    quiet_hours_start: Mapped[int] = mapped_column(Integer, nullable=False, default=21)
    quiet_hours_end: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    tone_instructions: Mapped[str | None] = mapped_column(Text)

    # Escalate to the human when a lead's budget crosses this (INR)
    escalation_budget_threshold: Mapped[int | None] = mapped_column(Integer)

    # Google Calendar OAuth (populated in M4)
    google_refresh_token: Mapped[str | None] = mapped_column(Text)
    google_calendar_id: Mapped[str | None] = mapped_column(String(255))

    # WhatsApp Business Cloud API (populated in M3)
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(String(64), index=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # SHA-256 of the agent's dashboard API token. Tokens are high-entropy random
    # strings, not user-chosen passwords, so a fast hash is appropriate — there is
    # no dictionary to attack. Replaced by real accounts in M7.
    api_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    listings: Mapped[list[Listing]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )
    leads: Mapped[list[Lead]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    appointments: Mapped[list[Appointment]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid, no PII
        return f"<Agent id={self.id} brokerage={self.brokerage_name!r}>"
