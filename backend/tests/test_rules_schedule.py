"""Schedules in site-local time (spec §10.2).

Most of these exist because a night-security product that gets midnight or DST wrong
disarms itself at exactly the hour it is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from app.modules.rules.schedule import (
    AFTER_HOURS,
    ALWAYS,
    InvalidSchedule,
    Schedule,
    is_outside_business_hours,
)

IST = "Europe/Istanbul"  # UTC+3, no DST since 2016
ALGIERS = "Africa/Algiers"  # UTC+1, no DST
BERLIN = "Europe/Berlin"  # UTC+1/+2, has DST


def utc(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class TestBasics:
    def test_always_on(self):
        assert ALWAYS.is_active(utc(2026, 9, 12, 13))
        assert ALWAYS.is_always_on

    def test_daytime_window(self):
        schedule = Schedule(timezone=IST, start=time(9, 0), end=time(17, 0))
        assert schedule.is_active(utc(2026, 9, 14, 9))  # 12:00 local, Monday
        assert not schedule.is_active(utc(2026, 9, 14, 15))  # 18:00 local

    def test_window_is_half_open(self):
        """Start is inclusive, end exclusive - no double firing on the boundary."""
        schedule = Schedule(timezone=IST, start=time(9, 0), end=time(17, 0))
        assert schedule.is_active(utc(2026, 9, 14, 6))  # exactly 09:00 local
        assert not schedule.is_active(utc(2026, 9, 14, 14))  # exactly 17:00 local

    def test_rejects_unknown_timezone(self):
        with pytest.raises(InvalidSchedule, match="timezone"):
            Schedule(timezone="Mars/Olympus")

    def test_rejects_empty_days(self):
        with pytest.raises(InvalidSchedule, match="at least one day"):
            Schedule(days=frozenset())

    def test_rejects_out_of_range_days(self):
        with pytest.raises(InvalidSchedule, match=r"0\.\.6"):
            Schedule(days=frozenset({9}))


class TestMidnightCrossing:
    """A 19:00-06:00 window is the normal case for this product."""

    def test_evening_is_inside(self):
        assert AFTER_HOURS.is_active(utc(2026, 9, 14, 18))  # 21:00 local Monday

    def test_after_midnight_is_inside(self):
        assert AFTER_HOURS.is_active(utc(2026, 9, 14, 23))  # 02:00 local Tuesday

    def test_daytime_is_outside(self):
        assert not AFTER_HOURS.is_active(utc(2026, 9, 15, 9))  # 12:00 local Tuesday

    def test_boundary_hours(self):
        assert AFTER_HOURS.is_active(utc(2026, 9, 14, 16))  # exactly 19:00 local
        assert not AFTER_HOURS.is_active(utc(2026, 9, 15, 3))  # exactly 06:00 local

    def test_the_window_belongs_to_the_day_it_opened(self):
        """Friday-night 19:00-06:00 must still be armed at 02:00 on Saturday."""
        friday_only = Schedule(timezone=IST, start=time(19, 0), end=time(6, 0), days=frozenset({4}))
        # 2026-09-18 is a Friday.
        assert friday_only.is_active(utc(2026, 9, 18, 18))  # 21:00 Fri local
        assert friday_only.is_active(utc(2026, 9, 18, 23))  # 02:00 Sat local
        assert not friday_only.is_active(utc(2026, 9, 19, 18))  # 21:00 Sat local

    def test_saturday_morning_is_not_armed_by_a_saturday_only_rule(self):
        """The reverse of the above: Saturday's window has not opened yet at 02:00."""
        saturday_only = Schedule(
            timezone=IST, start=time(19, 0), end=time(6, 0), days=frozenset({5})
        )
        assert not saturday_only.is_active(utc(2026, 9, 18, 23))  # 02:00 Sat local
        assert saturday_only.is_active(utc(2026, 9, 19, 18))  # 21:00 Sat local


class TestTimezones:
    def test_same_instant_differs_by_site(self):
        moment = utc(2026, 9, 14, 17)  # 20:00 Istanbul, 18:00 Algiers
        istanbul = Schedule(timezone=IST, start=time(19, 0), end=time(6, 0))
        algiers = Schedule(timezone=ALGIERS, start=time(19, 0), end=time(6, 0))
        assert istanbul.is_active(moment)
        assert not algiers.is_active(moment)

    def test_dst_is_handled_by_the_zone_database(self):
        """Berlin is UTC+2 in summer and UTC+1 in winter; 20:00 local both times."""
        summer = Schedule(timezone=BERLIN, start=time(19, 0), end=time(6, 0))
        assert summer.is_active(datetime(2026, 7, 15, 18, tzinfo=UTC))  # 20:00 CEST
        assert summer.is_active(datetime(2026, 1, 15, 19, tzinfo=UTC))  # 20:00 CET
        assert not summer.is_active(datetime(2026, 7, 15, 10, tzinfo=UTC))  # 12:00 CEST

    def test_naive_input_is_treated_as_utc(self):
        schedule = Schedule(timezone=IST, start=time(19, 0), end=time(6, 0))
        assert schedule.is_active(datetime(2026, 9, 14, 18))


class TestBusinessHours:
    def test_night_is_outside(self):
        assert is_outside_business_hours(utc(2026, 9, 14, 23), IST)  # 02:00 local

    def test_workday_noon_is_inside(self):
        assert not is_outside_business_hours(utc(2026, 9, 14, 9), IST)  # 12:00 Monday

    def test_sunday_is_outside_all_day(self):
        # 2026-09-13 is a Sunday.
        assert is_outside_business_hours(utc(2026, 9, 13, 9), IST)

    def test_saturday_counts_as_a_working_day(self):
        """Turkish construction sites work Saturdays; alarming all day would be noise."""
        assert not is_outside_business_hours(utc(2026, 9, 19, 9), IST)


def test_describe_is_human_readable():
    assert ALWAYS.describe() == "7/24"
    assert "19:00-06:00" in AFTER_HOURS.describe()
