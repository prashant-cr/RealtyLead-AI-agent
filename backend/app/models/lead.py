"""Inbound property enquiry + everything we learn while qualifying it."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    ConsentStatus,
    Language,
    LeadPurpose,
    LeadStatus,
    LeadTemperature,
    PropertyType,
)
from app.models.types import UtcDateTime, enum_column

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.appointment import Appointment
    from app.models.conversation import Conversation
    from app.models.followup import FollowUpTask


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (
        # One lead record per phone per agent — inbound webhooks dedupe against this.
        UniqueConstraint("agent_id", "phone", name="uq_leads_agent_id_phone"),
        Index("ix_leads_agent_status", "agent_id", "status"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # --- contact (PII: mask before logging) ---
    name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    language: Mapped[Language] = mapped_column(
        enum_column(Language), nullable=False, default=Language.ENGLISH
    )
    timezone: Mapped[str | None] = mapped_column(String(64))

    # --- pipeline ---
    status: Mapped[LeadStatus] = mapped_column(
        enum_column(LeadStatus), nullable=False, default=LeadStatus.NEW
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    temperature: Mapped[LeadTemperature] = mapped_column(
        enum_column(LeadTemperature), nullable=False, default=LeadTemperature.COLD
    )
    # The dashboard must show *why* — list of {"factor", "points", "detail"}
    score_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    scored_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # --- qualification data ---
    budget_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    budget_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    preferred_locations: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    property_type: Mapped[PropertyType | None] = mapped_column(enum_column(PropertyType))
    bhk: Mapped[int | None] = mapped_column(Integer)
    timeline_months: Mapped[int | None] = mapped_column(Integer)
    loan_preapproved: Mapped[bool | None] = mapped_column(Boolean)
    purpose: Mapped[LeadPurpose] = mapped_column(
        enum_column(LeadPurpose), nullable=False, default=LeadPurpose.UNKNOWN
    )
    site_visit_willing: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("listings.id", ondelete="SET NULL")
    )

    # --- consent / messaging state (DPDP + TRAI + WhatsApp 24h rule) ---
    consent_status: Mapped[ConsentStatus] = mapped_column(
        enum_column(ConsentStatus), nullable=False, default=ConsentStatus.UNKNOWN
    )
    opted_out_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_inbound_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    last_outbound_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    follow_up_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- human handoff ---
    handed_off_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    handoff_reason: Mapped[str | None] = mapped_column(String(255))

    agent: Mapped[Agent] = relationship(back_populates="leads")
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    appointments: Mapped[list[Appointment]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    follow_up_tasks: Mapped[list[FollowUpTask]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )

    @property
    def is_contactable(self) -> bool:
        """Outbound messaging is only ever allowed when this is True."""
        return self.consent_status is not ConsentStatus.OPTED_OUT

    def __repr__(self) -> str:  # pragma: no cover - debugging aid, never logs PII
        return f"<Lead id={self.id} status={self.status} score={self.score}>"
