"""Property inventory. The conversation engine may only quote facts from here."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ListingStatus, PropertyType
from app.models.types import enum_column

if TYPE_CHECKING:
    from app.models.agent import Agent


class Listing(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "listings"
    __table_args__ = (
        Index("ix_listings_agent_status", "agent_id", "status"),
        Index("ix_listings_city_type", "city", "property_type"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    property_type: Mapped[PropertyType] = mapped_column(enum_column(PropertyType), nullable=False)
    status: Mapped[ListingStatus] = mapped_column(
        enum_column(ListingStatus), nullable=False, default=ListingStatus.AVAILABLE
    )

    locality: Mapped[str | None] = mapped_column(String(160), index=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str | None] = mapped_column(String(120))

    price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    bhk: Mapped[int | None] = mapped_column(Integer)
    carpet_area_sqft: Mapped[int | None] = mapped_column(Integer)

    description: Mapped[str | None] = mapped_column(Text)
    # RERA registration must be shown wherever legally required
    rera_id: Mapped[str | None] = mapped_column(String(64))
    media_urls: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    agent: Mapped[Agent] = relationship(back_populates="listings")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Listing id={self.id} title={self.title!r} status={self.status}>"
