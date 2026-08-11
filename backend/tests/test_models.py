from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, Conversation, FollowUpTask, Lead, Message
from app.models.enums import (
    AppointmentType,
    Channel,
    ConsentStatus,
    LeadStatus,
    LeadTemperature,
    MessageDirection,
    MessageRole,
)
from tests.factories import make_agent, make_lead, make_listing


async def test_agent_defaults_applied_on_flush(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()

    assert agent.id is not None
    assert agent.is_active is True
    assert agent.quiet_hours_start == 21
    assert agent.working_hours["sun"] == []
    assert agent.created_at is not None


async def test_lead_defaults_to_new_and_cold(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()

    lead = make_lead(agent)
    session.add(lead)
    await session.flush()

    assert lead.status is LeadStatus.NEW
    assert lead.temperature is LeadTemperature.COLD
    assert lead.score == 0
    assert lead.score_reasons == []
    assert lead.consent_status is ConsentStatus.UNKNOWN
    assert lead.is_contactable is True


async def test_opted_out_lead_is_not_contactable(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()

    lead = make_lead(agent, consent_status=ConsentStatus.OPTED_OUT, opted_out_at=datetime.now(UTC))
    session.add(lead)
    await session.flush()

    assert lead.is_contactable is False


async def test_duplicate_phone_per_agent_is_rejected(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()

    session.add(make_lead(agent, phone="+919812345678"))
    await session.flush()
    session.add(make_lead(agent, phone="+919812345678"))

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_same_phone_allowed_for_different_agents(session: AsyncSession) -> None:
    first = make_agent()
    second = make_agent(email="neha@bluekey.example", phone="+919876500002")
    session.add_all([first, second])
    await session.flush()

    session.add_all(
        [make_lead(first, phone="+919812345678"), make_lead(second, phone="+919812345678")]
    )
    await session.flush()

    leads = (await session.execute(select(Lead))).scalars().all()
    assert len(leads) == 2


async def test_duplicate_external_message_id_is_rejected(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()
    lead = make_lead(agent)
    session.add(lead)
    await session.flush()

    conversation = Conversation(lead_id=lead.id, channel=Channel.WHATSAPP)
    session.add(conversation)
    await session.flush()

    for _ in range(2):
        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.LEAD,
                direction=MessageDirection.INBOUND,
                channel=Channel.WHATSAPP,
                content="Is the Bopal flat still available?",
                external_id="wamid.abc123",
            )
        )

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_conversation_cascade_deletes_messages(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()
    lead = make_lead(agent)
    session.add(lead)
    await session.flush()

    conversation = Conversation(lead_id=lead.id, channel=Channel.WHATSAPP)
    conversation.messages.append(
        Message(
            role=MessageRole.ASSISTANT,
            direction=MessageDirection.OUTBOUND,
            channel=Channel.WHATSAPP,
            content="Hi! I'm Rohan's assistant at Sunrise Homes.",
        )
    )
    session.add(conversation)
    await session.flush()

    await session.delete(conversation)
    await session.flush()

    assert (await session.execute(select(Message))).scalars().all() == []


async def test_appointment_and_follow_up_link_to_lead(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()
    listing = make_listing(agent)
    lead = make_lead(agent)
    session.add_all([listing, lead])
    await session.flush()

    starts = datetime.now(UTC) + timedelta(days=1)
    session.add(
        Appointment(
            lead_id=lead.id,
            agent_id=agent.id,
            listing_id=listing.id,
            appointment_type=AppointmentType.SITE_VISIT,
            starts_at=starts,
            ends_at=starts + timedelta(minutes=45),
        )
    )
    session.add(
        FollowUpTask(lead_id=lead.id, attempt_number=1, scheduled_for=starts, template_name="d1")
    )
    await session.flush()
    await session.refresh(lead, ["appointments", "follow_up_tasks"])

    assert len(lead.appointments) == 1
    assert lead.appointments[0].listing_id == listing.id
    assert len(lead.follow_up_tasks) == 1


async def test_repr_never_leaks_pii(session: AsyncSession) -> None:
    agent = make_agent()
    session.add(agent)
    await session.flush()
    lead = make_lead(agent, name="Priya Shah", phone="+919876543210")
    session.add(lead)
    await session.flush()

    rendered = repr(lead)
    assert "Priya" not in rendered
    assert "9876543210" not in rendered
