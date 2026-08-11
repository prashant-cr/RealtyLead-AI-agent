"""Dashboard API — the human agent's view of their pipeline.

Every query is scoped to the authenticated agent in its WHERE clause rather than
filtered after the fact, so there is no code path that loads another agent's lead
and then decides whether to return it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth import CurrentAgent
from app.api.schemas import (
    ActionResult,
    AppointmentOut,
    FollowUpOut,
    LeadDetail,
    LeadPage,
    LeadSummary,
    MessageOut,
    PipelineStats,
    SendMessageRequest,
    TakeoverRequest,
    TranscriptOut,
)
from app.channels.base import OutboundMessage
from app.channels.whatsapp import WhatsAppChannel, WhatsAppError, within_service_window
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.logging import get_logger, mask_phone
from app.models import Agent, Appointment, Conversation, Lead, Message
from app.models.enums import (
    AppointmentStatus,
    Channel,
    ConversationStatus,
    LeadStatus,
    LeadTemperature,
    MessageDirection,
    MessageRole,
    MessageStatus,
)
from app.services.followups import cancel_pending, schedule_next

router = APIRouter(prefix="/api", tags=["dashboard"])
log = get_logger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# Leads the agent should look at first: a human was asked for, or the AI gave up.
ATTENTION_STATUSES = (LeadStatus.HANDED_OFF,)


async def _get_lead(session: AsyncSession, agent: Agent, lead_id: uuid.UUID) -> Lead:
    lead = (
        await session.execute(select(Lead).where(Lead.id == lead_id, Lead.agent_id == agent.id))
    ).scalar_one_or_none()
    if lead is None:
        # 404 rather than 403: an agent should not be able to probe for the
        # existence of another agent's leads.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lead not found")
    return lead


async def _latest_conversation(session: AsyncSession, lead_id: uuid.UUID) -> Conversation | None:
    return (
        await session.execute(
            select(Conversation)
            .where(Conversation.lead_id == lead_id)
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("/me")
async def me(agent: CurrentAgent) -> dict[str, str | None]:
    """Who the token belongs to — the dashboard shows this in the header."""
    return {
        "id": str(agent.id),
        "name": agent.name,
        "brokerage_name": agent.brokerage_name,
        "timezone": agent.timezone,
        "calendar_connected": "yes" if agent.google_refresh_token else "no",
        "whatsapp_connected": "yes" if agent.whatsapp_phone_number_id else "no",
    }


@router.get("/stats", response_model=PipelineStats)
async def stats(agent: CurrentAgent, session: SessionDep) -> PipelineStats:
    rows = (
        await session.execute(
            select(Lead.status, func.count()).where(Lead.agent_id == agent.id).group_by(Lead.status)
        )
    ).all()
    by_status = {str(status_value.value): count for status_value, count in rows}

    temp_rows = (
        await session.execute(
            select(Lead.temperature, func.count())
            .where(Lead.agent_id == agent.id)
            .group_by(Lead.temperature)
        )
    ).all()
    by_temperature = {str(temp.value): count for temp, count in temp_rows}

    upcoming = (
        await session.execute(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.agent_id == agent.id,
                Appointment.starts_at >= datetime.now(UTC),
                Appointment.status.in_([AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED]),
            )
        )
    ).scalar_one()

    return PipelineStats(
        total=sum(by_status.values()),
        by_status=by_status,
        by_temperature=by_temperature,
        needs_attention=sum(by_status.get(s.value, 0) for s in ATTENTION_STATUSES),
        booked_upcoming=int(upcoming),
    )


@router.get("/leads", response_model=LeadPage)
async def list_leads(
    agent: CurrentAgent,
    session: SessionDep,
    lead_status: Annotated[list[LeadStatus] | None, Query(alias="status")] = None,
    temperature: Annotated[list[LeadTemperature] | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeadPage:
    """The pipeline board, newest activity first."""

    def scoped(stmt: Select[Any]) -> Select[Any]:
        stmt = stmt.where(Lead.agent_id == agent.id)
        if lead_status:
            stmt = stmt.where(Lead.status.in_(lead_status))
        if temperature:
            stmt = stmt.where(Lead.temperature.in_(temperature))
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(Lead.name.ilike(pattern) | Lead.phone.ilike(pattern))
        return stmt

    total = (await session.execute(scoped(select(func.count()).select_from(Lead)))).scalar_one()

    items = (
        (
            await session.execute(
                scoped(select(Lead))
                # Hot leads first, then by most recent activity.
                .order_by(Lead.score.desc(), Lead.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return LeadPage(
        items=[LeadSummary.model_validate(lead) for lead in items],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/leads/{lead_id}", response_model=LeadDetail)
async def lead_detail(lead_id: uuid.UUID, agent: CurrentAgent, session: SessionDep) -> LeadDetail:
    # Eager-load the collections: LeadDetail reads `appointments` off the model, and
    # a lazy relationship would fail outside the async context (and cost extra queries).
    lead = (
        await session.execute(
            select(Lead)
            .options(selectinload(Lead.appointments), selectinload(Lead.follow_up_tasks))
            .where(Lead.id == lead_id, Lead.agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lead not found")

    conversation = await _latest_conversation(session, lead.id)

    detail = LeadDetail.model_validate(lead)
    detail.appointments = [
        AppointmentOut.model_validate(a)
        for a in sorted(lead.appointments, key=lambda a: a.starts_at)
    ]
    detail.follow_ups = [
        FollowUpOut.model_validate(f)
        for f in sorted(lead.follow_up_tasks, key=lambda f: f.scheduled_for)
    ]
    if conversation is not None:
        detail.conversation_id = conversation.id
        detail.conversation_status = conversation.status
    return detail


@router.get("/leads/{lead_id}/transcript", response_model=TranscriptOut)
async def transcript(lead_id: uuid.UUID, agent: CurrentAgent, session: SessionDep) -> TranscriptOut:
    lead = await _get_lead(session, agent, lead_id)
    conversation = await _latest_conversation(session, lead.id)
    if conversation is None:
        return TranscriptOut(conversation_id=None, status=None, channel=None, messages=[])

    messages = (
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at, Message.id)
            )
        )
        .scalars()
        .all()
    )

    return TranscriptOut(
        conversation_id=conversation.id,
        status=conversation.status,
        channel=conversation.channel,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.post("/leads/{lead_id}/takeover", response_model=ActionResult)
async def take_over(
    lead_id: uuid.UUID,
    body: TakeoverRequest,
    agent: CurrentAgent,
    session: SessionDep,
) -> ActionResult:
    """Stop the AI replying so the agent can handle this lead themselves."""
    lead = await _get_lead(session, agent, lead_id)
    now = datetime.now(UTC)

    # A lead with no conversation yet can still be claimed — that is how an agent
    # says "don't let the assistant answer this one" before the lead writes in.
    conversation = await _latest_conversation(session, lead.id)
    if conversation is not None:
        conversation.status = ConversationStatus.HUMAN_TAKEOVER
    lead.status = LeadStatus.HANDED_OFF
    lead.handed_off_at = now
    lead.handoff_reason = (body.reason or "taken over from the dashboard")[:255]
    # Silence the nudges too — nothing is more jarring than an automated
    # follow-up landing while a human is mid-conversation.
    cancelled = await cancel_pending(session, lead.id, "human took over from the dashboard")
    await session.flush()

    log.info("agent %s took over lead %s (cancelled %s nudges)", agent.id, lead.id, cancelled)
    return ActionResult(
        ok=True,
        lead_status=lead.status,
        conversation_status=conversation.status if conversation else None,
        detail=f"You are now handling this lead. {cancelled} scheduled nudge(s) cancelled.",
    )


@router.post("/leads/{lead_id}/release", response_model=ActionResult)
async def release(
    lead_id: uuid.UUID,
    agent: CurrentAgent,
    session: SessionDep,
    settings: SettingsDep,
) -> ActionResult:
    """Hand the conversation back to the AI."""
    lead = await _get_lead(session, agent, lead_id)

    conversation = await _latest_conversation(session, lead.id)
    under_takeover = lead.status is LeadStatus.HANDED_OFF or (
        conversation is not None and conversation.status is ConversationStatus.HUMAN_TAKEOVER
    )
    if not under_takeover:
        raise HTTPException(status.HTTP_409_CONFLICT, "this lead is not under takeover")

    if lead.consent_status.value == "opted_out":
        # Releasing must never resurrect a lead who asked us to stop.
        raise HTTPException(status.HTTP_409_CONFLICT, "this lead has opted out")

    if conversation is not None:
        conversation.status = ConversationStatus.ACTIVE
    lead.status = LeadStatus.ENGAGED
    lead.handed_off_at = None
    lead.handoff_reason = None
    await session.flush()
    await schedule_next(
        session,
        lead,
        settings=settings,
        channel=conversation.channel if conversation else Channel.WHATSAPP,
    )

    log.info("agent %s released lead %s back to the assistant", agent.id, lead.id)
    return ActionResult(
        ok=True,
        lead_status=lead.status,
        conversation_status=conversation.status if conversation else None,
        detail="The assistant will handle replies again.",
    )


@router.post("/leads/{lead_id}/messages", response_model=MessageOut)
async def send_message(
    lead_id: uuid.UUID,
    body: SendMessageRequest,
    agent: CurrentAgent,
    session: SessionDep,
    settings: SettingsDep,
) -> MessageOut:
    """Send a message to the lead as the human agent."""
    lead = await _get_lead(session, agent, lead_id)
    now = datetime.now(UTC)

    if lead.consent_status.value == "opted_out":
        raise HTTPException(status.HTTP_409_CONFLICT, "this lead has opted out")

    conversation = await _latest_conversation(session, lead.id)
    if conversation is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "this lead has no conversation yet")

    if not within_service_window(lead.last_inbound_at, now, settings.whatsapp_service_window_hours):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"WhatsApp only allows free-form replies within "
            f"{settings.whatsapp_service_window_hours}h of the lead's last message. "
            "Call them instead, or wait for them to write.",
        )

    message = Message(
        conversation_id=conversation.id,
        role=MessageRole.HUMAN_AGENT,
        direction=MessageDirection.OUTBOUND,
        channel=conversation.channel,
        status=MessageStatus.PENDING,
        content=body.text,
        sent_at=now,
    )
    session.add(message)
    await session.flush()

    delivery = None
    if agent.whatsapp_phone_number_id:
        adapter = WhatsAppChannel(agent.whatsapp_phone_number_id, settings)
        try:
            delivery = await adapter.send(
                OutboundMessage(
                    channel=conversation.channel,
                    recipient=lead.phone,
                    text=body.text,
                    lead_id=lead.id,
                )
            )
        except WhatsAppError as exc:
            log.error("dashboard send to %s failed: %s", mask_phone(lead.phone), exc)
        finally:
            await adapter.close()

    if delivery is not None and delivery.accepted:
        message.status = MessageStatus.SENT
        message.external_id = delivery.external_id
        lead.last_outbound_at = now
    else:
        message.status = MessageStatus.FAILED

    conversation.last_message_at = now
    await session.flush()

    if message.status is MessageStatus.FAILED:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "the message was saved to the transcript but WhatsApp rejected it",
        )

    return MessageOut.model_validate(message)
