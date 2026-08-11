from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.quiet_hours import is_quiet_hour, next_send_time

IST = ZoneInfo("Asia/Kolkata")


def ist(hour: int, minute: int = 0, day: int = 12) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=IST).astimezone(UTC)


@pytest.mark.parametrize("hour", [21, 22, 23, 0, 3, 8])
def test_night_hours_are_quiet(hour: int) -> None:
    assert is_quiet_hour(ist(hour), "Asia/Kolkata", 21, 9) is True


@pytest.mark.parametrize("hour", [9, 12, 17, 20])
def test_daytime_hours_are_not_quiet(hour: int) -> None:
    assert is_quiet_hour(ist(hour), "Asia/Kolkata", 21, 9) is False


def test_window_is_evaluated_in_the_leads_timezone_not_utc() -> None:
    # 18:00 UTC is 23:30 in Kolkata — quiet there, but not in London.
    moment = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)

    assert is_quiet_hour(moment, "Asia/Kolkata", 21, 9) is True
    assert is_quiet_hour(moment, "Europe/London", 21, 9) is False


def test_unknown_timezone_falls_back_instead_of_raising() -> None:
    assert is_quiet_hour(ist(23), "Not/AZone", 21, 9) is True


def test_next_send_time_is_unchanged_outside_quiet_hours() -> None:
    moment = ist(14)

    assert next_send_time(moment, "Asia/Kolkata", 21, 9) == moment


def test_late_night_defers_to_the_next_morning() -> None:
    scheduled = next_send_time(ist(23, 30), "Asia/Kolkata", 21, 9)

    local = scheduled.astimezone(IST)
    assert (local.hour, local.minute) == (9, 0)
    assert local.day == 13


def test_pre_dawn_defers_to_the_same_morning() -> None:
    scheduled = next_send_time(ist(3, 15), "Asia/Kolkata", 21, 9)

    local = scheduled.astimezone(IST)
    assert (local.day, local.hour) == (12, 9)


def test_equal_start_and_end_disables_the_window() -> None:
    assert is_quiet_hour(ist(3), "Asia/Kolkata", 9, 9) is False
