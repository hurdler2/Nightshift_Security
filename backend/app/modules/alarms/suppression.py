"""Alarm suppression hierarchy (spec §13).

When a site loses its internet link, fourteen recorders and seventy cameras must not
produce seventy alarms. One alarm, at the highest level that explains the outage.

The dangerous half of this feature is the reason for :data:`NEVER_SUPPRESSED`. A thief
cutting power or stealing the recorder produces exactly the pattern that suppression is
designed to collapse — so the events that would reveal a theft are exempt. Suppression
may reduce noise; it may never hide an incident.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class AlarmLevel(enum.IntEnum):
    """Higher value swallows lower ones."""

    CHANNEL = 1
    DEVICE = 2
    SITE = 3


#: Event types that are never suppressed by a higher-level outage, because each one
#: can be the signature of the incident rather than a side effect of it (spec §13.1).
NEVER_SUPPRESSED: frozenset[str] = frozenset(
    {
        "person_detected",
        "vehicle_detected",
        "line_crossing",
        "zone_intrusion",
        "video_tampering",
        "device_silent",
        "site_offline",
    }
)

#: Symptom -> level. Anything not listed is treated as a channel-level symptom.
LEVEL_BY_EVENT: dict[str, AlarmLevel] = {
    "site_offline": AlarmLevel.SITE,
    "device_silent": AlarmLevel.DEVICE,
    "device_offline": AlarmLevel.DEVICE,
    "storage_not_exist": AlarmLevel.DEVICE,
    "storage_failure": AlarmLevel.DEVICE,
    "storage_low_space": AlarmLevel.DEVICE,
    "video_loss": AlarmLevel.CHANNEL,
    "video_tampering": AlarmLevel.CHANNEL,
}


def level_for(event_type: str) -> AlarmLevel:
    return LEVEL_BY_EVENT.get(event_type, AlarmLevel.CHANNEL)


@dataclass(slots=True)
class OutageState:
    """What is currently known to be down, per site and per device."""

    offline_sites: set[str] = field(default_factory=set)
    silent_devices: set[str] = field(default_factory=set)

    def mark_site_offline(self, site_id: str) -> None:
        self.offline_sites.add(site_id)

    def mark_site_online(self, site_id: str) -> None:
        self.offline_sites.discard(site_id)

    def mark_device_silent(self, device_id: str) -> None:
        self.silent_devices.add(device_id)

    def mark_device_healthy(self, device_id: str) -> None:
        self.silent_devices.discard(device_id)


@dataclass(slots=True)
class SuppressionDecision:
    suppressed: bool
    reason: str
    #: The alarm that already covers this symptom, if any.
    covered_by: AlarmLevel | None = None


def should_suppress(
    *,
    event_type: str,
    site_id: str,
    device_id: str,
    outages: OutageState,
) -> SuppressionDecision:
    """Decide whether this symptom is already explained by a bigger outage."""
    if event_type in NEVER_SUPPRESSED:
        return SuppressionDecision(
            suppressed=False,
            reason="security-relevant event types are never suppressed",
        )

    level = level_for(event_type)

    if site_id in outages.offline_sites and level < AlarmLevel.SITE:
        return SuppressionDecision(
            suppressed=True,
            reason="site is offline; the site alarm already covers this",
            covered_by=AlarmLevel.SITE,
        )

    if device_id in outages.silent_devices and level < AlarmLevel.DEVICE:
        return SuppressionDecision(
            suppressed=True,
            reason="recorder is silent; the device alarm already covers this",
            covered_by=AlarmLevel.DEVICE,
        )

    return SuppressionDecision(suppressed=False, reason="nothing higher explains this")
