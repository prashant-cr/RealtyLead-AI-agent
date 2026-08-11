"""The one interface every messaging channel implements.

WhatsApp is the first real adapter (M3); SMS and email follow. The in-memory
channel used by the CLI harness and the tests implements the same interface, so
the conversation engine never knows which channel it is talking to.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.enums import Channel


@dataclass(frozen=True)
class InboundMessage:
    """A message from a lead, normalised out of whatever the provider sent."""

    channel: Channel
    sender: str  # phone number in E.164, or email address
    text: str
    external_id: str | None = None
    received_at: datetime | None = None
    media_urls: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    recipient: str | None = None  # the agent's number/address the lead wrote to


@dataclass(frozen=True)
class OutboundMessage:
    channel: Channel
    recipient: str
    text: str
    lead_id: uuid.UUID | None = None
    # Business-initiated messages outside WhatsApp's 24h window need an approved template.
    template_name: str | None = None
    template_variables: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    external_id: str | None
    accepted: bool = True
    error: str | None = None


class ChannelAdapter(ABC):
    """Send messages out over one channel."""

    channel: Channel

    @abstractmethod
    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Deliver a message. Implementations own their own timeouts and retries."""

    async def close(self) -> None:
        """Release provider resources. Override where the adapter holds a client."""
        return None
