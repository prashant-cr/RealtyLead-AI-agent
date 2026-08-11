"""Putting a booked appointment on the agent's calendar.

Ordering matters here. The appointment row is written first and the calendar
event second, because the lead is told the booking is confirmed either way —
and an appointment the lead believes exists but the agent never sees is the
worst possible outcome. So a calendar failure keeps the booking, records why,
and escalates to the human agent rather than silently dropping either one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, mask_phone
from app.models import Agent, Appointment, Lead, Listing
from app.models.enums import AppointmentType
from app.services.google_calendar import (
    GoogleAuthRevokedError,
    GoogleCalendarClient,
    GoogleCalendarError,
    GoogleNotConnectedError,
)

log = get_logger(__name__)

TYPE_LABELS = {
    AppointmentType.CALL: "Call",
    AppointmentType.SITE_VISIT: "Site visit",
}


@dataclass(frozen=True)
class SyncOutcome:
    synced: bool
    event_id: str | None = None
    html_link: str | None = None
    reason: str | None = None

    @property
    def needs_attention(self) -> bool:
        """True when the agent should be told their calendar is out of step."""
        return not self.synced and self.reason is not None


def event_summary(lead: Lead, appointment: Appointment, listing: Listing | None) -> str:
    who = lead.name or mask_phone(lead.phone)
    label = TYPE_LABELS[appointment.appointment_type]
    if listing is not None:
        return f"{label}: {who} — {listing.title}"
    return f"{label}: {who}"


def event_description(lead: Lead, appointment: Appointment, listing: Listing | None) -> str:
    """What the agent reads on their phone before walking into the meeting."""
    lines = [
        "Booked by the RealtyLead AI assistant.",
        "",
        f"Lead: {lead.name or 'name not given'}",
        f"Phone: {lead.phone}",
    ]
    if lead.email:
        lines.append(f"Email: {lead.email}")

    if lead.budget_min or lead.budget_max:
        low = f"{lead.budget_min:,.0f}" if lead.budget_min else "?"
        high = f"{lead.budget_max:,.0f}" if lead.budget_max else "?"
        lines.append(f"Budget: INR {low} - {high}")
    if lead.preferred_locations:
        lines.append(f"Preferred areas: {', '.join(lead.preferred_locations)}")
    if lead.bhk:
        lines.append(f"Size: {lead.bhk} BHK")
    if lead.timeline_months is not None:
        lines.append(f"Timeline: {lead.timeline_months} months")
    if lead.loan_preapproved is not None:
        lines.append(f"Loan pre-approved: {'yes' if lead.loan_preapproved else 'no'}")

    lines.append(f"Score: {lead.score}/100 ({lead.temperature.value})")
    for reason in lead.score_reasons:
        lines.append(f"  - {reason.get('factor')}: +{reason.get('points')} {reason.get('detail')}")

    if listing is not None:
        lines += [
            "",
            f"Property: {listing.title}",
            f"Price: INR {listing.price:,.0f}",
            f"Location: {', '.join(filter(None, [listing.locality, listing.city]))}",
        ]
        if listing.rera_id:
            lines.append(f"RERA: {listing.rera_id}")

    if appointment.notes:
        lines += ["", f"Notes: {appointment.notes}"]

    return "\n".join(lines)


async def sync_to_calendar(
    session: AsyncSession,
    calendar: GoogleCalendarClient | None,
    agent: Agent,
    lead: Lead,
    appointment: Appointment,
    listing: Listing | None = None,
    now: datetime | None = None,
) -> SyncOutcome:
    """Create the calendar event for an appointment that is already booked."""
    if calendar is None:
        return SyncOutcome(synced=False)
    if not (agent.google_refresh_token and agent.google_calendar_id):
        # Not an error: agents can run without connecting a calendar.
        return SyncOutcome(synced=False)

    location = None
    if appointment.appointment_type is AppointmentType.SITE_VISIT and listing is not None:
        location = ", ".join(filter(None, [listing.locality, listing.city]))

    try:
        event = await calendar.create_event(
            agent.id,
            agent.google_refresh_token,
            agent.google_calendar_id,
            summary=event_summary(lead, appointment, listing),
            description=event_description(lead, appointment, listing),
            start=appointment.starts_at,
            end=appointment.ends_at,
            timezone_name=agent.timezone,
            attendee_email=lead.email,
            location=location or appointment.location,
            now=now,
        )
    except GoogleAuthRevokedError:
        log.error("agent %s must reconnect Google Calendar; booking not synced", agent.id)
        return SyncOutcome(
            synced=False,
            reason="Google Calendar access was revoked — the booking is not on the calendar",
        )
    except GoogleNotConnectedError:
        return SyncOutcome(synced=False)
    except GoogleCalendarError as exc:
        log.error("could not create calendar event for appointment %s: %s", appointment.id, exc)
        return SyncOutcome(
            synced=False, reason="Google Calendar was unreachable — the booking is not on it"
        )

    appointment.google_event_id = event.event_id
    if now is not None:
        appointment.confirmation_sent_at = now
    await session.flush()

    log.info("calendar event %s created for appointment %s", event.event_id, appointment.id)
    return SyncOutcome(synced=True, event_id=event.event_id, html_link=event.html_link)


async def cancel_calendar_event(
    calendar: GoogleCalendarClient | None,
    agent: Agent,
    appointment: Appointment,
    now: datetime | None = None,
) -> bool:
    """Remove an appointment's event. Best-effort — never raises at the call site."""
    if calendar is None or not appointment.google_event_id or not agent.google_calendar_id:
        return False
    try:
        return await calendar.cancel_event(
            agent.id,
            agent.google_refresh_token,
            agent.google_calendar_id,
            appointment.google_event_id,
            now=now,
        )
    except GoogleCalendarError as exc:
        log.error("could not cancel calendar event for appointment %s: %s", appointment.id, exc)
        return False
