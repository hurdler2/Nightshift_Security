"""Event-clip retrieval from XVR storage (spec §5).

    rtsp://<user>:<pass>@<host>:554/cam/playback?channel=<N>
        &starttime=YYYY_MM_DD_HH_MM_SS&endtime=YYYY_MM_DD_HH_MM_SS

Clip window defaults to ``T-10s .. T+20s`` around the event and is configurable.
Times are sent in the **device's local time**, not UTC: the XVR indexes its recordings
by wall clock, so a UTC start time silently returns the wrong footage (or none).
Spec §62.9 - NTP/clock correctness is a hard onboarding requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from urllib.parse import quote

from nightshift_edge.devices.dahua.cgi_client import DahuaCredentials
from nightshift_edge.devices.dahua.rtsp import DEFAULT_RTSP_PORT
from nightshift_edge.media.ffprobe import ProbeResult, probe_stream

PLAYBACK_PATH = "/cam/playback"
DAHUA_TIME_FORMAT = "%Y_%m_%d_%H_%M_%S"

DEFAULT_PRE_EVENT_SECONDS = 10
DEFAULT_POST_EVENT_SECONDS = 20


def format_dahua_time(moment: datetime) -> str:
    """Format as `YYYY_MM_DD_HH_MM_SS` (device local time, no timezone suffix)."""
    return moment.strftime(DAHUA_TIME_FORMAT)


def parse_dahua_time(value: str) -> datetime:
    return datetime.strptime(value, DAHUA_TIME_FORMAT)


@dataclass(frozen=True, slots=True)
class ClipWindow:
    """Resolved [start, end] of an event clip, in device local time."""

    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def as_query(self) -> dict[str, str]:
        return {
            "starttime": format_dahua_time(self.start),
            "endtime": format_dahua_time(self.end),
        }


def build_clip_window(
    occurred_at: datetime,
    *,
    pre_event_seconds: int = DEFAULT_PRE_EVENT_SECONDS,
    post_event_seconds: int = DEFAULT_POST_EVENT_SECONDS,
    device_timezone: tzinfo | None = None,
    now: datetime | None = None,
) -> ClipWindow:
    """Compute the clip window around an event.

    ``occurred_at`` may be timezone-aware (cloud/UTC) or naive (already device local).
    When ``device_timezone`` is given an aware timestamp is converted into it; the
    returned datetimes are always naive device-local, matching the URL format.
    """
    if pre_event_seconds < 0 or post_event_seconds < 0:
        raise ValueError("clip padding must be non-negative")

    local = occurred_at
    if local.tzinfo is not None:
        if device_timezone is not None:
            local = local.astimezone(device_timezone)
        local = local.replace(tzinfo=None)

    start = local - timedelta(seconds=pre_event_seconds)
    end = local + timedelta(seconds=post_event_seconds)

    # The XVR cannot return footage from the future; clamp so the request stays valid.
    if now is not None:
        reference = now
        if reference.tzinfo is not None:
            if device_timezone is not None:
                reference = reference.astimezone(device_timezone)
            reference = reference.replace(tzinfo=None)
        if end > reference:
            end = reference
    if end <= start:
        end = start + timedelta(seconds=1)
    return ClipWindow(start=start, end=end)


def build_playback_url(
    host: str,
    vendor_channel: int,
    window: ClipWindow,
    *,
    port: int = DEFAULT_RTSP_PORT,
    credentials: DahuaCredentials | None = None,
) -> str:
    """Build the `cam/playback` RTSP URL for a clip window."""
    if vendor_channel < 0:
        raise ValueError("vendor_channel must be >= 0")
    auth = ""
    if credentials is not None:
        user = quote(credentials.username, safe="")
        password = quote(credentials.password, safe="")
        auth = f"{user}:{password}@"
    host_part = f"{host}:{port}" if port != DEFAULT_RTSP_PORT else host
    query = window.as_query()
    return (
        f"rtsp://{auth}{host_part}{PLAYBACK_PATH}"
        f"?channel={vendor_channel}"
        f"&starttime={query['starttime']}&endtime={query['endtime']}"
    )


async def probe_playback(
    host: str,
    vendor_channel: int,
    credentials: DahuaCredentials,
    window: ClipWindow,
    *,
    port: int = DEFAULT_RTSP_PORT,
    timeout: float = 20.0,
) -> ProbeResult:
    """Check that recorded footage is actually retrievable for a window."""
    url = build_playback_url(host, vendor_channel, window, port=port, credentials=credentials)
    return await probe_stream(url, timeout=timeout)
