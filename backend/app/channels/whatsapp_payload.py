"""Parsing and authenticating WhatsApp webhook deliveries.

Kept free of HTTP and database concerns: everything here is a pure function over
the payload Meta posts, which makes the awkward cases (unknown message types,
statuses, malformed entries) cheap to test.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.channels.base import InboundMessage
from app.channels.whatsapp import normalise_phone
from app.core.logging import get_logger
from app.models.enums import Channel, MessageStatus

log = get_logger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="

# Meta's delivery receipts -> our MessageStatus.
STATUS_MAP = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}

# Message types that carry media rather than text.
MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")

# What the engine sees when a lead sends media with no caption. The model cannot
# see the file, so it is told what arrived rather than being handed an empty turn.
MEDIA_PLACEHOLDERS = {
    "image": "[the lead sent an image]",
    "video": "[the lead sent a video]",
    "audio": "[the lead sent a voice note]",
    "document": "[the lead sent a document]",
    "sticker": "[the lead sent a sticker]",
    "location": "[the lead shared a location]",
    "contacts": "[the lead shared a contact]",
    "unknown": "[the lead sent something this assistant cannot read]",
}


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """Constant-time check of Meta's `X-Hub-Signature-256` over the raw body.

    The signature covers the exact bytes Meta sent — re-serialising the parsed
    JSON produces a different digest, so callers must pass the raw request body.
    """
    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len(SIGNATURE_PREFIX) :])


def sign(raw_body: bytes, app_secret: str) -> str:
    """Produce a header value — used by tests and for local webhook replay."""
    digest = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


@dataclass(frozen=True)
class StatusUpdate:
    external_id: str
    status: MessageStatus
    timestamp: datetime | None = None
    error: str | None = None


@dataclass(frozen=True)
class ParsedWebhook:
    messages: list[InboundMessage] = field(default_factory=list)
    statuses: list[StatusUpdate] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.messages or self.statuses)


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError):
        return None


def _extract_text(message: dict[str, Any]) -> tuple[str, list[str]]:
    """Return the text the engine should see, plus any media ids."""
    message_type = message.get("type", "unknown")

    if message_type == "text":
        return message.get("text", {}).get("body", ""), []

    if message_type in MEDIA_TYPES:
        media = message.get(message_type, {}) or {}
        media_ids = [media["id"]] if media.get("id") else []
        caption = (media.get("caption") or "").strip()
        placeholder = MEDIA_PLACEHOLDERS.get(message_type, MEDIA_PLACEHOLDERS["unknown"])
        return (f"{placeholder} {caption}".strip() if caption else placeholder), media_ids

    if message_type == "interactive":
        # Button and list replies — the title is what the lead actually chose.
        interactive = message.get("interactive", {})
        for key in ("button_reply", "list_reply"):
            if reply := interactive.get(key):
                return reply.get("title", ""), []
        return MEDIA_PLACEHOLDERS["unknown"], []

    if message_type == "button":
        return message.get("button", {}).get("text", ""), []

    if message_type == "location":
        location = message.get("location", {})
        name = location.get("name") or location.get("address")
        base = MEDIA_PLACEHOLDERS["location"]
        return (f"{base} ({name})" if name else base), []

    return MEDIA_PLACEHOLDERS.get(message_type, MEDIA_PLACEHOLDERS["unknown"]), []


def parse_webhook(payload: dict[str, Any]) -> ParsedWebhook:
    """Flatten Meta's nested envelope into messages and status updates.

    Malformed or unrecognised entries are logged and skipped rather than raising:
    a 500 here makes Meta retry the whole batch, including the parts that were fine.
    """
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            recipient = metadata.get("phone_number_id")

            names = {
                contact.get("wa_id"): (contact.get("profile") or {}).get("name")
                for contact in value.get("contacts") or []
            }

            for message in value.get("messages") or []:
                try:
                    sender = message["from"]
                    text, media_ids = _extract_text(message)
                    messages.append(
                        InboundMessage(
                            channel=Channel.WHATSAPP,
                            sender=normalise_phone(sender),
                            text=text,
                            external_id=message.get("id"),
                            received_at=_timestamp(message.get("timestamp")),
                            media_urls=media_ids,
                            recipient=recipient,
                            raw={
                                "type": message.get("type"),
                                "profile_name": names.get(sender),
                                "context": message.get("context"),
                            },
                        )
                    )
                except (KeyError, TypeError) as exc:
                    log.warning("skipping malformed WhatsApp message: %s", type(exc).__name__)

            for status in value.get("statuses") or []:
                mapped = STATUS_MAP.get(status.get("status", ""))
                if mapped is None or not status.get("id"):
                    continue
                errors = status.get("errors") or []
                statuses.append(
                    StatusUpdate(
                        external_id=status["id"],
                        status=mapped,
                        timestamp=_timestamp(status.get("timestamp")),
                        error=errors[0].get("title") if errors else None,
                    )
                )

    return ParsedWebhook(messages=messages, statuses=statuses)
