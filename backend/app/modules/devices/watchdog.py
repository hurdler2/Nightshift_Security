"""Device silence watchdog (spec §13.1, §13.3).

With no agent on site, a recorder that stops talking is the only signal we get that
something happened to it — a cut cable, a pulled plug, or a thief carrying it away.

So silence is not a maintenance notice. At night, on a site that was armed, it is
treated as a security event and escalated (spec §13.1).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.modules.rules.schedule import Schedule

#: Health mail interval configured on the recorder. Six hours is far too slow to
#: notice a theft, so critical sites run 30-60 minutes (spec §13.1).
DEFAULT_INTERVAL_SECONDS = 30 * 60
#: Grace multiplier before a late device is called silent.
DEFAULT_TOLERANCE = 1.5


class SilenceState(enum.StrEnum):
    OK = "OK"
    #: Past due but inside the grace window - a single lost mail, probably.
    LATE = "LATE"
    #: Beyond grace. Something is wrong with the recorder, its power, or its link.
    SILENT = "SILENT"
    #: Never heard from since commissioning.
    NEVER_SEEN = "NEVER_SEEN"


@dataclass(slots=True)
class WatchdogConfig:
    expected_interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    tolerance: float = DEFAULT_TOLERANCE
    #: Site hours used to decide whether silence is suspicious or merely a fault.
    armed_schedule: Schedule | None = None

    @property
    def silent_after(self) -> timedelta:
        return timedelta(seconds=self.expected_interval_seconds * self.tolerance)

    @property
    def late_after(self) -> timedelta:
        return timedelta(seconds=self.expected_interval_seconds)


@dataclass(slots=True)
class WatchdogVerdict:
    state: SilenceState
    silent_for: timedelta | None
    #: True when this silence should raise a security alarm rather than a fault notice.
    is_security_relevant: bool
    reason: str

    @property
    def should_alarm(self) -> bool:
        return self.state in {SilenceState.SILENT, SilenceState.NEVER_SEEN}


def evaluate_silence(
    *,
    last_message_at: datetime | None,
    now: datetime,
    config: WatchdogConfig | None = None,
    site_timezone: str = "Europe/Istanbul",
    night_start: time = time(19, 0),
    night_end: time = time(6, 0),
) -> WatchdogVerdict:
    """Classify how worried we should be about a quiet recorder."""
    config = config or WatchdogConfig()

    if last_message_at is None:
        return WatchdogVerdict(
            state=SilenceState.NEVER_SEEN,
            silent_for=None,
            is_security_relevant=False,
            reason="no message has ever arrived from this device",
        )

    quiet_for = now - last_message_at

    if quiet_for < config.late_after:
        return WatchdogVerdict(
            state=SilenceState.OK,
            silent_for=quiet_for,
            is_security_relevant=False,
            reason="reporting on schedule",
        )

    if quiet_for < config.silent_after:
        return WatchdogVerdict(
            state=SilenceState.LATE,
            silent_for=quiet_for,
            is_security_relevant=False,
            reason="one health message missed; inside the grace window",
        )

    at_night = _is_night(now, site_timezone, night_start, night_end)
    armed = config.armed_schedule.is_active(now) if config.armed_schedule else at_night

    return WatchdogVerdict(
        state=SilenceState.SILENT,
        silent_for=quiet_for,
        is_security_relevant=armed,
        reason=(
            "recorder went quiet while the site was armed - treat as a possible "
            "tamper or theft (spec §13.1)"
            if armed
            else "recorder is not reporting; raise as a device fault"
        ),
    )


def _is_night(moment: datetime, timezone: str, start: time, end: time) -> bool:
    local = Schedule(timezone=timezone).local_time(moment).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end
