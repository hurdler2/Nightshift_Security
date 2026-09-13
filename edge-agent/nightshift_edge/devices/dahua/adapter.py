"""`DahuaDeviceAdapter` — CGI primary, NetSDK fallback (spec §6).

    DahuaDeviceAdapter
        ├── DahuaCgiAdapter    (primary, implemented in PHASE 1)
        └── DahuaNetSdkAdapter (fallback, feature-flagged, PHASE 1+)

Channel mapping lives here: the rest of the system speaks logical 1-based channels,
and the per-device bases discovered by the probe are applied at this boundary.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from nightshift_edge.devices.dahua.capabilities import fetch_device_identity
from nightshift_edge.devices.dahua.cgi_client import DahuaCgiClient, DahuaCredentials
from nightshift_edge.devices.dahua.event_parser import is_blocked_code, normalize_event
from nightshift_edge.devices.dahua.event_stream import ALL_CODES, DahuaEventStream
from nightshift_edge.devices.dahua.models import DeviceIdentity, NormalizedEvent, ProbeReport
from nightshift_edge.devices.dahua.playback import build_clip_window, build_playback_url
from nightshift_edge.devices.dahua.probe import DahuaProbe, ProbeOptions
from nightshift_edge.devices.dahua.rtsp import (
    DEFAULT_RTSP_PORT,
    StreamQuality,
    build_live_url,
    vendor_rtsp_channel,
)
from nightshift_edge.devices.dahua.snapshot import fetch_snapshot, vendor_snapshot_channel

log = logging.getLogger(__name__)

VENDOR = "dahua"


@dataclass(slots=True)
class ChannelMapping:
    """Per-device numbering, discovered at onboarding and persisted in the cloud.

    Defaults follow Dahua's documentation, but a device is only trusted after the
    probe confirms them (spec §3.1, §62.1).
    """

    snapshot_base: int = 1
    rtsp_base: int = 1
    event_index_base: int = 0
    #: Explicit overrides: device event index -> logical channel.
    event_index_overrides: dict[int, int] | None = None

    @classmethod
    def from_report(cls, report: ProbeReport) -> ChannelMapping:
        return cls(
            snapshot_base=report.snapshot_channel_base
            if report.snapshot_channel_base is not None
            else 1,
            rtsp_base=report.rtsp_channel_base if report.rtsp_channel_base is not None else 1,
            event_index_base=report.event_index_base if report.event_index_base is not None else 0,
        )


class DahuaCgiAdapter:
    """Primary adapter. All device access on the edge goes through this object."""

    vendor = VENDOR

    def __init__(
        self,
        host: str,
        credentials: DahuaCredentials,
        *,
        http_port: int = 80,
        rtsp_port: int = DEFAULT_RTSP_PORT,
        scheme: str = "http",
        mapping: ChannelMapping | None = None,
        event_codes: str = ALL_CODES,
        heartbeat_seconds: int = 5,
        client: DahuaCgiClient | None = None,
    ) -> None:
        self.host = host
        self.rtsp_port = rtsp_port
        self.mapping = mapping or ChannelMapping()
        self._credentials = credentials
        self._event_codes = event_codes
        self._heartbeat = heartbeat_seconds
        self._client = client or DahuaCgiClient(host, credentials, port=http_port, scheme=scheme)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- discovery ----------------------------------------------------------
    async def probe(self, **options: object) -> ProbeReport:
        report = await DahuaProbe(
            ProbeOptions(
                host=self.host,
                credentials=self._credentials,
                http_port=self._client.port,
                rtsp_port=self.rtsp_port,
                **options,  # type: ignore[arg-type]
            )
        ).run()
        self.mapping = ChannelMapping.from_report(report)
        return report

    async def identity(self) -> DeviceIdentity:
        return await fetch_device_identity(self._client)

    # -- events -------------------------------------------------------------
    async def events(self) -> AsyncIterator[NormalizedEvent]:
        """Normalized, self-reconnecting event stream. Heartbeats are filtered out."""
        stream = DahuaEventStream(
            self._client,
            codes=self._event_codes,
            heartbeat_seconds=self._heartbeat,
        )
        async for raw in stream.listen():
            if raw.is_heartbeat:
                continue
            if is_blocked_code(raw.code):
                # Recorder-side face detection (5-series -I3 and similar). V1 does no
                # face processing, so the payload stops here (spec §47).
                log.warning(
                    "dropped face event %s from %s - disable face detection on the device",
                    raw.code,
                    self.host,
                )
                continue
            yield normalize_event(
                raw,
                event_index_base=self.mapping.event_index_base,
                channel_map=self.mapping.event_index_overrides,
            )

    # -- media --------------------------------------------------------------
    async def snapshot(self, logical_channel: int) -> bytes:
        vendor_channel = vendor_snapshot_channel(logical_channel, self.mapping.snapshot_base)
        snap = await fetch_snapshot(self._client, vendor_channel, logical_channel=logical_channel)
        return snap.content

    async def live_url(self, logical_channel: int, *, high_quality: bool = False) -> str:
        quality = StreamQuality.HIGH if high_quality else StreamQuality.LOW
        return build_live_url(
            self.host,
            vendor_rtsp_channel(logical_channel, self.mapping.rtsp_base),
            subtype=quality.subtype,
            port=self.rtsp_port,
            credentials=self._credentials,
        )

    async def clip_url(
        self,
        logical_channel: int,
        occurred_at: datetime,
        *,
        pre_event_seconds: int = 10,
        post_event_seconds: int = 20,
    ) -> str:
        window = build_clip_window(
            occurred_at,
            pre_event_seconds=pre_event_seconds,
            post_event_seconds=post_event_seconds,
        )
        return build_playback_url(
            self.host,
            vendor_rtsp_channel(logical_channel, self.mapping.rtsp_base),
            window,
            port=self.rtsp_port,
            credentials=self._credentials,
        )


#: Selected by the edge device manager; NetSDK is opt-in per device.
DahuaDeviceAdapter = DahuaCgiAdapter
