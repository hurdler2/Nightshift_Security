"""Rule schedules in site-local time (spec §10.2).

Two things make this harder than it looks, and both are load-bearing for a product
whose whole job happens at night:

* **Midnight crossing.** A 19:00–06:00 window is the normal case, not the exception.
* **Which day owns the window.** For "Monday 19:00–06:00", 02:00 on *Tuesday* belongs
  to Monday's window. Checking the weekday of the instant instead of the weekday the
  window opened silently disarms every site at midnight.

Timestamps arrive in UTC and are converted with the site's IANA timezone, so DST is
handled by the zone database rather than by arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ALL_DAYS: frozenset[int] = frozenset(range(7))  # Monday = 0, matching datetime.weekday()


class InvalidSchedule(ValueError):
    pass


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise InvalidSchedule(f"unknown timezone: {name!r}") from exc


@dataclass(frozen=True, slots=True)
class Schedule:
    """An armed window, expressed in the site's local wall clock."""

    timezone: str = "Europe/Istanbul"
    start: time = time(0, 0)
    end: time = time(0, 0)
    #: Weekdays the window *opens* on. Monday = 0.
    days: frozenset[int] = field(default=ALL_DAYS)

    def __post_init__(self) -> None:
        resolve_timezone(self.timezone)
        if not self.days:
            raise InvalidSchedule("a schedule needs at least one day")
        if any(day not in ALL_DAYS for day in self.days):
            raise InvalidSchedule("days must be 0..6 with Monday = 0")

    @property
    def is_always_on(self) -> bool:
        """start == end means 24 hours, the same convention the recorder uses."""
        return self.start == self.end and self.days == ALL_DAYS

    @property
    def crosses_midnight(self) -> bool:
        return self.end < self.start

    def local_time(self, moment: datetime) -> datetime:
        """Convert an instant to site-local time. Naive input is assumed UTC."""
        aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        return aware.astimezone(resolve_timezone(self.timezone))

    def is_active(self, moment: datetime) -> bool:
        """Is the window open at this instant?"""
        if self.is_always_on:
            return True

        local = self.local_time(moment)

        if not self.crosses_midnight:
            if local.weekday() not in self.days:
                return False
            return self.start <= local.time() < self.end

        # Crossing midnight: the instant belongs either to a window that opened today
        # (>= start) or to one that opened yesterday (< end).
        if local.time() >= self.start and local.weekday() in self.days:
            return True
        if local.time() < self.end:
            yesterday = (local - timedelta(days=1)).weekday()
            return yesterday in self.days
        return False

    def describe(self) -> str:
        if self.is_always_on:
            return "7/24"
        day_names = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")
        days = "".join(day_names[d] for d in sorted(self.days))
        return f"{days} {self.start:%H:%M}-{self.end:%H:%M} ({self.timezone})"


#: The default posture for a construction site: armed outside working hours.
AFTER_HOURS = Schedule(start=time(19, 0), end=time(6, 0))
ALWAYS = Schedule()


def is_outside_business_hours(
    moment: datetime,
    timezone: str,
    *,
    business_start: time = time(7, 0),
    business_end: time = time(19, 0),
    working_days: frozenset[int] = frozenset({0, 1, 2, 3, 4, 5}),
) -> bool:
    """Risk-engine input (spec §10.3), independent of any particular rule.

    Saturday counts as a working day on Turkish construction sites; Sunday does not.
    """
    local = Schedule(timezone=timezone).local_time(moment)
    if local.weekday() not in working_days:
        return True
    return not (business_start <= local.time() < business_end)
