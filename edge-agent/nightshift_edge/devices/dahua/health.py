"""Device / channel health derived from CGI state and the event stream (spec §26).

PHASE 1 provides the read side (poll the device, classify the answers). Alert
suppression hierarchy and cloud state transitions land in PHASE 10.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nightshift_edge.devices.dahua.capabilities import fetch_storage_info
from nightshift_edge.devices.dahua.cgi_client import DahuaCgiClient, DahuaError, parse_kv_response

log = logging.getLogger(__name__)

VIDEO_IN_CGI = "devVideoInput.cgi"


class HealthState(enum.StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    FAILED = "FAILED"
    #: Spec §26: a firmware that cannot answer must never be reported as healthy.
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class StorageHealth:
    state: HealthState = HealthState.UNKNOWN
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_percent: float | None = None
    detail: str | None = None


@dataclass(slots=True)
class DeviceHealth:
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reachable: HealthState = HealthState.UNKNOWN
    event_stream: HealthState = HealthState.UNKNOWN
    storage: StorageHealth = field(default_factory=StorageHealth)
    #: logical channel -> video-loss state
    channels: dict[int, HealthState] = field(default_factory=dict)
    recording_fresh: HealthState = HealthState.UNKNOWN
    notes: list[str] = field(default_factory=list)


#: Free-space threshold that turns storage from OK into WARNING.
LOW_SPACE_PERCENT = 10.0


async def check_storage(client: DahuaCgiClient) -> StorageHealth:
    """Classify HDD state. Anything unreadable stays ``UNKNOWN``, never ``OK``."""
    try:
        values = await fetch_storage_info(client)
    except DahuaError as exc:
        return StorageHealth(state=HealthState.UNKNOWN, detail=str(exc))
    if not values:
        return StorageHealth(state=HealthState.UNKNOWN, detail="storage info unavailable")

    total = _first_int(values, ".TotalBytes")
    used = _first_int(values, ".UsedBytes")
    state = HealthState.OK
    detail = None

    for key, value in values.items():
        if key.lower().endswith(".state") and value.lower() not in {"active", "normal", "ok"}:
            state = HealthState.FAILED
            detail = f"{key}={value}"
            break

    free_percent: float | None = None
    if total and total > 0 and used is not None:
        free_percent = round((total - used) / total * 100, 2)
        if state is HealthState.OK and free_percent < LOW_SPACE_PERCENT:
            state = HealthState.WARNING
            detail = f"only {free_percent:.1f}% free"

    return StorageHealth(
        state=state,
        total_bytes=total,
        used_bytes=used,
        free_percent=free_percent,
        detail=detail,
    )


async def check_channels(client: DahuaCgiClient, channel_count: int) -> dict[int, HealthState]:
    """Per-channel video presence, if the firmware exposes it."""
    try:
        text = await client.get_text(VIDEO_IN_CGI, {"action": "getCollect"})
    except DahuaError as exc:
        log.debug("channel health unavailable: %s", exc)
        return dict.fromkeys(range(1, channel_count + 1), HealthState.UNKNOWN)

    values = parse_kv_response(text)
    result: dict[int, HealthState] = {}
    for logical in range(1, channel_count + 1):
        raw = values.get(f"result[{logical - 1}]") or values.get(f"channel[{logical - 1}]")
        if raw is None:
            result[logical] = HealthState.UNKNOWN
        else:
            result[logical] = HealthState.OK if raw.lower() == "true" else HealthState.FAILED
    return result


def _first_int(values: dict[str, str], suffix: str) -> int | None:
    for key, value in values.items():
        if key.endswith(suffix):
            try:
                return int(float(value))
            except ValueError:
                return None
    return None


# TODO(V1-BLOCKER): PHASE 10 — recording freshness. Requires a firmware-verified
# media-file query; until one is confirmed on the pilot devices, `recording_fresh`
# must stay UNKNOWN rather than reporting RECORDING_OK (spec §26).
