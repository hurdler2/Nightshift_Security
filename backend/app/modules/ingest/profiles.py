"""Firmware-specific alarm-mail body parsing (spec §7.3).

The body format is firmware-dependent and **is not hard-coded**. A profile is a set
of regex rules loaded from `firmware_email_profiles`; the profile below is a
provisional starting point, explicitly marked unverified until a real message from
the pilot recorder is captured.

Two rules govern everything here:

* **A parse failure never costs an alarm.** Missing fields become `None` and the
  caller still raises an event (spec §7.3).
* **Body text never establishes identity.** The device is resolved from SMTP AUTH
  (spec §7.1); the device name in the body is a label, not a credential.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

#: Face events are refused before they can be stored (spec §19.3). The device should
#: not even be able to produce them under AI Mode = IVS&SMD, but the mode can be
#: changed on the device and the pipeline must not depend on that.
BLOCKED_EVENT_PATTERN = re.compile(r"(?i)\bface\b")


class EventField(enum.StrEnum):
    EVENT = "event"
    CHANNEL_NUMBER = "channel_number"
    CHANNEL_NAME = "channel_name"
    DEVICE_NAME = "device_name"
    OCCURRED_AT = "occurred_at"
    DEVICE_IP = "device_ip"
    SERIAL = "serial"


@dataclass(frozen=True, slots=True)
class FieldRule:
    """One regex that lifts a single field out of the body."""

    field: EventField
    pattern: re.Pattern[str]
    group: int = 1

    def apply(self, text: str) -> str | None:
        match = self.pattern.search(text)
        if not match:
            return None
        try:
            value = match.group(self.group)
        except IndexError:  # pragma: no cover - profile with a bad group index
            return None
        return value.strip() if value else None


@dataclass(frozen=True, slots=True)
class EmailProfile:
    """A named set of extraction rules for one firmware family."""

    name: str
    #: False until a message from a real device has been parsed with it (spec §0.3).
    verified: bool
    rules: tuple[FieldRule, ...]
    #: Vendor event label (as it appears in the mail) -> SiteGuard event type.
    event_type_map: dict[str, str] = field(default_factory=dict)
    #: Formats tried, in order, for the event timestamp.
    time_formats: tuple[str, ...] = (
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%Y_%m_%d_%H_%M_%S",
    )


@dataclass(slots=True)
class ParsedBody:
    """Result of applying a profile. Every field may be `None`."""

    profile_name: str
    event_label: str | None = None
    event_type: str = "unknown_alarm"
    channel_number: int | None = None
    channel_name: str | None = None
    device_name: str | None = None
    device_ip: str | None = None
    serial: str | None = None
    occurred_at: datetime | None = None
    occurred_at_raw: str | None = None
    is_blocked: bool = False
    missing: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing

    @property
    def has_event_identity(self) -> bool:
        """Enough to raise a meaningful alarm: what happened and where."""
        return self.event_type != "unknown_alarm" and self.channel_number is not None


def _rule(field_name: EventField, pattern: str, group: int = 1) -> FieldRule:
    return FieldRule(field_name, re.compile(pattern, re.IGNORECASE | re.MULTILINE), group)


#: Mail labels differ from the CGI event codes, so this map is separate from the
#: edge agent's EVENT_CODE_MAP. Keys are matched case-insensitively as substrings.
DAHUA_EVENT_LABELS: dict[str, str] = {
    "smart motion human": "person_detected",
    "smd human": "person_detected",
    "human detection": "person_detected",
    "smart motion vehicle": "vehicle_detected",
    "smd vehicle": "vehicle_detected",
    "vehicle detection": "vehicle_detected",
    "smart motion": "motion_detected",
    "tripwire": "line_crossing",
    "cross line": "line_crossing",
    "crossing line": "line_crossing",
    "intrusion": "zone_intrusion",
    "cross region": "zone_intrusion",
    "perimeter": "zone_intrusion",
    "motion detection": "motion_detected",
    "video loss": "video_loss",
    "video tampering": "video_tampering",
    "camera masking": "video_tampering",
    "tampering": "video_tampering",
    "no disk": "storage_not_exist",
    "disk error": "storage_failure",
    "hdd error": "storage_failure",
    "disk full": "storage_low_space",
    "low space": "storage_low_space",
    "offline": "device_offline",
}


#: Starting point only. Built from commonly documented Dahua alarm-mail wording;
#: **not yet confirmed against the pilot recorder**. The first real message captured
#: in `email_samples` replaces this with a verified profile (spec §24.3).
PROVISIONAL_DAHUA = EmailProfile(
    name="dahua-provisional",
    verified=False,
    rules=(
        _rule(EventField.EVENT, r"^\s*Alarm\s*(?:Event|Type)\s*[:：]\s*(.+)$"),
        _rule(
            EventField.CHANNEL_NUMBER,
            r"^\s*(?:Alarm\s*(?:Input\s*)?Channel(?:\s*No\.?)?|Channel)\s*[:：]\s*(\d+)",
        ),
        _rule(EventField.CHANNEL_NAME, r"^\s*Channel\s*Name\s*[:：]\s*(.+)$"),
        _rule(EventField.DEVICE_NAME, r"^\s*(?:Alarm\s*)?Device\s*Name\s*[:：]\s*(.+)$"),
        _rule(
            EventField.OCCURRED_AT,
            # The label often carries a parenthetical format hint that itself contains
            # colons, e.g. "Alarm Start Time(D/M/Y H:M:S):" - so skip a (...) group
            # before the real separator.
            r"^\s*(?:Alarm\s*)?(?:Start\s*)?Time\s*(?:\([^)]*\))?\s*[:：]\s*"
            r"(\d{2,4}[-/_.]\d{1,2}[-/_.]\d{2,4}[ T_]\d{1,2}[:_]\d{2}(?:[:_]\d{2})?)",
        ),
        _rule(EventField.DEVICE_IP, r"^\s*IP\s*Address\s*[:：]\s*([\d.]+)"),
        _rule(EventField.SERIAL, r"^\s*(?:Serial\s*(?:No\.?|Number)|SN)\s*[:：]\s*(\S+)"),
    ),
    event_type_map=DAHUA_EVENT_LABELS,
)


def classify_event_label(label: str | None, mapping: dict[str, str]) -> str:
    """Map a vendor label to a SiteGuard event type.

    Longest key first, so "smart motion human" wins over "smart motion".
    """
    if not label:
        return "unknown_alarm"
    lowered = label.lower()
    for key in sorted(mapping, key=len, reverse=True):
        if key in lowered:
            return mapping[key]
    return "unknown_alarm"


def parse_body(text: str, profile: EmailProfile = PROVISIONAL_DAHUA) -> ParsedBody:
    """Apply a profile to a mail body. Returns partial results rather than failing."""
    result = ParsedBody(profile_name=profile.name)
    if not text or not text.strip():
        result.missing = [f.value for f in EventField]
        return result

    values: dict[EventField, str | None] = {rule.field: rule.apply(text) for rule in profile.rules}

    result.event_label = values.get(EventField.EVENT)
    result.channel_name = values.get(EventField.CHANNEL_NAME)
    result.device_name = values.get(EventField.DEVICE_NAME)
    result.device_ip = values.get(EventField.DEVICE_IP)
    result.serial = values.get(EventField.SERIAL)
    result.occurred_at_raw = values.get(EventField.OCCURRED_AT)

    channel_raw = values.get(EventField.CHANNEL_NUMBER)
    if channel_raw and channel_raw.isdigit():
        result.channel_number = int(channel_raw)

    if result.occurred_at_raw:
        result.occurred_at = _parse_time(result.occurred_at_raw, profile.time_formats)

    # Face events are refused wherever they appear - label or free text (spec §19.3).
    if BLOCKED_EVENT_PATTERN.search(result.event_label or "") or BLOCKED_EVENT_PATTERN.search(text):
        result.is_blocked = True
        result.event_type = "blocked:face_event"
        return result

    result.event_type = classify_event_label(result.event_label, profile.event_type_map)
    result.missing = sorted(_missing_fields(values))
    return result


def _missing_fields(values: dict[EventField, str | None]) -> Iterable[str]:
    for field_name in (
        EventField.EVENT,
        EventField.CHANNEL_NUMBER,
        EventField.OCCURRED_AT,
    ):
        if not values.get(field_name):
            yield field_name.value


def _parse_time(raw: str, formats: tuple[str, ...]) -> datetime | None:
    """Parse the recorder's local wall clock. Returns naive datetime by design.

    The recorder reports its own local time with no offset; converting it needs the
    site's IANA timezone, which lives in the site record, not here.
    """
    candidate = raw.strip().replace("T", " ")
    for fmt in formats:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None
