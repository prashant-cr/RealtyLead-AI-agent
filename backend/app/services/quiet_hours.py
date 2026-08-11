"""Quiet-hours guard for outbound messaging (TRAI / basic decency).

Applies to messages *we* initiate — follow-up nudges (M5), digests, reminders.
Replying to a message a lead just sent is not an outbound-initiated message and
is never suppressed: someone who messages at 11pm expects an answer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.scheduling import resolve_timezone


def is_quiet_hour(
    moment: datetime, timezone_name: str | None, start_hour: int, end_hour: int
) -> bool:
    """True when `moment` falls inside the lead's local quiet window.

    The window wraps midnight when `start_hour > end_hour` (the usual 21:00-09:00).
    """
    local_hour = moment.astimezone(resolve_timezone(timezone_name)).hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= local_hour < end_hour
    return local_hour >= start_hour or local_hour < end_hour


def next_send_time(
    moment: datetime, timezone_name: str | None, start_hour: int, end_hour: int
) -> datetime:
    """The earliest moment at or after `moment` that is outside quiet hours."""
    if not is_quiet_hour(moment, timezone_name, start_hour, end_hour):
        return moment

    tz = resolve_timezone(timezone_name)
    local = moment.astimezone(tz)
    candidate = local.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)
