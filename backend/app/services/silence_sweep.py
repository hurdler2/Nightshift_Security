"""Deciding what a quiet fleet means (spec §13).

A recorder that stops sending is the single most important thing this product watches
for, because it is what a competent thief produces on purpose: cut the power, cut the
link, or take the box. It is also what a tripped breaker produces by accident, and the
two look identical from the outside.

So the rule is: **never stay silent about silence**, but say it once at the right
level. One site whose fourteen recorders all went dark together is one alarm about the
site, not fourteen about recorders — while a single dark recorder on a site whose
others are fine is exactly the shape of a stolen box and gets its own alarm.

The decision is a pure function over the fleet's last-seen times so the interesting
cases (all dark, one dark, coming back) can be tested without a database or a clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.modules.devices.watchdog import (
    DEFAULT_INTERVAL_SECONDS,
    SilenceState,
    WatchdogConfig,
    WatchdogVerdict,
    evaluate_silence,
)

#: Site-level alarm once this share of a site's recorders is silent. Below it, the
#: quiet ones are individually suspicious; at or above it, the site is the story.
SITE_OUTAGE_THRESHOLD = 0.75
#: A site needs at least this many recorders before "they all went quiet" means
#: an outage rather than "the one recorder here is gone".
MIN_DEVICES_FOR_SITE_OUTAGE = 2


@dataclass(slots=True)
class DeviceSnapshot:
    """What the sweep needs to know about one recorder."""

    device_id: UUID
    site_id: UUID
    tenant_id: UUID
    name: str
    last_message_at: datetime | None
    stored_state: str = SilenceState.NEVER_SEEN.value
    expected_interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    site_timezone: str = "Europe/Istanbul"
    site_armed: bool = True


@dataclass(slots=True)
class SilenceAlarm:
    """One thing to tell somebody about."""

    event_type: str  # device_silent | site_offline
    site_id: UUID
    tenant_id: UUID
    #: Empty for a site-level alarm; the recorder for a device-level one.
    device_id: UUID | None
    device_name: str | None
    silent_for: timedelta | None
    is_security_relevant: bool
    reason: str
    #: Every recorder this alarm speaks for, so the detail view can list them.
    device_ids: list[UUID] = field(default_factory=list)


@dataclass(slots=True)
class SweepResult:
    alarms: list[SilenceAlarm] = field(default_factory=list)
    #: device_id -> new stored state, for the rows the caller must update.
    state_changes: dict[UUID, str] = field(default_factory=dict)
    #: Recorders that came back on their own; no alarm, but worth recording.
    recovered: list[UUID] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not self.alarms and not self.state_changes


def evaluate_device(snapshot: DeviceSnapshot, now: datetime) -> WatchdogVerdict:
    config = WatchdogConfig(expected_interval_seconds=snapshot.expected_interval_seconds)
    return evaluate_silence(
        last_message_at=snapshot.last_message_at,
        now=now,
        config=config,
        site_timezone=snapshot.site_timezone,
    )


def plan_sweep(snapshots: list[DeviceSnapshot], *, now: datetime | None = None) -> SweepResult:
    """Turn the fleet's last-seen times into alarms and state changes."""
    moment = now or datetime.now(UTC)
    result = SweepResult()

    by_site: dict[UUID, list[tuple[DeviceSnapshot, WatchdogVerdict]]] = {}
    for snapshot in snapshots:
        verdict = evaluate_device(snapshot, moment)
        by_site.setdefault(snapshot.site_id, []).append((snapshot, verdict))

    for site_id, pairs in by_site.items():
        _plan_site(site_id, pairs, result)

    return result


def _plan_site(
    site_id: UUID,
    pairs: list[tuple[DeviceSnapshot, WatchdogVerdict]],
    result: SweepResult,
) -> None:
    silent = [(s, v) for s, v in pairs if v.state is SilenceState.SILENT]
    total = len(pairs)

    for snapshot, verdict in pairs:
        if verdict.state.value != snapshot.stored_state:
            result.state_changes[snapshot.device_id] = verdict.state.value
        if verdict.state is SilenceState.OK and snapshot.stored_state in {
            SilenceState.SILENT.value,
            SilenceState.LATE.value,
        }:
            result.recovered.append(snapshot.device_id)

    # Only newly silent recorders raise anything: a site that has been dark since
    # yesterday must not re-alarm on every sweep.
    newly_silent = [
        (s, v) for s, v in silent if s.stored_state != SilenceState.SILENT.value
    ]
    if not newly_silent:
        return

    site_wide = (
        total >= MIN_DEVICES_FOR_SITE_OUTAGE and len(silent) >= total * SITE_OUTAGE_THRESHOLD
    )
    tenant_id = pairs[0][0].tenant_id

    if site_wide:
        longest = max((v.silent_for for _, v in silent if v.silent_for), default=None)
        armed = any(v.is_security_relevant for _, v in silent)
        result.alarms.append(
            SilenceAlarm(
                event_type="site_offline",
                site_id=site_id,
                tenant_id=tenant_id,
                device_id=None,
                device_name=None,
                silent_for=longest,
                is_security_relevant=armed,
                reason=(
                    f"{len(silent)} of {total} recorders went quiet together: "
                    "treat as a site-wide power or link outage until proven otherwise"
                ),
                device_ids=[s.device_id for s, _ in silent],
            )
        )
        return

    for snapshot, verdict in newly_silent:
        result.alarms.append(
            SilenceAlarm(
                event_type="device_silent",
                site_id=site_id,
                tenant_id=tenant_id,
                device_id=snapshot.device_id,
                device_name=snapshot.name,
                silent_for=verdict.silent_for,
                is_security_relevant=verdict.is_security_relevant,
                reason=(
                    f"{snapshot.name} stopped reporting while its site's other "
                    "recorders kept going - the signature of a stolen or unplugged "
                    "box (spec §13.1)"
                    if len(pairs) > 1
                    else verdict.reason
                ),
                device_ids=[snapshot.device_id],
            )
        )


__all__ = [
    "MIN_DEVICES_FOR_SITE_OUTAGE",
    "SITE_OUTAGE_THRESHOLD",
    "DeviceSnapshot",
    "SilenceAlarm",
    "SweepResult",
    "evaluate_device",
    "plan_sweep",
]
