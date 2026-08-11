"""Booked calls and site visits."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AppointmentStatus, AppointmentType
from app.models.types import UtcDateTime, enum_column

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.lead import Lead
    from app.models.listing import Listing


class Appointment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (Index("ix_appointments_agent_starts_at", "agent_id", "starts_at"),)

    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("listings.id", ondelete="SET NULL")
    )

    appointment_type: Mapped[AppointmentType] = mapped_column(
        enum_column(AppointmentType), nullable=False, default=AppointmentType.CALL
    )
    status: Mapped[AppointmentStatus] = mapped_column(
        enum_column(AppointmentStatus), nullable=False, default=AppointmentStatus.PENDING
    )

    starts_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    location: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    google_event_id: Mapped[str | None] = mapped_column(String(255))
    confirmation_sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    lead: Mapped[Lead] = relationship(back_populates="appointments")
    agent: Mapped[Agent] = relationship(back_populates="appointments")
    listing: Mapped[Listing | None] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Appointment id={self.id} type={self.appointment_type} status={self.status}>"
