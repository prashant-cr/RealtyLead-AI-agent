"""Dashboard login sessions.

A row per browser login rather than one long-lived token per agent: sessions
expire, can be revoked individually ("sign out everywhere"), and record when they
were last used. Only the SHA-256 of the token is stored — the token itself exists
only in the agent's browser.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.types import UtcDateTime

if TYPE_CHECKING:
    from app.models.agent import Agent


class AgentSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (Index("ix_agent_sessions_agent_expires", "agent_id", "expires_at"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Coarse client hint for the "your sessions" list. Never used for auth.
    user_agent: Mapped[str | None] = mapped_column(String(255))

    agent: Mapped[Agent] = relationship(back_populates="sessions")

    def is_valid(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now

    def __repr__(self) -> str:  # pragma: no cover - never log the token
        return f"<AgentSession id={self.id} agent={self.agent_id} expires={self.expires_at}>"
