from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.channels.base import OutboundMessage
from app.channels.whatsapp import (
    WhatsAppChannel,
    WhatsAppError,
    within_service_window,
)
from app.core.config import Settings
from app.models.enums import Channel

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    return Settings(
        whatsapp_access_token="tok",
        http_max_retries=2,
        http_backoff_base_seconds=0.0,  # keep tests fast
        **overrides,  # type: ignore[arg-type]
    )


def channel(handler: object, **setting_overrides: object) -> WhatsAppChannel:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return WhatsAppChannel("PNID123", settings(**setting_overrides), client=client)


# ------------------------------------------------------------- service window


def test_reply_allowed_inside_the_24h_window() -> None:
    assert within_service_window(NOW - timedelta(hours=23), NOW) is True


def test_reply_blocked_outside_the_24h_window() -> None:
    assert within_service_window(NOW - timedelta(hours=25), NOW) is False


def test_never_messaged_lead_is_outside_the_window() -> None:
    assert within_service_window(None, NOW) is False


# --------------------------------------------------------------------- sending


async def test_text_message_uses_the_expected_wire_format() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = httpx.Response(200).json if False else request.content
        return httpx.Response(200, json={"messages": [{"id": "wamid.SENT1"}]})

    adapter = channel(handler)
    result = await adapter.send(
        OutboundMessage(channel=Channel.WHATSAPP, recipient="+919876543210", text="Hello!")
    )

    assert result.accepted is True
    assert result.external_id == "wamid.SENT1"
    assert seen["url"] == "https://graph.facebook.com/v21.0/PNID123/messages"
    assert seen["auth"] == "Bearer tok"
    body = seen["body"]
    assert b'"to":"919876543210"' in body.replace(b" ", b"")  # type: ignore[union-attr]
    assert b'"type":"text"' in body.replace(b" ", b"")  # type: ignore[union-attr]


async def test_template_message_is_sent_as_a_template() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.TPL"}]})

    adapter = channel(handler)
    await adapter.send(
        OutboundMessage(
            channel=Channel.WHATSAPP,
            recipient="+919876543210",
            text="ignored for templates",
            template_name="followup_day_1",
            template_variables={"language": "en", "name": "Priya"},
        )
    )

    assert captured["type"] == "template"
    template = captured["template"]
    assert template["name"] == "followup_day_1"  # type: ignore[index]
    assert template["language"] == {"code": "en"}  # type: ignore[index]
    params = template["components"][0]["parameters"]  # type: ignore[index]
    assert params == [{"type": "text", "text": "Priya"}]


# ----------------------------------------------------------- retries + errors


async def test_transient_error_is_retried_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"error": {"message": "try later"}})
        return httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]})

    adapter = channel(handler)
    result = await adapter.send(
        OutboundMessage(channel=Channel.WHATSAPP, recipient="+919876543210", text="hi")
    )

    assert attempts["n"] == 3
    assert result.accepted is True


async def test_network_error_is_retried_then_reported() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("no route to host")

    adapter = channel(handler)
    result = await adapter.send(
        OutboundMessage(channel=Channel.WHATSAPP, recipient="+919876543210", text="hi")
    )

    assert attempts["n"] == 3  # 1 attempt + 2 retries
    assert result.accepted is False
    assert result.error is not None


async def test_client_error_is_not_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"error": {"message": "Invalid phone number"}})

    adapter = channel(handler)
    result = await adapter.send(
        OutboundMessage(channel=Channel.WHATSAPP, recipient="+91bogus", text="hi")
    )

    assert attempts["n"] == 1  # a 400 will never succeed on retry
    assert result.accepted is False
    assert "Invalid phone number" in (result.error or "")


async def test_send_failure_is_reported_not_raised() -> None:
    adapter = channel(lambda request: httpx.Response(401, json={"error": {"message": "bad token"}}))

    result = await adapter.send(
        OutboundMessage(channel=Channel.WHATSAPP, recipient="+919876543210", text="hi")
    )

    assert result.accepted is False
    assert result.external_id is None


def test_missing_access_token_fails_loudly() -> None:
    with pytest.raises(WhatsAppError, match="WHATSAPP_ACCESS_TOKEN"):
        WhatsAppChannel("PNID123", Settings(whatsapp_access_token=None))


# ----------------------------------------------------------------- read + media


async def test_mark_read_posts_the_status() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    adapter = channel(handler)
    assert await adapter.mark_read("wamid.IN1") is True
    assert captured == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.IN1",
    }


async def test_mark_read_failure_is_swallowed() -> None:
    adapter = channel(lambda request: httpx.Response(400, json={"error": {"message": "gone"}}))

    assert await adapter.mark_read("wamid.OLD") is False


async def test_media_download_resolves_then_fetches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/MEDIA1"):
            return httpx.Response(
                200,
                json={
                    "url": "https://lookaside.example/asset",
                    "mime_type": "image/jpeg",
                    "sha256": "abc123",
                },
            )
        return httpx.Response(200, content=b"\xff\xd8jpegbytes")

    adapter = channel(handler)
    media = await adapter.download_media("MEDIA1")

    assert media.mime_type == "image/jpeg"
    assert media.content == b"\xff\xd8jpegbytes"
    assert media.sha256 == "abc123"


async def test_media_without_a_url_raises() -> None:
    adapter = channel(lambda request: httpx.Response(200, json={"mime_type": "image/jpeg"}))

    with pytest.raises(WhatsAppError, match="no download URL"):
        await adapter.download_media("MEDIA_BROKEN")
