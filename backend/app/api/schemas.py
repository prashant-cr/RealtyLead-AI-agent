"""Response and request shapes for the dashboard API.

The dashboard is the agent's own view of their own leads, so contact details are
returned in full — they need the number to make the call. Logs still mask.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    AppointmentStatus,
    AppointmentType,
    Channel,
    ConsentStatus,
    ConversationStatus,
    Language,
    LeadPurpose,
    LeadStatus,
    LeadTemperature,
    MessageDirection,
    MessageRole,
    MessageStatus,
    PropertyType,
)


class ScoreReasonOut(BaseModel):
    factor: str
    points: int
    detail: str


class LeadSummary(BaseModel):
    """One row in the pipeline board."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str | None
    phone: str
    status: LeadStatus
    temperature: LeadTemperature
    score: int
    language: Language
    source: str
    budget_min: Decimal | None
    budget_max: Decimal | None
    preferred_locations: list[str]
    bhk: int | None
    timeline_months: int | None
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    created_at: datetime
    consent_status: ConsentStatus
    follow_up_count: int
    handoff_reason: str | None


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    appointment_type: AppointmentType
    status: AppointmentStatus
    starts_at: datetime
    ends_at: datetime
    timezone: str
    location: str | None
    notes: str | None
    listing_id: uuid.UUID | None
    google_event_id: str | None


class FollowUpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    attempt_number: int
    scheduled_for: datetime
    status: str
    template_name: str | None
    sent_at: datetime | None
    outcome_reason: str | None


class LeadDetail(LeadSummary):
    """Everything the agent needs before picking up the phone."""

    email: str | None
    property_type: PropertyType | None
    purpose: LeadPurpose
    loan_preapproved: bool | None
    site_visit_willing: bool | None
    notes: str | None
    scored_at: datetime | None
    handed_off_at: datetime | None
    opted_out_at: datetime | None
    timezone: str | None
    score_reasons: list[ScoreReasonOut] = Field(default_factory=list)
    appointments: list[AppointmentOut] = Field(default_factory=list)
    follow_ups: list[FollowUpOut] = Field(default_factory=list)
    conversation_id: uuid.UUID | None = None
    conversation_status: ConversationStatus | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    direction: MessageDirection
    channel: Channel
    status: MessageStatus
    content: str
    media_urls: list[str]
    created_at: datetime
    sent_at: datetime | None


class TranscriptOut(BaseModel):
    conversation_id: uuid.UUID | None
    status: ConversationStatus | None
    channel: Channel | None
    messages: list[MessageOut] = Field(default_factory=list)


class PipelineStats(BaseModel):
    """Counts for the board header."""

    total: int
    by_status: dict[str, int]
    by_temperature: dict[str, int]
    needs_attention: int
    booked_upcoming: int


class LeadPage(BaseModel):
    items: list[LeadSummary]
    total: int
    limit: int
    offset: int


class TakeoverRequest(BaseModel):
    reason: str | None = Field(
        default=None, max_length=255, description="Why the agent is stepping in."
    )


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ActionResult(BaseModel):
    ok: bool
    lead_status: LeadStatus
    conversation_status: ConversationStatus | None = None
    detail: str | None = None
