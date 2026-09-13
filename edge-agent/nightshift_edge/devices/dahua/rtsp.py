"""Dahua RTSP live URL construction and verification (spec §4).

    rtsp://<user>:<pass>@<host>:554/cam/realmonitor?channel=<N>&subtype=<0|1>

``subtype=0`` is the main stream, ``subtype=1`` the first sub stream. Channels are
1-based per Dahua's documentation; the base is still probed per device and stored,
because we never hard-code vendor numbering (spec §3.1, §62.1).

These URLs contain credentials. They are built as late as possible, never logged, and
never sent to the cloud or the mobile app (spec §20, §33).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from urllib.parse import quote

from nightshift_edge.devices.dahua.cgi_client import DahuaCredentials
from nightshift_edge.media.ffprobe import ProbeResult, probe_stream

DEFAULT_RTSP_PORT = 554
REALMONITOR_PATH = "/cam/realmonitor"


class StreamQuality(enum.StrEnum):
    """Spec §23: LOW -> substream, HIGH -> main stream."""

    LOW = "LOW"
    HIGH = "HIGH"

    @property
    def subtype(self) -> int:
        return 1 if self is StreamQuality.LOW else 0


@dataclass(frozen=True, slots=True)
class RtspTarget:
    """Everything needed to open one RTSP stream."""

    host: str
    vendor_channel: int
    subtype: int
    port: int = DEFAULT_RTSP_PORT

    def url(self, credentials: DahuaCredentials | None = None) -> str:
        return build_live_url(
            self.host,
            self.vendor_channel,
            subtype=self.subtype,
            port=self.port,
            credentials=credentials,
        )

    @property
    def safe_url(self) -> str:
        """Credential-free form, safe for logs and telemetry."""
        return self.url(None)


def build_live_url(
    host: str,
    vendor_channel: int,
    *,
    subtype: int = 1,
    port: int = DEFAULT_RTSP_PORT,
    credentials: DahuaCredentials | None = None,
) -> str:
    """Build a `cam/realmonitor` URL. Credentials are percent-encoded when given."""
    if vendor_channel < 0:
        raise ValueError("vendor_channel must be >= 0")
    if subtype not in (0, 1, 2, 3):
        raise ValueError("subtype must be 0 (main) or 1..3 (sub)")
    auth = ""
    if credentials is not None:
        user = quote(credentials.username, safe="")
        password = quote(credentials.password, safe="")
        auth = f"{user}:{password}@"
    host_part = f"{host}:{port}" if port != DEFAULT_RTSP_PORT else host
    return f"rtsp://{auth}{host_part}{REALMONITOR_PATH}?channel={vendor_channel}&subtype={subtype}"


def vendor_rtsp_channel(logical_channel: int, base: int = 1) -> int:
    """Map a Nightshift logical channel (1-based) to the device's RTSP channel."""
    return logical_channel - 1 + base


async def probe_live_stream(
    host: str,
    vendor_channel: int,
    credentials: DahuaCredentials,
    *,
    subtype: int = 1,
    port: int = DEFAULT_RTSP_PORT,
    timeout: float = 15.0,
) -> ProbeResult:
    """Verify a live channel really decodes. Never raises on stream failure."""
    url = build_live_url(host, vendor_channel, subtype=subtype, port=port, credentials=credentials)
    return await probe_stream(url, timeout=timeout)
