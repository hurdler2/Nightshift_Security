"""Data models for the Dahua adapter.

Plain dataclasses (not pydantic) so the parser/probe layer stays importable with a
minimal dependency set and can run on a constrained edge gateway.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


class Capability(enum.StrEnum):
    """Tri-state capability.

    Spec §0.3 / §26: never report a capability as supported unless it was actually
    observed. Anything not proven is ``UNKNOWN`` — never silently ``False``.
    """

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_bool(cls, value: bool | None) -> Capability:
        if value is None:
            return cls.UNKNOWN
        return cls.SUPPORTED if value else cls.UNSUPPORTED

    @property
    def is_supported(self) -> bool:
        return self is Capability.SUPPORTED


class EventAction(enum.StrEnum):
    """Normalized event action (spec §12)."""

    OPENED = "opened"  # Dahua "Start"
    CLOSED = "closed"  # Dahua "Stop"
    PULSE = "pulse"  # Dahua "Pulse"
    UNKNOWN = "unknown"  # firmware-specific action; kept, logged, never dropped


@dataclass(slots=True)
class DahuaRawEvent:
    """One `Code=...;action=...;index=...;data={...}` payload, unmodified."""

    code: str
    action: str
    index: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_heartbeat(self) -> bool:
        return self.code.lower() in {"heartbeat", "keepalive"}


@dataclass(slots=True)
class NormalizedEvent:
    """Vendor-neutral event handed to the cloud (spec §12 normalize örneği)."""

    event_type: str
    vendor: str
    vendor_event_code: str
    vendor_action: str
    action: EventAction
    vendor_channel_index: int | None
    logical_channel: int | None
    occurred_at: datetime
    raw_data: dict[str, Any] = field(default_factory=dict)
    detected_object_type: str | None = None
    is_known_code: bool = True
    is_known_action: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action"] = self.action.value
        payload["occurred_at"] = self.occurred_at.isoformat()
        return payload


@dataclass(slots=True)
class ChannelProbeResult:
    """Per-channel outcome of the onboarding probe."""

    logical_channel: int
    snapshot_channel: int | None = None
    snapshot_ok: bool = False
    snapshot_bytes: int = 0
    snapshot_error: str | None = None
    rtsp_sub_ok: Capability = Capability.UNKNOWN
    rtsp_main_ok: Capability = Capability.UNKNOWN
    rtsp_codec: str | None = None
    rtsp_resolution: str | None = None
    rtsp_error: str | None = None
    playback_ok: Capability = Capability.UNKNOWN
    playback_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("rtsp_sub_ok", "rtsp_main_ok", "playback_ok"):
            payload[key] = getattr(self, key).value
        return payload


@dataclass(slots=True)
class DeviceIdentity:
    """Fields persisted at onboarding (spec §2.2 minimum kayıt alanları)."""

    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    web_version: str | None = None
    build_date: str | None = None
    device_type: str | None = None
    channel_count: int | None = None
    http_port: int | None = None
    https_port: int | None = None
    rtsp_port: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProbeReport:
    """Full capability report written by `scripts/dahua_probe.py` (spec §50)."""

    host: str
    probed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_seconds: float = 0.0

    reachable: Capability = Capability.UNKNOWN
    digest_auth: Capability = Capability.UNKNOWN
    identity: DeviceIdentity = field(default_factory=DeviceIdentity)

    cgi: Capability = Capability.UNKNOWN
    snapshot: Capability = Capability.UNKNOWN
    rtsp: Capability = Capability.UNKNOWN
    rtsp_playback: Capability = Capability.UNKNOWN
    event_stream: Capability = Capability.UNKNOWN
    smart_motion_human: Capability = Capability.UNKNOWN
    smart_motion_vehicle: Capability = Capability.UNKNOWN
    video_loss_events: Capability = Capability.UNKNOWN
    video_blind_events: Capability = Capability.UNKNOWN
    storage_events: Capability = Capability.UNKNOWN

    event_index_base: int | None = None
    snapshot_channel_base: int | None = None
    rtsp_channel_base: int | None = None

    caps_raw: str | None = None
    exposure_events: list[str] = field(default_factory=list)
    observed_event_codes: dict[str, int] = field(default_factory=dict)
    observed_events: list[dict[str, Any]] = field(default_factory=list)
    channels: list[ChannelProbeResult] = field(default_factory=list)
    ffprobe_available: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def fail(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "host": self.host,
            "probed_at": self.probed_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
            "identity": self.identity.to_dict(),
            "channels": [c.to_dict() for c in self.channels],
            "event_index_base": self.event_index_base,
            "snapshot_channel_base": self.snapshot_channel_base,
            "rtsp_channel_base": self.rtsp_channel_base,
            "caps_raw": self.caps_raw,
            "exposure_events": self.exposure_events,
            "observed_event_codes": self.observed_event_codes,
            "observed_events": self.observed_events,
            "ffprobe_available": self.ffprobe_available,
            "warnings": self.warnings,
            "errors": self.errors,
        }
        for key in (
            "reachable",
            "digest_auth",
            "cgi",
            "snapshot",
            "rtsp",
            "rtsp_playback",
            "event_stream",
            "smart_motion_human",
            "smart_motion_vehicle",
            "video_loss_events",
            "video_blind_events",
            "storage_events",
        ):
            payload[key] = getattr(self, key).value
        return payload
