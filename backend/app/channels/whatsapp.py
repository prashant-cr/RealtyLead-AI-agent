"""WhatsApp Business Cloud API adapter (Meta Graph API).

Covers the three things the product needs from Meta: send a message, fetch media
a lead sent us, and mark a message read. Webhook *parsing* lives next door in
`payload.py` so it can be tested without a client.

Every call has a timeout, retries with exponential backoff on transient failures,
and logs with the phone number masked.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.channels.base import ChannelAdapter, DeliveryResult, OutboundMessage
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, mask_phone
from app.models.enums import Channel

log = get_logger(__name__)

# Meta retries the webhook if these fail; retrying our side is safe and cheaper.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class WhatsAppError(RuntimeError):
    """A Graph API call failed after exhausting retries."""


@dataclass(frozen=True)
class MediaPayload:
    media_id: str
    mime_type: str
    content: bytes
    sha256: str | None = None


def normalise_phone(raw: str) -> str:
    """Meta sends `919876543210`; we store E.164 (`+919876543210`)."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"+{digits}" if digits else raw


def to_wa_id(phone: str) -> str:
    """The inverse — Meta's `to` field wants no leading plus."""
    return phone.lstrip("+")


def within_service_window(
    last_inbound_at: datetime | None, now: datetime, window_hours: int = 24
) -> bool:
    """True when a free-form (non-template) message is still allowed.

    Meta only permits free-form replies for `window_hours` after the lead's last
    message. Outside it, business-initiated messages must use an approved template
    — which is what the follow-up worker (M5) will hit.
    """
    if last_inbound_at is None:
        return False
    return now - last_inbound_at < timedelta(hours=window_hours)


class WhatsAppChannel(ChannelAdapter):
    channel = Channel.WHATSAPP

    def __init__(
        self,
        phone_number_id: str,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        access_token: str | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._phone_number_id = phone_number_id
        self._token = access_token or self._settings.whatsapp_access_token
        if not self._token:
            raise WhatsAppError("WHATSAPP_ACCESS_TOKEN is not set")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.http_timeout_seconds)
        )

    # ---------------------------------------------------------------- plumbing

    @property
    def _base(self) -> str:
        return f"{self._settings.whatsapp_graph_url}/{self._settings.whatsapp_api_version}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One Graph call, retried with exponential backoff and jitter."""
        attempts = self._settings.http_max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = await self._client.request(method, url, headers=self._headers, **kwargs)
            except httpx.RequestError as exc:
                last_error = exc
                log.warning(
                    "whatsapp %s %s failed (attempt %s/%s): %s",
                    method,
                    url.rsplit("/", 1)[-1],
                    attempt + 1,
                    attempts,
                    type(exc).__name__,
                )
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in RETRYABLE_STATUS:
                    raise WhatsAppError(
                        f"WhatsApp API rejected the request "
                        f"({response.status_code}): {self._error_detail(response)}"
                    )
                last_error = WhatsAppError(f"transient WhatsApp error {response.status_code}")
                log.warning(
                    "whatsapp %s returned %s (attempt %s/%s)",
                    method,
                    response.status_code,
                    attempt + 1,
                    attempts,
                )

            if attempt < attempts - 1:
                backoff = self._settings.http_backoff_base_seconds * (2**attempt)
                await asyncio.sleep(backoff + random.uniform(0, backoff / 2))

        raise WhatsAppError(f"WhatsApp API unreachable after {attempts} attempts") from last_error

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            return str(response.json().get("error", {}).get("message", response.text[:200]))
        except ValueError:
            return response.text[:200]

    # ------------------------------------------------------------------ sending

    def _build_body(self, message: OutboundMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_id(message.recipient),
        }
        if message.template_name:
            payload["type"] = "template"
            payload["template"] = {
                "name": message.template_name,
                "language": {"code": message.template_variables.get("language", "en")},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": value}
                            for key, value in message.template_variables.items()
                            if key != "language"
                        ],
                    }
                ],
            }
        else:
            payload["type"] = "text"
            payload["text"] = {"preview_url": False, "body": message.text}
        return payload

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        url = f"{self._base}/{self._phone_number_id}/messages"
        try:
            response = await self._request("POST", url, json=self._build_body(message))
        except WhatsAppError as exc:
            log.error("whatsapp send to %s failed: %s", mask_phone(message.recipient), exc)
            return DeliveryResult(external_id=None, accepted=False, error=str(exc))

        body = response.json()
        external_id = (body.get("messages") or [{}])[0].get("id")
        log.info(
            "whatsapp message sent to %s (template=%s)",
            mask_phone(message.recipient),
            message.template_name or "-",
        )
        return DeliveryResult(external_id=external_id)

    async def mark_read(self, message_id: str) -> bool:
        """Blue ticks. Best-effort — never let this failure break a turn."""
        url = f"{self._base}/{self._phone_number_id}/messages"
        try:
            await self._request(
                "POST",
                url,
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                },
            )
        except WhatsAppError as exc:
            log.warning("could not mark %s read: %s", message_id, exc)
            return False
        return True

    # -------------------------------------------------------------------- media

    async def download_media(self, media_id: str) -> MediaPayload:
        """Two hops: resolve the media id to a short-lived URL, then fetch it."""
        lookup = await self._request("GET", f"{self._base}/{media_id}")
        meta = lookup.json()
        url = meta.get("url")
        if not url:
            raise WhatsAppError(f"no download URL returned for media {media_id}")

        content = await self._request("GET", url)
        return MediaPayload(
            media_id=media_id,
            mime_type=meta.get("mime_type", "application/octet-stream"),
            content=content.content,
            sha256=meta.get("sha256"),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
