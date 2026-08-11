"""Slot generation from an agent's working hours.

M2 computes availability from working hours minus already-booked appointments.
M4 replaces the busy-slot source with Google Calendar free/busy; the slot maths
and the tool contract stay the same.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import Agent, Appointment
from app.models.enums import AppointmentStatus, AppointmentType
from app.services.google_calendar import (
    GoogleAuthRevokedError,
    GoogleCalendarClient,
    GoogleCalendarError,
)

log = get_logger(__name__)

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

SLOT_MINUTES = {
    AppointmentType.CALL: 30,
    AppointmentType.SITE_VISIT: 60,
}
# Don't offer a slot that starts sooner than this.
MIN_LEAD_TIME = timedelta(hours=2)


@dataclass(frozen=True)
class Slot:
    starts_at: datetime
    ends_at: datetime

    def label(self, tz: ZoneInfo) -> str:
        local = self.starts_at.astimezone(tz)
        return local.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def resolve_timezone(name: str | None, fallback: str = "Asia/Kolkata") -> ZoneInfo:
    try:
        return ZoneInfo(name or fallback)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(fallback)


def _parse_hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute or 0))


def _day_windows(agent: Agent, day: date, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    hours = agent.working_hours.get(WEEKDAY_KEYS[day.weekday()]) or []
    if len(hours) != 2:
        return []
    start, end = (_parse_hhmm(h) for h in hours)
    if start >= end:
        return []
    return [
        (
            datetime.combine(day, start, tzinfo=tz).astimezone(UTC),
            datetime.combine(day, end, tzinfo=tz).astimezone(UTC),
        )
    ]


async def appointment_intervals(
    session: AsyncSession, agent_id: uuid.UUID, window_start: datetime, window_end: datetime
) -> list[tuple[datetime, datetime]]:
    """Appointments we booked ourselves."""
    result = await session.execute(
        select(Appointment).where(
            Appointment.agent_id == agent_id,
            Appointment.status.in_([AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED]),
            Appointment.ends_at > window_start,
            Appointment.starts_at < window_end,
        )
    )
    return [(a.starts_at, a.ends_at) for a in result.scalars().all()]


async def busy_intervals(
    session: AsyncSession,
    agent: Agent,
    window_start: datetime,
    window_end: datetime,
    calendar: GoogleCalendarClient | None = None,
    now: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """Everything that blocks a slot: our appointments plus the agent's calendar.

    A Google outage must not stop the agent taking bookings, so a failed free/busy
    lookup degrades to our own appointments rather than raising. The cost is that
    we may offer a slot the agent has privately blocked — a double-booking they
    can decline, which is better than being unable to book at all.
    """
    busy = await appointment_intervals(session, agent.id, window_start, window_end)

    if calendar is None or not (agent.google_refresh_token and agent.google_calendar_id):
        return busy

    try:
        busy.extend(
            await calendar.free_busy(
                agent.id,
                agent.google_refresh_token,
                agent.google_calendar_id,
                window_start,
                window_end,
                now=now,
            )
        )
    except GoogleAuthRevokedError:
        # Surfaced to the agent by the dashboard (M6); do not block bookings now.
        log.warning("agent %s must reconnect Google Calendar — access was revoked", agent.id)
    except GoogleCalendarError as exc:
        log.warning("free/busy lookup failed for agent %s: %s", agent.id, exc)

    return busy


def _overlaps(slot: Slot, busy: list[tuple[datetime, datetime]]) -> bool:
    return any(slot.starts_at < end and start < slot.ends_at for start, end in busy)


async def find_available_slots(
    session: AsyncSession,
    agent: Agent,
    *,
    appointment_type: AppointmentType = AppointmentType.CALL,
    search_days: int = 7,
    limit: int = 6,
    now: datetime | None = None,
    calendar: GoogleCalendarClient | None = None,
) -> list[Slot]:
    """Open slots in the agent's working hours over the next `search_days` days."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    tz = resolve_timezone(agent.timezone)
    duration = timedelta(minutes=SLOT_MINUTES[appointment_type])
    earliest = now + MIN_LEAD_TIME

    window_end = now + timedelta(days=search_days)
    busy = await busy_intervals(session, agent, now, window_end, calendar=calendar, now=now)

    slots: list[Slot] = []
    for offset in range(search_days):
        day = (now.astimezone(tz) + timedelta(days=offset)).date()
        for window_start, window_end_local in _day_windows(agent, day, tz):
            cursor = max(window_start, earliest)
            # Align to the next half hour so offered times read naturally.
            if cursor.minute % 30 or cursor.second or cursor.microsecond:
                cursor = (cursor + timedelta(minutes=30 - cursor.minute % 30)).replace(
                    second=0, microsecond=0
                )
            while cursor + duration <= window_end_local:
                slot = Slot(cursor, cursor + duration)
                if not _overlaps(slot, busy):
                    slots.append(slot)
                    if len(slots) >= limit:
                        return slots
                cursor += duration
    return slots
