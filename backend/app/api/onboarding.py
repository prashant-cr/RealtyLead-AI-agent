"""Onboarding: settings, listing import, and the checklist that ties them together.

The product promise is "onboard in under 10 minutes", so this is the shortest
path from a new account to a working assistant: set your hours and tone, connect
WhatsApp, upload a CSV of listings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CurrentAgent
from app.core.db import get_session
from app.core.logging import get_logger
from app.models import Listing
from app.models.enums import Language, ListingStatus, PropertyType
from app.services.listing_import import SAMPLE_CSV, parse_csv
from app.services.scheduling import WEEKDAY_KEYS, resolve_timezone

router = APIRouter(prefix="/api", tags=["onboarding"])
log = get_logger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
MAX_TONE_LENGTH = 2000


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    email: str
    phone: str
    brokerage_name: str | None
    timezone: str
    languages: list[str]
    working_hours: dict[str, Any]
    quiet_hours_start: int
    quiet_hours_end: int
    tone_instructions: str | None
    escalation_budget_threshold: int | None
    whatsapp_phone_number_id: str | None
    calendar_connected: bool = False
    onboarded: bool = False


class SettingsUpdate(BaseModel):
    """Every field optional — the onboarding wizard saves one step at a time."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, min_length=5, max_length=20)
    brokerage_name: str | None = Field(default=None, max_length=160)
    timezone: str | None = Field(default=None, max_length=64)
    languages: list[Language] | None = None
    working_hours: dict[str, list[str]] | None = None
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)
    tone_instructions: str | None = Field(default=None, max_length=MAX_TONE_LENGTH)
    escalation_budget_threshold: int | None = Field(default=None, ge=0)
    whatsapp_phone_number_id: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # resolve_timezone falls back silently; here we want to reject typos so an
        # agent never sets a timezone that quietly behaves as Asia/Kolkata.
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{value!r} is not a known timezone") from exc
        return value

    @field_validator("working_hours")
    @classmethod
    def _valid_hours(cls, value: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
        if value is None:
            return None
        for day, hours in value.items():
            if day not in WEEKDAY_KEYS:
                raise ValueError(f"{day!r} is not a day (use {', '.join(WEEKDAY_KEYS)})")
            if hours == []:
                continue  # a closed day
            if len(hours) != 2:
                raise ValueError(f"{day}: give an open and close time, or [] if closed")
            for entry in hours:
                if not _looks_like_time(entry):
                    raise ValueError(f"{day}: {entry!r} should look like 09:30")
            if hours[0] >= hours[1]:
                raise ValueError(f"{day}: closing time must be after opening time")
        return value


def _looks_like_time(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


class ListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    property_type: PropertyType
    status: ListingStatus
    city: str
    locality: str | None
    price: Decimal
    bhk: int | None
    carpet_area_sqft: int | None
    rera_id: str | None
    is_active: bool


class RowErrorOut(BaseModel):
    line: int
    message: str


class ImportResultOut(BaseModel):
    imported: int
    replaced: int
    skipped_blank: int
    errors: list[RowErrorOut] = Field(default_factory=list)


class ChecklistStep(BaseModel):
    key: str
    label: str
    done: bool
    detail: str | None = None


class OnboardingStatus(BaseModel):
    complete: bool
    steps: list[ChecklistStep]


@router.get("/settings", response_model=SettingsOut)
async def get_settings_(agent: CurrentAgent) -> SettingsOut:
    out = SettingsOut.model_validate(agent)
    out.calendar_connected = agent.google_refresh_token is not None
    out.onboarded = agent.onboarded_at is not None
    return out


@router.patch("/settings", response_model=SettingsOut)
async def update_settings(
    body: SettingsUpdate, agent: CurrentAgent, session: SessionDep
) -> SettingsOut:
    data = body.model_dump(exclude_unset=True)

    for key in (
        "name",
        "phone",
        "brokerage_name",
        "timezone",
        "quiet_hours_start",
        "quiet_hours_end",
        "escalation_budget_threshold",
        "whatsapp_phone_number_id",
    ):
        if key in data:
            value = data[key]
            setattr(agent, key, value.strip() if isinstance(value, str) else value)

    if "languages" in data and data["languages"]:
        agent.languages = [Language(lang).value for lang in data["languages"]]
    if "working_hours" in data and data["working_hours"] is not None:
        agent.working_hours = data["working_hours"]
    if "tone_instructions" in data:
        tone = (data["tone_instructions"] or "").strip()
        agent.tone_instructions = tone or None

    await session.flush()
    log.info("agent %s updated settings: %s", agent.id, ", ".join(sorted(data)))
    return await get_settings_(agent)


@router.get("/listings", response_model=list[ListingOut])
async def list_listings(agent: CurrentAgent, session: SessionDep) -> list[ListingOut]:
    listings = (
        (
            await session.execute(
                select(Listing).where(Listing.agent_id == agent.id).order_by(Listing.price)
            )
        )
        .scalars()
        .all()
    )
    return [ListingOut.model_validate(listing) for listing in listings]


@router.get("/listings/sample.csv")
async def sample_csv() -> Response:
    """A file the agent can open in Excel, edit and upload straight back."""
    return Response(
        content=SAMPLE_CSV,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="realtylead-listings-sample.csv"'},
    )


@router.post("/listings/import", response_model=ImportResultOut)
async def import_listings(
    agent: CurrentAgent,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="CSV of listings")],
    replace: bool = False,
) -> ImportResultOut:
    """Import listings from a CSV.

    All-or-nothing: if any row fails to parse, nothing is written. A partially
    loaded inventory would make the assistant quote from half a catalogue, and
    the agent would have no way to tell.
    """
    content = await file.read()
    result = parse_csv(content, agent.id)

    if not result.ok:
        return ImportResultOut(
            imported=0,
            replaced=0,
            skipped_blank=result.skipped_blank,
            errors=[RowErrorOut(line=e.line, message=e.message) for e in result.errors],
        )

    replaced = 0
    if replace:
        deleted = await session.execute(delete(Listing).where(Listing.agent_id == agent.id))
        replaced = int(getattr(deleted, "rowcount", 0) or 0)

    session.add_all(result.listings)
    await session.flush()

    log.info(
        "agent %s imported %s listing(s)%s",
        agent.id,
        len(result.listings),
        f", replacing {replaced}" if replace else "",
    )
    return ImportResultOut(
        imported=len(result.listings),
        replaced=replaced,
        skipped_blank=result.skipped_blank,
    )


@router.get("/onboarding", response_model=OnboardingStatus)
async def onboarding_status(agent: CurrentAgent, session: SessionDep) -> OnboardingStatus:
    listing_count = (
        await session.execute(
            select(func.count()).select_from(Listing).where(Listing.agent_id == agent.id)
        )
    ).scalar_one()

    tz = resolve_timezone(agent.timezone)
    open_days = sum(
        1 for hours in agent.working_hours.values() if isinstance(hours, list) and hours
    )

    steps = [
        ChecklistStep(
            key="account",
            label="Create your account",
            done=True,
            detail=agent.email,
        ),
        ChecklistStep(
            key="hours",
            label="Set your working hours",
            done=open_days > 0,
            detail=f"{open_days} day(s) open · {tz.key}",
        ),
        ChecklistStep(
            key="listings",
            label="Add your listings",
            done=listing_count > 0,
            detail=f"{listing_count} listing(s)",
        ),
        ChecklistStep(
            key="whatsapp",
            label="Connect WhatsApp",
            done=agent.whatsapp_phone_number_id is not None,
            detail="Leads can message you" if agent.whatsapp_phone_number_id else None,
        ),
        ChecklistStep(
            key="calendar",
            label="Connect Google Calendar (optional)",
            done=agent.google_refresh_token is not None,
            detail="Bookings appear on your calendar"
            if agent.google_refresh_token
            else "Bookings still work without it",
        ),
        ChecklistStep(
            key="tone",
            label="Set the assistant's tone (optional)",
            done=bool(agent.tone_instructions),
            detail=None,
        ),
    ]

    # Optional steps do not gate completion — an agent with hours, listings and
    # WhatsApp can start taking leads today.
    required = {"account", "hours", "listings", "whatsapp"}
    complete = all(step.done for step in steps if step.key in required)

    if complete and agent.onboarded_at is None:
        agent.onboarded_at = datetime.now(UTC)
        await session.flush()
        log.info("agent %s completed onboarding", agent.id)

    return OnboardingStatus(complete=complete, steps=steps)


@router.delete("/listings/{listing_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_listing(listing_id: uuid.UUID, agent: CurrentAgent, session: SessionDep) -> None:
    listing = (
        await session.execute(
            select(Listing).where(Listing.id == listing_id, Listing.agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if listing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found")
    await session.delete(listing)
    await session.flush()
