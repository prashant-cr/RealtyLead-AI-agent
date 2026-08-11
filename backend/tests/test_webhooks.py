import json
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.whatsapp_payload import SIGNATURE_HEADER, sign
from app.core.config import Settings
from app.models import Lead, Message
from app.models.enums import Channel, MessageDirection, MessageRole, MessageStatus
from tests.factories import make_agent, make_listing
from tests.test_whatsapp_payload import TEXT_MESSAGE, envelope, message_value

SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
PNID = "PNID123"

ClientFactory = Callable[..., Awaitable[AsyncClient]]


def wa_settings(**overrides: object) -> Settings:
    """Fully-configured WhatsApp settings, independent of the developer's .env."""
    return Settings(
        **{
            "whatsapp_app_secret": SECRET,
            "whatsapp_verify_token": VERIFY_TOKEN,
            "whatsapp_access_token": "tok",
            **overrides,
        }  # type: ignore[arg-type]
    )


@pytest.fixture
async def wa_client(client_factory: ClientFactory) -> AsyncClient:
    return await client_factory(wa_settings())


def signed(payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    return body, {SIGNATURE_HEADER: sign(body, SECRET), "Content-Type": "application/json"}


async def seed_agent(session: AsyncSession) -> None:
    agent = make_agent(whatsapp_phone_number_id=PNID)
    session.add(agent)
    await session.flush()
    session.add(make_listing(agent))
    await session.flush()


# ------------------------------------------------------------- verification


async def test_subscription_handshake_echoes_the_challenge(wa_client: AsyncClient) -> None:

    response = await wa_client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )

    assert response.status_code == 200
    assert response.text == "1158201444"


async def test_handshake_with_a_wrong_token_is_forbidden(wa_client: AsyncClient) -> None:

    response = await wa_client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "guessed", "hub.challenge": "x"},
    )

    assert response.status_code == 403


async def test_handshake_without_configuration_is_unavailable(
    client_factory: ClientFactory,
) -> None:
    unconfigured = await client_factory(wa_settings(whatsapp_verify_token=None))

    response = await unconfigured.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "y"},
    )

    assert response.status_code == 503


# ---------------------------------------------------------------- signatures


async def test_delivery_without_a_signature_is_rejected(wa_client: AsyncClient) -> None:
    body, _ = signed(envelope(message_value(TEXT_MESSAGE)))

    response = await wa_client.post(
        "/webhooks/whatsapp", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 401


async def test_delivery_with_a_forged_signature_is_rejected(wa_client: AsyncClient) -> None:
    body, _ = signed(envelope(message_value(TEXT_MESSAGE)))

    response = await wa_client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={SIGNATURE_HEADER: "sha256=" + "0" * 64, "Content-Type": "application/json"},
    )

    assert response.status_code == 401


async def test_signature_is_checked_against_the_raw_body(wa_client: AsyncClient) -> None:
    """A signature for one body must not authenticate a different one."""
    _, headers = signed(envelope(message_value(TEXT_MESSAGE)))
    tampered = json.dumps(
        envelope(message_value({**TEXT_MESSAGE, "from": "919999999999"}))
    ).encode()

    response = await wa_client.post("/webhooks/whatsapp", content=tampered, headers=headers)

    assert response.status_code == 401


async def test_unconfigured_secret_refuses_deliveries(client_factory: ClientFactory) -> None:
    unconfigured = await client_factory(wa_settings(whatsapp_app_secret=None))
    body, headers = signed(envelope(message_value(TEXT_MESSAGE)))

    response = await unconfigured.post("/webhooks/whatsapp", content=body, headers=headers)

    assert response.status_code == 503


# ------------------------------------------------------------------ delivery


async def test_valid_delivery_is_accepted_and_recorded(
    wa_client: AsyncClient, session: AsyncSession
) -> None:
    await seed_agent(session)
    body, headers = signed(envelope(message_value(TEXT_MESSAGE)))

    response = await wa_client.post("/webhooks/whatsapp", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    message = (await session.execute(select(Message))).scalar_one()
    assert message.external_id == "wamid.TEXT1"
    assert message.direction is MessageDirection.INBOUND
    lead = (await session.execute(select(Lead))).scalar_one()
    assert lead.phone == "+919876543210"


async def test_redelivery_of_the_same_message_is_deduplicated(
    wa_client: AsyncClient, session: AsyncSession
) -> None:
    await seed_agent(session)
    body, headers = signed(envelope(message_value(TEXT_MESSAGE)))

    first = await wa_client.post("/webhooks/whatsapp", content=body, headers=headers)
    second = await wa_client.post("/webhooks/whatsapp", content=body, headers=headers)

    assert first.json()["accepted"] == 1
    assert second.json()["accepted"] == 0
    count = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
    assert count == 1


async def test_delivery_for_an_unknown_number_is_acknowledged_not_retried(
    wa_client: AsyncClient, session: AsyncSession
) -> None:
    """A 5xx here would make Meta redeliver this batch forever."""
    # No agent registered for PNID123.
    body, headers = signed(envelope(message_value(TEXT_MESSAGE)))

    response = await wa_client.post("/webhooks/whatsapp", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["accepted"] == 0


async def test_status_only_delivery_updates_the_message(
    wa_client: AsyncClient, session: AsyncSession
) -> None:
    await seed_agent(session)
    # An inbound delivery first, so there is a conversation to hang the reply off.
    body, headers = signed(envelope(message_value(TEXT_MESSAGE)))
    await wa_client.post("/webhooks/whatsapp", content=body, headers=headers)
    inbound_row = (await session.execute(select(Message))).scalars().first()
    assert inbound_row is not None

    outbound = Message(
        conversation_id=inbound_row.conversation_id,
        role=MessageRole.ASSISTANT,
        direction=MessageDirection.OUTBOUND,
        channel=Channel.WHATSAPP,
        status=MessageStatus.SENT,
        content="Hi there",
        external_id="wamid.OUT1",
    )
    session.add(outbound)
    await session.flush()

    status_body, status_headers = signed(
        envelope(
            {
                "metadata": {"phone_number_id": PNID},
                "statuses": [
                    {"id": "wamid.OUT1", "status": "delivered", "timestamp": "1786500001"}
                ],
            }
        )
    )
    response = await wa_client.post(
        "/webhooks/whatsapp", content=status_body, headers=status_headers
    )

    assert response.json()["statuses_applied"] == 1
    await session.refresh(outbound)
    assert outbound.status is MessageStatus.DELIVERED


async def test_malformed_json_is_a_client_error(wa_client: AsyncClient) -> None:
    body = b"{not json"

    response = await wa_client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={SIGNATURE_HEADER: sign(body, SECRET), "Content-Type": "application/json"},
    )

    assert response.status_code == 400


async def test_empty_delivery_is_acknowledged(wa_client: AsyncClient) -> None:
    body, headers = signed({"object": "whatsapp_business_account", "entry": []})

    response = await wa_client.post("/webhooks/whatsapp", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "statuses_applied": 0}
