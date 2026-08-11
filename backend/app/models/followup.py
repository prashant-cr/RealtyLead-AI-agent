"""Scheduled nudges for unresponsive leads (day 1, 3, 7, 14, then monthly)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import Channel, FollowUpStatus
from app.models.types import UtcDateTime, enum_column

if TYPE_CHECKING:
    from app.models.lead import Lead

# Days after the last inbound message. Index = attempt number - 1; monthly after the last entry.
FOLLOW_UP_CADENCE_DAYS: tuple[int, ...] = (1, 3, 7, 14, 44, 74)


class FollowUpTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "follow_up_tasks"
    __table_args__ = (Index("ix_follow_up_tasks_status_scheduled_for", "status", "scheduled_for"),)

    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )

    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    scheduled_for: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    channel: Mapped[Channel] = mapped_column(
        enum_column(Channel), nullable=False, default=Channel.WHATSAPP
    )
    status: Mapped[FollowUpStatus] = mapped_column(
        enum_column(FollowUpStatus), nullable=False, default=FollowUpStatus.SCHEDULED
    )

    # Business-initiated WhatsApp messages after the 24h window need an approved template.
    template_name: Mapped[str | None] = mapped_column(String(120))
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    outcome_reason: Mapped[str | None] = mapped_column(String(255))

    lead: Mapped[Lead] = relationship(back_populates="follow_up_tasks")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FollowUpTask id={self.id} attempt={self.attempt_number} status={self.status}>"
