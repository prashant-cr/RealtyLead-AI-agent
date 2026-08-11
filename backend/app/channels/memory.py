"""In-memory channel — the M2 test harness.

Lets us drive a full conversation from the CLI and from pytest with no WhatsApp
account, no webhooks and no network. Everything sent is kept in `outbox`.
"""

from __future__ import annotations

import itertools

from app.channels.base import ChannelAdapter, DeliveryResult, OutboundMessage
from app.models.enums import Channel


class InMemoryChannel(ChannelAdapter):
    channel = Channel.CLI

    def __init__(self, channel: Channel = Channel.CLI) -> None:
        self.channel = channel
        self.outbox: list[OutboundMessage] = []
        self._ids = itertools.count(1)

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        self.outbox.append(message)
        return DeliveryResult(external_id=f"mem-{next(self._ids)}")

    @property
    def last_text(self) -> str | None:
        return self.outbox[-1].text if self.outbox else None

    def clear(self) -> None:
        self.outbox.clear()
