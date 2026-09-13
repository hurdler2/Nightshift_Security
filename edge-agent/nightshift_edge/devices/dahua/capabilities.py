"""Device identity and capability discovery (spec §2.2).

Every call here is best-effort: old firmwares are missing endpoints, and a missing
endpoint must degrade to ``UNKNOWN`` rather than an exception or a fabricated value.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from nightshift_edge.devices.dahua.cgi_client import (
    DahuaCgiClient,
    DahuaError,
    parse_kv_response,
)
from nightshift_edge.devices.dahua.models import DeviceIdentity

log = logging.getLogger(__name__)

MAGICBOX = "magicBox.cgi"
CONFIG_MANAGER = "configManager.cgi"
EVENT_MANAGER = "eventManager.cgi"
GLOBAL_CGI = "global.cgi"
STORAGE_CGI = "storageDevice.cgi"

_CHANNEL_TITLE_RE = re.compile(r"table\.ChannelTitle\[(\d+)\]\.Name", re.IGNORECASE)
_EVENT_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


async def _safe_text(client: DahuaCgiClient, path: str, params: dict[str, str]) -> str | None:
    try:
        return await client.get_text(path, params)
    except DahuaError as exc:
        log.debug("dahua endpoint unavailable %s %s: %s", path, params, exc)
        return None


async def fetch_device_identity(client: DahuaCgiClient) -> DeviceIdentity:
    """Collect manufacturer/model/serial/firmware. Missing fields stay ``None``."""
    identity = DeviceIdentity(http_port=client.port)

    vendor = await _safe_text(client, MAGICBOX, {"action": "getVendor"})
    if vendor:
        identity.manufacturer = parse_kv_response(vendor).get("vendor") or "Dahua"
    else:
        identity.manufacturer = "Dahua"

    device_type = await _safe_text(client, MAGICBOX, {"action": "getDeviceType"})
    if device_type:
        values = parse_kv_response(device_type)
        identity.model = values.get("type") or values.get("deviceType")
        identity.device_type = identity.model

    serial = await _safe_text(client, MAGICBOX, {"action": "getSerialNo"})
    if serial:
        identity.serial_number = parse_kv_response(serial).get("sn")

    software = await _safe_text(client, MAGICBOX, {"action": "getSoftwareVersion"})
    if software:
        values = parse_kv_response(software)
        identity.firmware_version = values.get("version")
        identity.build_date = values.get("BuildDate") or values.get("build")

    system = await _safe_text(client, MAGICBOX, {"action": "getSystemInfo"})
    if system:
        values = parse_kv_response(system)
        identity.model = identity.model or values.get("deviceType")
        identity.serial_number = identity.serial_number or values.get("serialNumber")
        identity.web_version = values.get("updateSerial") or values.get("hardwareVersion")

    identity.channel_count = await fetch_channel_count(client)
    identity.rtsp_port = await fetch_rtsp_port(client)
    return identity


async def fetch_channel_count(client: DahuaCgiClient) -> int | None:
    """Number of video-in channels, from ChannelTitle config or the device caps."""
    titles = await _safe_text(
        client, CONFIG_MANAGER, {"action": "getConfig", "name": "ChannelTitle"}
    )
    if titles:
        indices = {int(m.group(1)) for m in _CHANNEL_TITLE_RE.finditer(titles)}
        if indices:
            return max(indices) + 1

    caps = await _safe_text(client, MAGICBOX, {"action": "getProductDefinition"})
    if caps:
        values = parse_kv_response(caps)
        for key in ("table.ProductDefinition.MaxRemoteInputChannels", "MaxChannel"):
            raw = values.get(key)
            if raw and raw.isdigit():
                return int(raw)
    return None


async def fetch_channel_titles(client: DahuaCgiClient) -> dict[int, str]:
    """Map 0-based device channel index -> configured camera name."""
    titles = await _safe_text(
        client, CONFIG_MANAGER, {"action": "getConfig", "name": "ChannelTitle"}
    )
    if not titles:
        return {}
    result: dict[int, str] = {}
    for key, value in parse_kv_response(titles).items():
        match = _CHANNEL_TITLE_RE.search(key)
        if match and value:
            result[int(match.group(1))] = value
    return result


async def fetch_rtsp_port(client: DahuaCgiClient) -> int | None:
    config = await _safe_text(client, CONFIG_MANAGER, {"action": "getConfig", "name": "RTSP"})
    if not config:
        return None
    for key, value in parse_kv_response(config).items():
        if key.lower().endswith(".port") and value.isdigit():
            return int(value)
    return None


async def fetch_event_caps(client: DahuaCgiClient) -> str | None:
    """`eventManager.cgi?action=getCaps` — absent on several older firmwares."""
    return await _safe_text(client, EVENT_MANAGER, {"action": "getCaps"})


async def fetch_exposure_events(client: DahuaCgiClient) -> list[str]:
    """Event codes the device claims to expose.

    Returns an empty list when the endpoint is missing — that is *not* evidence that
    the device lacks the events; the attach probe decides (spec §2.2).
    """
    text = await _safe_text(client, EVENT_MANAGER, {"action": "getExposureEvents"})
    if not text:
        return []
    codes: set[str] = set()
    for line in text.splitlines():
        _, _, value = line.partition("=")
        for token in _EVENT_TOKEN_RE.findall(value or line):
            if token.lower() not in {"events", "result", "true", "false"}:
                codes.add(token)
    return sorted(codes)


@dataclass(slots=True)
class DeviceClock:
    device_time: datetime | None
    ntp_enabled: bool | None
    ntp_server: str | None
    skew_seconds: float | None


async def fetch_device_clock(client: DahuaCgiClient, *, now: datetime | None = None) -> DeviceClock:
    """Read device time and NTP config; compute skew against the edge clock.

    Clock skew silently corrupts event timestamps and makes playback windows point at
    the wrong footage (spec §62.9), so onboarding fails loudly on a large skew.
    """
    device_time: datetime | None = None
    text = await _safe_text(client, GLOBAL_CGI, {"action": "getCurrentTime"})
    if text:
        raw = parse_kv_response(text).get("result") or text.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y_%m_%d_%H_%M_%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                device_time = datetime.strptime(raw.strip(), fmt)
                break
            except ValueError:
                continue

    ntp_enabled: bool | None = None
    ntp_server: str | None = None
    ntp_text = await _safe_text(client, CONFIG_MANAGER, {"action": "getConfig", "name": "NTP"})
    if ntp_text:
        values = parse_kv_response(ntp_text)
        for key, value in values.items():
            lowered = key.lower()
            if lowered.endswith(".enable"):
                ntp_enabled = value.strip().lower() == "true"
            elif lowered.endswith(".address"):
                ntp_server = value

    skew: float | None = None
    if device_time is not None:
        reference = now or datetime.now()
        if reference.tzinfo is not None:
            reference = reference.astimezone().replace(tzinfo=None)
        skew = (reference - device_time).total_seconds()

    return DeviceClock(
        device_time=device_time,
        ntp_enabled=ntp_enabled,
        ntp_server=ntp_server,
        skew_seconds=skew,
    )


async def fetch_storage_info(client: DahuaCgiClient) -> dict[str, str]:
    """Raw storage key/values (used by PHASE 10 health monitoring)."""
    text = await _safe_text(client, STORAGE_CGI, {"action": "getDeviceAllInfo"})
    return parse_kv_response(text) if text else {}
