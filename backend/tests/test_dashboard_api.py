"""Dashboard API: auth, tenant isolation, pipeline, transcripts, takeover."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import generate_token, hash_token
from app.core.config import Settings
from app.models import Appointment, Conversation, FollowUpTask, Message
from app.models.enums import (
    AppointmentType,
    Channel,
    ConsentStatus,
    ConversationStatus,
    FollowUpStatus,
    LeadStatus,
    LeadTemperature,
    MessageDirection,
    MessageRole,
    MessageStatus,
)
from tests.factories import make_agent, make_lead

ClientFactory = Callable[..., Awaitable[AsyncClient]]
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def dash_settings(**overrides: object) -> Settings:
    return Settings(**{"whatsapp_access_token": "tok", **overrides})  # type: ignore[arg-type]


async def make_agent_with_token(session: AsyncSession, **overrides: object):
    token = generate_token()
    agent = make_agent(api_token_hash=hash_token(token), **overrides)
    session.add(agent)
    await session.flush()
    return agent, token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def make_full_lead(session: AsyncSession, agent, **overrides: object):
    defaults: dict[str, object] = {
        "name": "Priya Shah",
        "phone": "+919876543210",
        "status": LeadStatus.QUALIFIED,
        "temperature": LeadTemperature.HOT,
        "score": 80,
        "budget_min": Decimal("6000000"),
        "budget_max": Decimal("9000000"),
        "preferred_locations": ["Bopal"],
        "bhk": 3,
        "timeline_months": 2,
        "consent_status": ConsentStatus.OPTED_IN,
        "last_inbound_at": NOW,
        "score_reasons": [{"factor": "timeline", "points": 25, "detail": "Buying in 2 months"}],
    }
    lead = make_lead(agent, **{**defaults, **overrides})
    session.add(lead)
    await session.flush()

    conversation = Conversation(lead_id=lead.id, channel=Channel.WHATSAPP)
    session.add(conversation)
    await session.flush()
    session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                role=MessageRole.LEAD,
                direction=MessageDirection.INBOUND,
                channel=Channel.WHATSAPP,
                status=MessageStatus.RECEIVED,
                content="Is the Bopal flat available?",
            ),
            Message(
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT,
                direction=MessageDirection.OUTBOUND,
                channel=Channel.WHATSAPP,
                status=MessageStatus.SENT,
                content="Yes! What's your budget?",
            ),
        ]
    )
    await session.flush()
    return lead, conversation


@pytest.fixture
async def dash(client_factory: ClientFactory) -> AsyncClient:
    return await client_factory(dash_settings())


# ---------------------------------------------------------------------- auth


async def test_endpoints_require_a_token(dash: AsyncClient) -> None:
    for path in ("/api/me", "/api/stats", "/api/leads"):
        response = await dash.get(path)
        assert response.status_code == 401, path


async def test_invalid_token_is_rejected(dash: AsyncClient, session: AsyncSession) -> None:
    await make_agent_with_token(session)

    response = await dash.get("/api/leads", headers=auth("rl_not-a-real-token"))

    assert response.status_code == 401


async def test_valid_token_identifies_the_agent(dash: AsyncClient, session: AsyncSession) -> None:
    agent, token = await make_agent_with_token(session)

    response = await dash.get("/api/me", headers=auth(token))

    assert response.status_code == 200
    assert response.json()["name"] == agent.name


async def test_deactivated_agent_cannot_authenticate(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent, token = await make_agent_with_token(session)
    agent.is_active = False
    await session.flush()

    assert (await dash.get("/api/me", headers=auth(token))).status_code == 401


async def test_token_is_stored_only_as_a_hash(session: AsyncSession) -> None:
    agent, token = await make_agent_with_token(session)

    assert agent.api_token_hash != token
    assert agent.api_token_hash == hash_token(token)
    assert len(agent.api_token_hash or "") == 64


# ------------------------------------------------------------ tenant isolation


async def test_agents_only_see_their_own_leads(dash: AsyncClient, session: AsyncSession) -> None:
    agent_a, token_a = await make_agent_with_token(session)
    agent_b, _ = await make_agent_with_token(
        session, email="other@example.com", phone="+919876500002"
    )
    await make_full_lead(session, agent_a, name="Mine")
    await make_full_lead(session, agent_b, name="Theirs", phone="+919812345678")

    response = await dash.get("/api/leads", headers=auth(token_a))

    names = [item["name"] for item in response.json()["items"]]
    assert names == ["Mine"]


async def test_another_agents_lead_is_a_404_not_a_403(
    dash: AsyncClient, session: AsyncSession
) -> None:
    """403 would confirm the lead exists — a 404 leaks nothing."""
    _, token_a = await make_agent_with_token(session)
    agent_b, _ = await make_agent_with_token(
        session, email="other@example.com", phone="+919876500002"
    )
    lead_b, _ = await make_full_lead(session, agent_b, phone="+919812345678")

    for path in ("", "/transcript"):
        response = await dash.get(f"/api/leads/{lead_b.id}{path}", headers=auth(token_a))
        assert response.status_code == 404, path


async def test_cannot_take_over_another_agents_lead(
    dash: AsyncClient, session: AsyncSession
) -> None:
    _, token_a = await make_agent_with_token(session)
    agent_b, _ = await make_agent_with_token(
        session, email="other@example.com", phone="+919876500002"
    )
    lead_b, _ = await make_full_lead(session, agent_b, phone="+919812345678")

    response = await dash.post(
        f"/api/leads/{lead_b.id}/takeover", json={"reason": "mine now"}, headers=auth(token_a)
    )

    assert response.status_code == 404
    await session.refresh(lead_b)
    assert lead_b.status is not LeadStatus.HANDED_OFF


async def test_stats_only_count_the_agents_own_leads(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent_a, token_a = await make_agent_with_token(session)
    agent_b, _ = await make_agent_with_token(
        session, email="other@example.com", phone="+919876500002"
    )
    await make_full_lead(session, agent_a)
    await make_full_lead(session, agent_b, phone="+919812345678")
    await make_full_lead(session, agent_b, phone="+919812345679")

    stats = (await dash.get("/api/stats", headers=auth(token_a))).json()

    assert stats["total"] == 1


# ------------------------------------------------------------------ pipeline


async def test_pipeline_returns_the_lead_summary(dash: AsyncClient, session: AsyncSession) -> None:
    agent, token = await make_agent_with_token(session)
    await make_full_lead(session, agent)

    page = (await dash.get("/api/leads", headers=auth(token))).json()

    assert page["total"] == 1
    item = page["items"][0]
    assert item["name"] == "Priya Shah"
    assert item["score"] == 80
    assert item["temperature"] == "hot"
    assert item["phone"] == "+919876543210"  # the agent needs the real number


async def test_pipeline_sorts_hottest_first(dash: AsyncClient, session: AsyncSession) -> None:
    agent, token = await make_agent_with_token(session)
    await make_full_lead(session, agent, phone="+919000000001", name="Cold", score=10)
    await make_full_lead(session, agent, phone="+919000000002", name="Hot", score=90)
    await make_full_lead(session, agent, phone="+919000000003", name="Warm", score=50)

    page = (await dash.get("/api/leads", headers=auth(token))).json()

    assert [i["name"] for i in page["items"]] == ["Hot", "Warm", "Cold"]


async def test_pipeline_filters_by_status(dash: AsyncClient, session: AsyncSession) -> None:
    agent, token = await make_agent_with_token(session)
    await make_full_lead(session, agent, phone="+919000000001", status=LeadStatus.NEW)
    await make_full_lead(session, agent, phone="+919000000002", status=LeadStatus.BOOKED)

    page = (await dash.get("/api/leads?status=booked", headers=auth(token))).json()

    assert page["total"] == 1
    assert page["items"][0]["status"] == "booked"


async def test_pipeline_search_matches_name_or_phone(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent, token = await make_agent_with_token(session)
    await make_full_lead(session, agent, phone="+919000000001", name="Priya Shah")
    await make_full_lead(session, agent, phone="+919000000002", name="Amit Patel")

    by_name = (await dash.get("/api/leads?search=Amit", headers=auth(token))).json()
    by_phone = (await dash.get("/api/leads?search=0000001", headers=auth(token))).json()

    assert [i["name"] for i in by_name["items"]] == ["Amit Patel"]
    assert [i["name"] for i in by_phone["items"]] == ["Priya Shah"]


async def test_pipeline_pagination(dash: AsyncClient, session: AsyncSession) -> None:
    agent, token = await make_agent_with_token(session)
    for i in range(5):
        await make_full_lead(session, agent, phone=f"+91900000000{i}", score=90 - i)

    page = (await dash.get("/api/leads?limit=2&offset=2", headers=auth(token))).json()

    assert page["total"] == 5
    assert len(page["items"]) == 2
    assert page["offset"] == 2


# -------------------------------------------------------------------- detail


async def test_detail_includes_score_reasons_and_history(
    dash: AsyncClient, session: AsyncSession
) -> None:
    """The dashboard must show *why* a lead scored what it did."""
    agent, token = await make_agent_with_token(session)
    lead, _ = await make_full_lead(session, agent)
    session.add(
        Appointment(
            lead_id=lead.id,
            agent_id=agent.id,
            appointment_type=AppointmentType.SITE_VISIT,
            starts_at=NOW + timedelta(days=1),
            ends_at=NOW + timedelta(days=1, hours=1),
        )
    )
    session.add(
        FollowUpTask(
            lead_id=lead.id,
            attempt_number=1,
            scheduled_for=NOW + timedelta(days=1),
            status=FollowUpStatus.SCHEDULED,
        )
    )
    await session.flush()

    detail = (await dash.get(f"/api/leads/{lead.id}", headers=auth(token))).json()

    assert detail["score_reasons"][0]["factor"] == "timeline"
    assert detail["score_reasons"][0]["detail"] == "Buying in 2 months"
    assert len(detail["appointments"]) == 1
    assert len(detail["follow_ups"]) == 1
    assert detail["conversation_status"] == "active"


async def test_unknown_lead_is_a_404(dash: AsyncClient, session: AsyncSession) -> None:
    _, token = await make_agent_with_token(session)

    response = await dash.get(f"/api/leads/{uuid.uuid4()}", headers=auth(token))

    assert response.status_code == 404


# ---------------------------------------------------------------- transcript


async def test_transcript_returns_messages_in_order(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent, token = await make_agent_with_token(session)
    lead, _ = await make_full_lead(session, agent)

    body = (await dash.get(f"/api/leads/{lead.id}/transcript", headers=auth(token))).json()

    assert [m["direction"] for m in body["messages"]] == ["inbound", "outbound"]
    assert body["messages"][0]["content"] == "Is the Bopal flat available?"


async def test_transcript_for_a_lead_with_no_conversation_is_empty(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent, token = await make_agent_with_token(session)
    lead = make_lead(agent, phone="+919000000009")
    session.add(lead)
    await session.flush()

    body = (await dash.get(f"/api/leads/{lead.id}/transcript", headers=auth(token))).json()

    assert body["messages"] == []
    assert body["conversation_id"] is None


# ------------------------------------------------------------------ takeover


async def test_takeover_stops_the_assistant_and_the_nudges(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent, token = await make_agent_with_token(session)
    lead, conversation = await make_full_lead(session, agent)
    session.add(
        FollowUpTask(
            lead_id=lead.id,
            attempt_number=1,
            scheduled_for=NOW + timedelta(days=1),
            status=FollowUpStatus.SCHEDULED,
        )
    )
    await session.flush()

    response = await dash.post(
        f"/api/leads/{lead.id}/takeover",
        json={"reason": "I know this buyer"},
        headers=auth(token),
    )

    assert response.status_code == 200
    await session.refresh(lead)
    await session.refresh(conversation)
    assert lead.status is LeadStatus.HANDED_OFF
    assert lead.handoff_reason == "I know this buyer"
    assert conversation.status is ConversationStatus.HUMAN_TAKEOVER
    task = (await session.execute(select(FollowUpTask))).scalar_one()
    assert task.status is FollowUpStatus.CANCELLED


async def test_release_hands_the_conversation_back(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent, token = await make_agent_with_token(session)
    lead, conversation = await make_full_lead(session, agent)
    await dash.post(f"/api/leads/{lead.id}/takeover", json={}, headers=auth(token))

    response = await dash.post(f"/api/leads/{lead.id}/release", headers=auth(token))

    assert response.status_code == 200
    await session.refresh(lead)
    await session.refresh(conversation)
    assert lead.status is LeadStatus.ENGAGED
    assert lead.handoff_reason is None
    assert conversation.status is ConversationStatus.ACTIVE


async def test_release_without_takeover_is_a_conflict(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent, token = await make_agent_with_token(session)
    lead, _ = await make_full_lead(session, agent)

    response = await dash.post(f"/api/leads/{lead.id}/release", headers=auth(token))

    assert response.status_code == 409


async def test_release_never_resurrects_an_opted_out_lead(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent, token = await make_agent_with_token(session)
    lead, conversation = await make_full_lead(session, agent)
    await dash.post(f"/api/leads/{lead.id}/takeover", json={}, headers=auth(token))
    lead.consent_status = ConsentStatus.OPTED_OUT
    await session.flush()

    response = await dash.post(f"/api/leads/{lead.id}/release", headers=auth(token))

    assert response.status_code == 409
    await session.refresh(conversation)
    assert conversation.status is ConversationStatus.HUMAN_TAKEOVER


# -------------------------------------------------------------- manual send


async def test_sending_to_an_opted_out_lead_is_refused(
    dash: AsyncClient, session: AsyncSession
) -> None:
    agent, token = await make_agent_with_token(session)
    lead, _ = await make_full_lead(session, agent, consent_status=ConsentStatus.OPTED_OUT)

    response = await dash.post(
        f"/api/leads/{lead.id}/messages", json={"text": "hello"}, headers=auth(token)
    )

    assert response.status_code == 409


async def test_sending_outside_the_service_window_is_refused(
    dash: AsyncClient, session: AsyncSession
) -> None:
    """WhatsApp would reject it; better to say so than to fail silently."""
    agent, token = await make_agent_with_token(session)
    lead, _ = await make_full_lead(
        session, agent, last_inbound_at=datetime.now(UTC) - timedelta(hours=30)
    )

    response = await dash.post(
        f"/api/leads/{lead.id}/messages", json={"text": "hello"}, headers=auth(token)
    )

    assert response.status_code == 409
    assert "24h" in response.json()["detail"] or "free-form" in response.json()["detail"]


async def test_empty_message_is_rejected(dash: AsyncClient, session: AsyncSession) -> None:
    agent, token = await make_agent_with_token(session)
    lead, _ = await make_full_lead(session, agent, last_inbound_at=datetime.now(UTC))

    response = await dash.post(
        f"/api/leads/{lead.id}/messages", json={"text": ""}, headers=auth(token)
    )

    assert response.status_code == 422


async def test_a_lead_with_no_conversation_can_still_be_claimed(
    dash: AsyncClient, session: AsyncSession
) -> None:
    """An agent can say "I'll handle this one" before the lead ever writes in."""
    agent, token = await make_agent_with_token(session)
    lead = make_lead(agent, phone="+919000000099")
    session.add(lead)
    await session.flush()

    response = await dash.post(f"/api/leads/{lead.id}/takeover", json={}, headers=auth(token))

    assert response.status_code == 200
    await session.refresh(lead)
    assert lead.status is LeadStatus.HANDED_OFF

    released = await dash.post(f"/api/leads/{lead.id}/release", headers=auth(token))
    assert released.status_code == 200
