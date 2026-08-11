from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import InboundMessage
from app.channels.memory import InMemoryChannel
from app.channels.whatsapp_payload import StatusUpdate
from app.core.config import Settings
from app.models import Lead, Message
from app.models.enums import (
    Channel,
    ConsentStatus,
    LeadStatus,
    MessageDirection,
    MessageStatus,
)
from app.services.ingestion import (
    DeliveryRejectedError,
    UnknownAgentError,
    apply_status_updates,
    claim_inbound,
    process_claimed,
)
from tests.factories import make_agent, make_listing
from tests.fakes import FakeLLM, text_turn

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
PNID = "PNID123"


def inbound(text: str = "Hi", external_id: str = "wamid.1", **overrides: object) -> InboundMessage:
    defaults: dict[str, object] = {
        "channel": Channel.WHATSAPP,
        "sender": "+919876543210",
        "text": text,
        "external_id": external_id,
        "received_at": NOW,
        "recipient": PNID,
        "raw": {"profile_name": "Priya Shah", "type": "text"},
    }
    return InboundMessage(**{**defaults, **overrides})  # type: ignore[arg-type]


async def seed_agent(session: AsyncSession, phone_number_id: str | None = PNID):
    agent = make_agent(whatsapp_phone_number_id=phone_number_id)
    session.add(agent)
    await session.flush()
    session.add(make_listing(agent))
    await session.flush()
    return agent


def whatsapp_settings() -> Settings:
    return Settings(whatsapp_access_token="tok", anthropic_api_key="test")


# ------------------------------------------------------------- agent routing


async def test_unknown_phone_number_id_is_rejected(session: AsyncSession) -> None:
    await seed_agent(session, phone_number_id="OTHER")

    with pytest.raises(UnknownAgentError):
        await claim_inbound(session, inbound())


async def test_missing_phone_number_id_is_rejected(session: AsyncSession) -> None:
    await seed_agent(session)

    with pytest.raises(UnknownAgentError):
        await claim_inbound(session, inbound(recipient=None))


async def test_inactive_agent_is_not_matched(session: AsyncSession) -> None:
    agent = await seed_agent(session)
    agent.is_active = False
    await session.flush()

    with pytest.raises(UnknownAgentError):
        await claim_inbound(session, inbound())


# ------------------------------------------------------------------ claiming


async def test_first_delivery_creates_lead_conversation_and_message(
    session: AsyncSession,
) -> None:
    agent = await seed_agent(session)

    claim = await claim_inbound(session, inbound("Is the Bopal flat available?"))

    assert claim is not None
    lead = await session.get_one(Lead, claim.lead_id)
    assert lead.phone == "+919876543210"
    assert lead.name == "Priya Shah"
    assert lead.source == "whatsapp"
    assert lead.agent_id == agent.id
    message = await session.get_one(Message, claim.message_id)
    assert message.direction is MessageDirection.INBOUND
    assert message.external_id == "wamid.1"
    assert message.status is MessageStatus.RECEIVED


async def test_duplicate_delivery_is_ignored(session: AsyncSession) -> None:
    await seed_agent(session)
    first = await claim_inbound(session, inbound())

    second = await claim_inbound(session, inbound())

    assert first is not None
    assert second is None
    count = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
    assert count == 1


async def test_second_message_reuses_the_lead_and_conversation(session: AsyncSession) -> None:
    await seed_agent(session)
    first = await claim_inbound(session, inbound("one", "wamid.1"))
    second = await claim_inbound(session, inbound("two", "wamid.2"))

    assert first is not None and second is not None
    assert first.lead_id == second.lead_id
    m1 = await session.get_one(Message, first.message_id)
    m2 = await session.get_one(Message, second.message_id)
    assert m1.conversation_id == m2.conversation_id


async def test_media_ids_and_metadata_are_recorded(session: AsyncSession) -> None:
    await seed_agent(session)

    claim = await claim_inbound(
        session,
        inbound(
            "[the lead sent an image]",
            "wamid.IMG",
            media_urls=["MEDIA1"],
            raw={"profile_name": "Priya Shah", "type": "image"},
        ),
    )

    assert claim is not None
    message = await session.get_one(Message, claim.message_id)
    assert message.media_urls == ["MEDIA1"]
    assert message.meta["type"] == "image"


async def test_existing_lead_gets_a_name_when_meta_supplies_one(session: AsyncSession) -> None:
    agent = await seed_agent(session)
    session.add(Lead(agent_id=agent.id, phone="+919876543210", source="portal"))
    await session.flush()

    claim = await claim_inbound(session, inbound())

    assert claim is not None
    lead = await session.get_one(Lead, claim.lead_id)
    assert lead.name == "Priya Shah"
    assert lead.source == "portal"  # original attribution preserved


# ---------------------------------------------------------------- processing


async def test_processing_runs_the_turn_and_sends_the_reply(session: AsyncSession) -> None:
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("Do you have 3 BHK in Bopal?"))
    assert claim is not None
    adapter = InMemoryChannel(Channel.WHATSAPP)
    llm = FakeLLM(text_turn("Yes! What's your budget?"))

    result = await process_claimed(session, llm, adapter, claim, whatsapp_settings(), now=NOW)

    assert result.reply == "Yes! What's your budget?"
    assert len(adapter.outbox) == 1
    assert adapter.outbox[0].recipient == "+919876543210"
    assert adapter.outbox[0].text == "Yes! What's your budget?"


async def test_processing_does_not_duplicate_the_inbound_row(session: AsyncSession) -> None:
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("hello"))
    assert claim is not None

    await process_claimed(
        session,
        FakeLLM(text_turn("hi")),
        InMemoryChannel(Channel.WHATSAPP),
        claim,
        whatsapp_settings(),
        now=NOW,
    )

    inbound_count = (
        await session.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.direction == MessageDirection.INBOUND)
        )
    ).scalar_one()
    assert inbound_count == 1


async def test_reply_is_recorded_with_the_provider_message_id(session: AsyncSession) -> None:
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("hello"))
    assert claim is not None
    adapter = InMemoryChannel(Channel.WHATSAPP)

    await process_claimed(
        session, FakeLLM(text_turn("hi there")), adapter, claim, whatsapp_settings(), now=NOW
    )

    outbound = (
        await session.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND))
    ).scalar_one()
    assert outbound.external_id == "mem-1"
    assert outbound.status is MessageStatus.SENT


async def test_opt_out_over_whatsapp_is_honoured(session: AsyncSession) -> None:
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("STOP"))
    assert claim is not None
    llm = FakeLLM(text_turn("never sent"))

    result = await process_claimed(
        session, llm, InMemoryChannel(Channel.WHATSAPP), claim, whatsapp_settings(), now=NOW
    )

    assert llm.calls == []
    assert result.opted_out is True
    lead = await session.get_one(Lead, claim.lead_id)
    assert lead.status is LeadStatus.OPTED_OUT


async def test_reply_outside_the_service_window_is_not_sent(session: AsyncSession) -> None:
    """Meta forbids free-form replies more than 24h after the lead's message."""
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("hello"))  # received at NOW
    assert claim is not None
    adapter = InMemoryChannel(Channel.WHATSAPP)
    late = NOW + timedelta(hours=30)  # processing badly delayed

    await process_claimed(
        session, FakeLLM(text_turn("hi")), adapter, claim, whatsapp_settings(), now=late
    )

    assert adapter.outbox == []
    outbound = (
        await session.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND))
    ).scalar_one()
    assert outbound.status is MessageStatus.FAILED


async def test_prompt_reply_inside_the_window_is_sent(session: AsyncSession) -> None:
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("hello"))
    assert claim is not None
    adapter = InMemoryChannel(Channel.WHATSAPP)

    await process_claimed(
        session,
        FakeLLM(text_turn("hi")),
        adapter,
        claim,
        whatsapp_settings(),
        now=NOW + timedelta(hours=23),
    )

    assert len(adapter.outbox) == 1


async def test_send_failure_marks_the_message_failed(session: AsyncSession) -> None:
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("hello"))
    assert claim is not None

    class FailingChannel(InMemoryChannel):
        async def send(self, message):  # type: ignore[no-untyped-def]
            from app.channels.base import DeliveryResult

            return DeliveryResult(external_id=None, accepted=False, error="rejected")

    with pytest.raises(DeliveryRejectedError):
        await process_claimed(
            session,
            FakeLLM(text_turn("hi")),
            FailingChannel(Channel.WHATSAPP),
            claim,
            whatsapp_settings(),
            now=NOW,
        )

    # Marked failed before raising, so a caller that commits rather than retries
    # still records the truth. Raising is what lets the inbound worker retry
    # instead of acknowledging a reply the lead never received.
    outbound = (
        await session.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND))
    ).scalar_one()
    assert outbound.status is MessageStatus.FAILED


# ------------------------------------------------------------ status updates


async def test_delivery_receipts_advance_message_status(session: AsyncSession) -> None:
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("hello"))
    assert claim is not None
    await process_claimed(
        session,
        FakeLLM(text_turn("hi")),
        InMemoryChannel(Channel.WHATSAPP),
        claim,
        whatsapp_settings(),
        now=NOW,
    )

    applied = await apply_status_updates(
        session, [StatusUpdate(external_id="mem-1", status=MessageStatus.DELIVERED)]
    )

    assert applied == 1
    outbound = (
        await session.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND))
    ).scalar_one()
    assert outbound.status is MessageStatus.DELIVERED


async def test_out_of_order_receipts_never_regress_status(session: AsyncSession) -> None:
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("hello"))
    assert claim is not None
    await process_claimed(
        session,
        FakeLLM(text_turn("hi")),
        InMemoryChannel(Channel.WHATSAPP),
        claim,
        whatsapp_settings(),
        now=NOW,
    )
    await apply_status_updates(
        session, [StatusUpdate(external_id="mem-1", status=MessageStatus.READ)]
    )

    # A delayed "sent" receipt arrives after "read".
    await apply_status_updates(
        session, [StatusUpdate(external_id="mem-1", status=MessageStatus.SENT)]
    )

    outbound = (
        await session.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND))
    ).scalar_one()
    assert outbound.status is MessageStatus.READ


async def test_receipt_for_an_unknown_message_is_ignored(session: AsyncSession) -> None:
    applied = await apply_status_updates(
        session, [StatusUpdate(external_id="wamid.NEVER", status=MessageStatus.DELIVERED)]
    )

    assert applied == 0


async def test_failed_receipt_records_the_error(session: AsyncSession) -> None:
    await seed_agent(session)
    claim = await claim_inbound(session, inbound("hello"))
    assert claim is not None
    await process_claimed(
        session,
        FakeLLM(text_turn("hi")),
        InMemoryChannel(Channel.WHATSAPP),
        claim,
        whatsapp_settings(),
        now=NOW,
    )

    await apply_status_updates(
        session,
        [
            StatusUpdate(
                external_id="mem-1",
                status=MessageStatus.FAILED,
                error="Re-engagement message",
            )
        ],
    )

    outbound = (
        await session.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND))
    ).scalar_one()
    assert outbound.status is MessageStatus.FAILED
    assert outbound.meta["delivery_error"] == "Re-engagement message"


async def test_claiming_records_consent_even_if_the_turn_never_runs(
    session: AsyncSession,
) -> None:
    """Opt-in is a fact about the lead's action, not about our processing."""
    await seed_agent(session)

    claim = await claim_inbound(session, inbound("hello"))

    assert claim is not None
    lead = await session.get_one(Lead, claim.lead_id)
    assert lead.consent_status is ConsentStatus.OPTED_IN


async def test_claiming_does_not_resurrect_an_opted_out_lead(session: AsyncSession) -> None:
    agent = await seed_agent(session)
    session.add(
        Lead(
            agent_id=agent.id,
            phone="+919876543210",
            source="whatsapp",
            consent_status=ConsentStatus.OPTED_OUT,
        )
    )
    await session.flush()

    claim = await claim_inbound(session, inbound("hello again"))

    assert claim is not None
    lead = await session.get_one(Lead, claim.lead_id)
    assert lead.consent_status is ConsentStatus.OPTED_OUT
