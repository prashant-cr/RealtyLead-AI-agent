"""Conversation threads and their messages."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    Channel,
    ConversationStatus,
    MessageDirection,
    MessageRole,
    MessageStatus,
)
from app.models.types import UtcDateTime, enum_column

if TYPE_CHECKING:
    from app.models.lead import Lead


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_lead_channel", "lead_id", "channel"),)

    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[Channel] = mapped_column(enum_column(Channel), nullable=False)
    status: Mapped[ConversationStatus] = mapped_column(
        enum_column(ConversationStatus), nullable=False, default=ConversationStatus.ACTIVE
    )
    last_message_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    lead: Mapped[Lead] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Conversation id={self.id} channel={self.channel} status={self.status}>"


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Channel webhooks retry; (channel, external_id) makes ingestion idempotent.
        UniqueConstraint("channel", "external_id", name="uq_messages_channel_external_id"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[MessageRole] = mapped_column(enum_column(MessageRole), nullable=False)
    direction: Mapped[MessageDirection] = mapped_column(
        enum_column(MessageDirection), nullable=False
    )
    channel: Mapped[Channel] = mapped_column(enum_column(Channel), nullable=False)
    status: Mapped[MessageStatus] = mapped_column(
        enum_column(MessageStatus), nullable=False, default=MessageStatus.PENDING
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Provider message id — nullable for locally generated messages.
    external_id: Mapped[str | None] = mapped_column(Text)
    media_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Tool calls / provider payload for debugging; never rendered to the lead.
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover - content is PII, never repr it
        return f"<Message id={self.id} role={self.role} direction={self.direction}>"
