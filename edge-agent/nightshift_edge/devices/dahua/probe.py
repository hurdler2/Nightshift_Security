"""PHASE 1 hardware probe (spec §50).

Runs the full onboarding interrogation against a real XVR and writes a JSON
capability report. Nothing is reported as supported unless it was observed:
un-exercised checks stay ``UNKNOWN``.

Steps, in order:

1.  TCP reachability                     9.  attach stream listen (default 60s)
2.  Digest auth                          10. operator SMD trigger prompt
3.  model / firmware / serial            11. observed event codes
4.  event caps                           12. SmartMotionHuman seen?
5.  exposure events                      13. SmartMotionVehicle seen?
6.  snapshot per channel + base          14. channel index mapping
7.  RTSP substream per channel           15. playback window test
8.  RTSP main stream (optional)          16. JSON report
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from nightshift_edge.devices.dahua import capabilities as caps
from nightshift_edge.devices.dahua.cgi_client import (
    DahuaAuthError,
    DahuaCgiClient,
    DahuaCredentials,
    DahuaError,
)
from nightshift_edge.devices.dahua.event_parser import is_blocked_code
from nightshift_edge.devices.dahua.event_stream import ALL_CODES, collect_events
from nightshift_edge.devices.dahua.models import (
    Capability,
    ChannelProbeResult,
    DahuaRawEvent,
    ProbeReport,
)
from nightshift_edge.devices.dahua.playback import build_clip_window, probe_playback
from nightshift_edge.devices.dahua.rtsp import (
    DEFAULT_RTSP_PORT,
    build_live_url,
    probe_live_stream,
)
from nightshift_edge.devices.dahua.snapshot import detect_snapshot_channel_base, try_snapshot
from nightshift_edge.media.ffprobe import ffprobe_available

log = logging.getLogger(__name__)

HUMAN_CODE = "SmartMotionHuman"
VEHICLE_CODE = "SmartMotionVehicle"
STORAGE_CODES = {"StorageNotExist", "StorageFailure", "StorageLowSpace"}

#: A device clock more than this far from the edge clock breaks playback windows.
MAX_CLOCK_SKEW_SECONDS = 60


@dataclass(slots=True)
class ProbeOptions:
    host: str
    credentials: DahuaCredentials
    http_port: int = 80
    scheme: str = "http"
    rtsp_port: int = DEFAULT_RTSP_PORT
    channels: int = 8
    listen_seconds: float = 60.0
    event_codes: str = ALL_CODES
    heartbeat_seconds: int = 5
    test_main_stream: bool = False
    test_playback: bool = True
    playback_offset_minutes: int = 5
    #: Channel the operator will walk in front of, for index-mapping (spec §50.14).
    trigger_channel: int | None = None
    verify_tls: bool = True
    max_parallel_channel_tests: int = 3


ProgressFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


@dataclass(slots=True)
class DahuaProbe:
    """Orchestrates the capability interrogation for one device."""

    options: ProbeOptions
    progress: ProgressFn = field(default=_noop)

    async def run(self) -> ProbeReport:
        started = time.monotonic()
        report = ProbeReport(host=self.options.host)
        report.ffprobe_available = ffprobe_available()
        if not report.ffprobe_available:
            report.warn("ffprobe not installed - RTSP and playback checks left UNKNOWN")

        # 1. reachability
        self.progress(f"[1/16] TCP {self.options.host}:{self.options.http_port} ...")
        reachable, detail = await self._check_tcp(self.options.host, self.options.http_port)
        report.reachable = Capability.from_bool(reachable)
        if not reachable:
            report.fail(f"host unreachable on port {self.options.http_port}: {detail}")
            report.duration_seconds = time.monotonic() - started
            return report

        client = DahuaCgiClient(
            self.options.host,
            self.options.credentials,
            port=self.options.http_port,
            scheme=self.options.scheme,
            verify_tls=self.options.verify_tls,
        )
        try:
            await self._run_with_client(client, report)
        finally:
            await client.aclose()
            report.duration_seconds = time.monotonic() - started
        return report

    # -- stages -------------------------------------------------------------
    async def _run_with_client(self, client: DahuaCgiClient, report: ProbeReport) -> None:
        # 2. digest auth
        self.progress("[2/16] HTTP Digest auth ...")
        try:
            await client.get_text(caps.MAGICBOX, {"action": "getSystemInfo"})
            report.digest_auth = Capability.SUPPORTED
            report.cgi = Capability.SUPPORTED
        except DahuaAuthError as exc:
            report.digest_auth = Capability.UNSUPPORTED
            report.fail(f"digest auth failed: {exc}")
            return
        except DahuaError as exc:
            report.digest_auth = Capability.UNKNOWN
            report.warn(f"getSystemInfo unavailable ({exc}); continuing")

        # 3. identity
        self.progress("[3/16] Device identity ...")
        report.identity = await caps.fetch_device_identity(client)
        report.identity.rtsp_port = report.identity.rtsp_port or self.options.rtsp_port
        if report.identity.model:
            self.progress(
                f"        model={report.identity.model} "
                f"firmware={report.identity.firmware_version or '?'} "
                f"channels={report.identity.channel_count or '?'}"
            )

        clock = await caps.fetch_device_clock(client)
        if clock.device_time is None:
            report.warn("device time could not be read - verify NTP manually (spec §62.9)")
        elif clock.skew_seconds is not None and abs(clock.skew_seconds) > MAX_CLOCK_SKEW_SECONDS:
            report.fail(
                f"device clock is off by {clock.skew_seconds:.0f}s - "
                "event times and playback windows will be wrong (spec §62.9)"
            )
        if clock.ntp_enabled is False:
            report.warn("NTP is disabled on the device")

        # 4. event caps
        self.progress("[4/16] eventManager getCaps ...")
        report.caps_raw = await caps.fetch_event_caps(client)
        if report.caps_raw is None:
            report.warn("getCaps unsupported on this firmware")

        # 5. exposure events
        self.progress("[5/16] eventManager getExposureEvents ...")
        report.exposure_events = await caps.fetch_exposure_events(client)
        if not report.exposure_events:
            report.warn("getExposureEvents empty/unsupported - relying on the attach probe instead")

        channel_count = self._channel_count(report)

        # 6. snapshots
        self.progress(f"[6/16] Snapshot channels 1..{channel_count} ...")
        await self._probe_snapshots(client, report, channel_count)

        # 7-8. rtsp
        self.progress(f"[7/16] RTSP substream channels 1..{channel_count} ...")
        await self._probe_rtsp(report, channel_count, subtype=1)
        if self.options.test_main_stream:
            self.progress("[8/16] RTSP main stream ...")
            await self._probe_rtsp(report, channel_count, subtype=0)
        else:
            self.progress("[8/16] RTSP main stream skipped (--main-stream to enable)")

        # 9-13. event stream
        await self._probe_event_stream(client, report)

        # 14. channel index mapping
        self._resolve_index_base(report)

        # 15. playback
        if self.options.test_playback:
            self.progress("[15/16] RTSP playback ...")
            await self._probe_playback(report)
        else:
            self.progress("[15/16] Playback skipped")

    def _channel_count(self, report: ProbeReport) -> int:
        detected = report.identity.channel_count
        if detected and detected > 0:
            return min(detected, self.options.channels)
        return self.options.channels

    async def _probe_snapshots(
        self, client: DahuaCgiClient, report: ProbeReport, channel_count: int
    ) -> None:
        base, attempts = await detect_snapshot_channel_base(client, logical_channel=1)
        report.snapshot_channel_base = base
        if base is None:
            report.snapshot = Capability.UNSUPPORTED
            errors = "; ".join(str(v[2]) for v in attempts.values() if v[2])
            report.fail(f"snapshot.cgi returned no JPEG on channel 1 or 0: {errors}")
            for logical in range(1, channel_count + 1):
                report.channels.append(
                    ChannelProbeResult(logical_channel=logical, snapshot_error="base unknown")
                )
            return

        report.snapshot = Capability.SUPPORTED
        for logical in range(1, channel_count + 1):
            vendor_channel = logical - 1 + base
            ok, size, error = attempts.get(vendor_channel) or await try_snapshot(
                client, vendor_channel
            )
            report.channels.append(
                ChannelProbeResult(
                    logical_channel=logical,
                    snapshot_channel=vendor_channel,
                    snapshot_ok=ok,
                    snapshot_bytes=size,
                    snapshot_error=error,
                )
            )
            self.progress(
                f"        ch{logical} (vendor {vendor_channel}): "
                + (f"OK {size} bytes" if ok else f"FAIL {error}")
            )

    async def _probe_rtsp(self, report: ProbeReport, channel_count: int, *, subtype: int) -> None:
        if not report.ffprobe_available:
            report.rtsp = Capability.UNKNOWN
            return

        semaphore = asyncio.Semaphore(self.options.max_parallel_channel_tests)
        # Dahua documents RTSP channels as 1-based; confirmed here, not assumed.
        base = 1

        async def probe_one(entry: ChannelProbeResult) -> None:
            vendor_channel = entry.logical_channel - 1 + base
            async with semaphore:
                result = await probe_live_stream(
                    self.options.host,
                    vendor_channel,
                    self.options.credentials,
                    subtype=subtype,
                    port=self.options.rtsp_port,
                )
            capability = Capability.from_bool(result.ok)
            if subtype == 1:
                entry.rtsp_sub_ok = capability
            else:
                entry.rtsp_main_ok = capability
            if result.ok:
                entry.rtsp_codec = result.codec
                entry.rtsp_resolution = result.resolution
            else:
                entry.rtsp_error = result.error
            self.progress(
                f"        ch{entry.logical_channel} subtype={subtype}: "
                + (
                    f"OK {result.codec} {result.resolution}"
                    if result.ok
                    else f"FAIL {result.error}"
                )
            )

        entries = report.channels or [
            ChannelProbeResult(logical_channel=i) for i in range(1, channel_count + 1)
        ]
        if not report.channels:
            report.channels = entries
        await asyncio.gather(*(probe_one(entry) for entry in entries))

        any_ok = any(
            (entry.rtsp_sub_ok if subtype == 1 else entry.rtsp_main_ok).is_supported
            for entry in entries
        )
        if any_ok:
            report.rtsp = Capability.SUPPORTED
            report.rtsp_channel_base = base
            codecs = {e.rtsp_codec for e in entries if e.rtsp_codec}
            if codecs & {"hevc", "h265"}:
                report.warn(
                    "H.265 stream detected - WebRTC/browser playback may need transcoding "
                    "(spec §62.5)"
                )
        elif report.rtsp is not Capability.SUPPORTED:
            report.rtsp = Capability.UNSUPPORTED

    async def _probe_event_stream(self, client: DahuaCgiClient, report: ProbeReport) -> None:
        seconds = self.options.listen_seconds
        self.progress(f"[9/16] Attaching to event stream for {seconds:.0f}s ...")
        self.progress("")
        self.progress("        >>> [10/16] TRIGGER SMD NOW <<<")
        self.progress("        Walk in front of a camera (human), then drive/move a vehicle.")
        if self.options.trigger_channel:
            self.progress(
                f"        Use logical channel {self.options.trigger_channel} "
                "so channel mapping can be verified."
            )
        self.progress("        SMD Plus must be enabled on the XVR for that channel.")
        self.progress("")

        def on_event(event: DahuaRawEvent) -> None:
            if event.is_heartbeat:
                return
            self.progress(f"        <- {event.code} action={event.action} index={event.index}")

        try:
            events = await collect_events(
                client,
                duration_seconds=seconds,
                codes=self.options.event_codes,
                heartbeat_seconds=self.options.heartbeat_seconds,
                on_event=on_event,
            )
        except DahuaError as exc:
            report.event_stream = Capability.UNSUPPORTED
            report.fail(f"event stream attach failed: {exc}")
            return

        report.event_stream = Capability.from_bool(bool(events))
        if not events:
            report.fail(
                "no data on the attach stream (not even a heartbeat) - "
                "check firmware support and credentials"
            )
            return

        # 11. observed codes
        counts: dict[str, int] = {}
        for event in events:
            counts[event.code] = counts.get(event.code, 0) + 1
        report.observed_event_codes = dict(sorted(counts.items()))
        # Face payloads are never written to the report file (spec §47).
        report.observed_events = [
            {
                "code": e.code,
                "action": e.action,
                "index": e.index,
                "received_at": e.received_at.isoformat(),
                "data": {} if is_blocked_code(e.code) else e.data,
            }
            for e in events
            if not e.is_heartbeat
        ][:200]

        blocked = sorted({e.code for e in events if is_blocked_code(e.code)})
        if blocked:
            report.warn(
                f"device is running face detection ({', '.join(blocked)}). V1 does no face "
                "processing: the edge drops these events and their payload is not stored. "
                "Disable the feature on the recorder unless the customer has a legal basis "
                "for it (spec §47)."
            )

        # 12-13. SMD verdicts
        report.smart_motion_human = Capability.from_bool(HUMAN_CODE in counts)
        report.smart_motion_vehicle = Capability.from_bool(VEHICLE_CODE in counts)
        # Absence proves nothing here: nobody may have unplugged a camera during the
        # probe window, so these stay UNKNOWN unless actually observed.
        report.video_loss_events = (
            Capability.SUPPORTED if "VideoLoss" in counts else Capability.UNKNOWN
        )
        report.video_blind_events = (
            Capability.SUPPORTED if "VideoBlind" in counts else Capability.UNKNOWN
        )
        report.storage_events = (
            Capability.SUPPORTED if STORAGE_CODES & counts.keys() else Capability.UNKNOWN
        )

        if HUMAN_CODE not in counts:
            report.fail(
                f"{HUMAN_CODE} never arrived - enable SMD Plus + Human on the XVR and re-run "
                "(spec §62.3)"
            )
        if VEHICLE_CODE not in counts:
            report.warn(
                f"{VEHICLE_CODE} never arrived - vehicle SMD may be disabled or untriggered"
            )
        for code in report.exposure_events:
            if code in {HUMAN_CODE, VEHICLE_CODE} and code not in counts:
                report.warn(f"device advertises {code} but none was observed during the probe")

    def _resolve_index_base(self, report: ProbeReport) -> None:
        """Step 14 — how the device numbers channels in `index=`."""
        indices = [e["index"] for e in report.observed_events if isinstance(e.get("index"), int)]
        if not indices:
            report.warn("no indexed events observed - event_index_base stays unknown")
            return

        trigger = self.options.trigger_channel
        if trigger:
            human = [
                e["index"]
                for e in report.observed_events
                if e["code"] in (HUMAN_CODE, VEHICLE_CODE) and isinstance(e.get("index"), int)
            ]
            if human:
                observed = min(human)
                report.event_index_base = observed - (trigger - 1)
                self.progress(
                    f"[14/16] index base = {report.event_index_base} "
                    f"(logical ch{trigger} reported index={observed})"
                )
                return
            report.warn("no SMD event on the trigger channel - falling back to range inference")

        low = min(indices)
        if low == 0:
            report.event_index_base = 0
        elif low == 1 and max(indices) >= (report.identity.channel_count or 0):
            report.event_index_base = 1
        else:
            report.warn(
                f"ambiguous event index range (min={low}, max={max(indices)}) - "
                "re-run with --trigger-channel to pin the mapping"
            )
        if report.event_index_base is not None:
            self.progress(f"[14/16] index base = {report.event_index_base} (inferred from range)")

    async def _probe_playback(self, report: ProbeReport) -> None:
        if not report.ffprobe_available:
            report.rtsp_playback = Capability.UNKNOWN
            report.warn("playback not verified: ffprobe missing")
            return

        target = next(
            (c for c in report.channels if c.rtsp_sub_ok.is_supported or c.snapshot_ok),
            None,
        )
        if target is None:
            report.rtsp_playback = Capability.UNKNOWN
            report.warn("playback not verified: no working channel to test against")
            return

        vendor_channel = target.logical_channel - 1 + (report.rtsp_channel_base or 1)
        moment = datetime.now() - timedelta(minutes=self.options.playback_offset_minutes)
        window = build_clip_window(moment, pre_event_seconds=10, post_event_seconds=20)
        result = await probe_playback(
            self.options.host,
            vendor_channel,
            self.options.credentials,
            window,
            port=self.options.rtsp_port,
        )
        target.playback_ok = Capability.from_bool(result.ok)
        target.playback_error = result.error
        report.rtsp_playback = Capability.from_bool(result.ok)
        if result.ok:
            self.progress(
                f"        playback ch{target.logical_channel} OK ({window.start} .. {window.end})"
            )
        else:
            report.warn(
                f"playback failed on ch{target.logical_channel}: {result.error} - "
                "check the XVR recording schedule, then the media-file fallback (spec §5)"
            )

    @staticmethod
    async def _check_tcp(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except TimeoutError:
            return False, f"timeout after {timeout:.0f}s"
        except OSError as exc:
            return False, str(exc)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        del reader
        return True, "open"


def summarize(report: ProbeReport) -> str:
    """Human-readable verdict printed at the end of a probe run."""
    identity = report.identity
    lines = [
        "",
        "=" * 68,
        f" Nightshift Dahua probe — {report.host}",
        "=" * 68,
        f" model            : {identity.model or 'unknown'}",
        f" firmware         : {identity.firmware_version or 'unknown'}",
        f" serial           : {identity.serial_number or 'unknown'}",
        f" channels         : {identity.channel_count or 'unknown'}",
        "",
        f" digest auth      : {report.digest_auth.value}",
        f" cgi              : {report.cgi.value}",
        f" event stream     : {report.event_stream.value}",
        f" SmartMotionHuman : {report.smart_motion_human.value}",
        f" SmartMotionVehicle: {report.smart_motion_vehicle.value}",
        f" snapshot         : {report.snapshot.value} (base={report.snapshot_channel_base})",
        f" rtsp live        : {report.rtsp.value} (base={report.rtsp_channel_base})",
        f" rtsp playback    : {report.rtsp_playback.value}",
        f" event index base : {report.event_index_base}",
        "",
    ]
    if report.observed_event_codes:
        lines.append(" observed event codes:")
        lines.extend(
            f"   {code:<28} x{count}" for code, count in report.observed_event_codes.items()
        )
        lines.append("")
    if report.warnings:
        lines.append(" warnings:")
        lines.extend(f"   ! {w}" for w in report.warnings)
        lines.append("")
    if report.errors:
        lines.append(" BLOCKERS:")
        lines.extend(f"   x {e}" for e in report.errors)
        lines.append("")
    verdict = "PASS" if not report.errors else "FAIL"
    lines.append(f" verdict: {verdict}   ({report.duration_seconds:.1f}s)")
    lines.append("=" * 68)
    return "\n".join(lines)


def example_commands(host: str, username: str = "USER") -> list[str]:
    """The equivalent manual curl/ffprobe commands (spec §51), credential-free."""
    return [
        f"curl --digest -u '{username}:PASS' "
        f"'http://{host}/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5'",
        f"curl --digest -u '{username}:PASS' "
        f"'http://{host}/cgi-bin/snapshot.cgi?channel=1' --output snapshot.jpg",
        f"curl --digest -u '{username}:PASS' "
        f"'http://{host}/cgi-bin/eventManager.cgi?action=getExposureEvents'",
        f"ffprobe -rtsp_transport tcp '{build_live_url(host, 1, subtype=1)}'",
    ]
