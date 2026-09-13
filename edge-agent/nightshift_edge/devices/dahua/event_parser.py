"""Parser for Dahua `eventManager.cgi?action=attach` streams.

The device answers with `multipart/x-mixed-replace`; each part carries a payload of
the shape::

    Code=SmartMotionHuman;action=Start;index=0;data={ ... json ... }

Design notes
------------
* **Incremental.** ``feed()`` accepts arbitrary chunk boundaries — a payload may be
  split mid-JSON across TCP reads.
* **Zero added latency.** When a part declares ``Content-Length`` the body is emitted
  as soon as those bytes arrive; we never wait for the *next* boundary to close the
  previous event (that would delay every alarm by one event).
* **Never drop.** Unknown codes and unknown actions are surfaced with
  ``is_known_code`` / ``is_known_action`` set to ``False`` so the caller can log them
  (spec §12: "unknown action loglanıp drop edilmemelidir").
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from nightshift_edge.devices.dahua.models import DahuaRawEvent, EventAction, NormalizedEvent

log = logging.getLogger(__name__)

VENDOR = "dahua"

#: Dahua event code -> Nightshift normalized event type (spec §2.1, §12).
EVENT_CODE_MAP: dict[str, str] = {
    "SmartMotionHuman": "person_detected",
    "SmartMotionVehicle": "vehicle_detected",
    "VideoMotion": "motion_detected",
    "VideoMotionInfo": "motion_info",
    "VideoLoss": "video_loss",
    "VideoBlind": "video_blind",
    "StorageNotExist": "storage_not_exist",
    "StorageFailure": "storage_failure",
    "StorageLowSpace": "storage_low_space",
    "CrossLineDetection": "line_crossing",
    "CrossRegionDetection": "zone_intrusion",
    "VideoAbnormalDetection": "scene_abnormal",
    "NewFile": "new_recording_file",
    "AlarmLocal": "alarm_input",
    "TamperDetection": "camera_tamper",
    "SceneChange": "scene_change",
    "Heartbeat": "heartbeat",
    "KeepAlive": "heartbeat",
    "TimeChange": "device_time_changed",
    "NTPAdjustTime": "device_time_synced",
    "Reboot": "device_reboot",
    "RtspSessionDisconnect": "rtsp_session_disconnect",
    # IVS events that WizSense recorders (e.g. XVR5xxx-I3) can emit when the feature
    # is enabled. Mapped so they are not lost, but no rule may depend on them until a
    # probe confirms the device actually produces them.
    "WanderDetection": "loitering_detected",
    "LeftDetection": "abandoned_object",
    "TakenAwayDetection": "object_removed",
    "ParkingDetection": "illegal_parking",
}

#: Face events are refused at the gateway and never reach the cloud.
#:
#: V1 does no face processing at all (spec §47) - person detection is anonymous object
#: detection. AI-capable recorders can nevertheless run recorder-side face detection,
#: and `codes=[All]` would then hand us payloads describing people's faces. Ingesting
#: that would start processing biometric-adjacent personal data we never agreed to
#: handle, so the edge drops these events and strips their payload.
BLOCKED_EVENT_CODES: frozenset[str] = frozenset(
    {
        "FaceDetection",
        "FaceRecognition",
        "FaceComparison",
        "FaceAnalysis",
        "HumanFaceDetect",
        "FaceCapture",
    }
)


def is_blocked_code(code: str) -> bool:
    """True for events the edge must never forward (face processing, spec §47)."""
    return code in BLOCKED_EVENT_CODES or code.lower().startswith("face")


ACTION_MAP: dict[str, EventAction] = {
    "start": EventAction.OPENED,
    "stop": EventAction.CLOSED,
    "pulse": EventAction.PULSE,
    "state": EventAction.PULSE,
}

#: Codes whose payload may carry an object classification (Human / Vehicle / ...).
_OBJECT_KEYS = ("Object", "Objects", "DetectObject")

_HEADER_SEP = re.compile(rb"\r?\n\r?\n")
_LINE_SEP = re.compile(rb"\r?\n")


def _looks_like_body(line: bytes) -> bool:
    """True for a payload line that appeared where a header was expected.

    Some firmwares omit the blank line between part headers and the body.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(b"--"):
        return False
    colon = stripped.find(b":")
    equals = stripped.find(b"=")
    if equals == -1:
        return False
    return colon == -1 or equals < colon


class _Incomplete:
    """Sentinel: the buffer does not yet hold a full part."""

    __slots__ = ()


_INCOMPLETE = _Incomplete()


class DahuaEventStreamParser:
    """Incremental multipart parser. Not thread-safe; one instance per connection."""

    #: Guard against a runaway/desynced stream eating memory.
    max_buffer_bytes = 1 << 20  # 1 MiB

    def __init__(self, boundary: str | bytes | None = None) -> None:
        self._buffer = bytearray()
        self._headers: dict[str, str] = {}
        self._expected: int | None = None
        self._in_body = False
        self._boundary: bytes | None = None
        if boundary:
            self.set_boundary(boundary)

    # -- boundary -----------------------------------------------------------
    def set_boundary(self, boundary: str | bytes) -> None:
        raw = boundary.encode() if isinstance(boundary, str) else boundary
        raw = raw.strip().strip(b'"')
        self._boundary = raw if raw.startswith(b"--") else b"--" + raw

    @staticmethod
    def boundary_from_content_type(content_type: str | None) -> str | None:
        """Extract the boundary token from a `Content-Type` header value."""
        if not content_type:
            return None
        for part in content_type.split(";"):
            key, _, value = part.strip().partition("=")
            if key.strip().lower() == "boundary":
                return value.strip().strip('"') or None
        return None

    # -- feeding ------------------------------------------------------------
    def feed(self, chunk: bytes) -> list[DahuaRawEvent]:
        """Push raw bytes; return every complete event contained in them."""
        if chunk:
            self._buffer.extend(chunk)
        if len(self._buffer) > self.max_buffer_bytes:
            log.warning(
                "dahua event buffer overflow (%d bytes) - resyncing stream",
                len(self._buffer),
            )
            self._resync()
        return list(self._drain())

    def flush(self) -> list[DahuaRawEvent]:
        """Emit whatever a closing stream left behind (no Content-Length case)."""
        events: list[DahuaRawEvent] = []
        if self._in_body and self._expected is None and self._buffer.strip():
            event = self._build(bytes(self._buffer), self._headers)
            self._buffer.clear()
            self._in_body = False
            self._headers = {}
            if event:
                events.append(event)
        return events

    def _resync(self) -> None:
        """Drop everything up to the last boundary we can find."""
        self._in_body = False
        self._expected = None
        self._headers = {}
        if self._boundary:
            idx = self._buffer.rfind(self._boundary)
            if idx > 0:
                del self._buffer[:idx]
                return
        self._buffer.clear()

    def _drain(self) -> Iterator[DahuaRawEvent]:
        while True:
            if not self._in_body and not self._consume_headers():
                return
            event = self._consume_body()
            if event is _INCOMPLETE:
                return
            if isinstance(event, DahuaRawEvent):
                yield event

    def _consume_headers(self) -> bool:
        # Skip keepalive newlines some firmwares emit between parts.
        while self._buffer[:1] in (b"\r", b"\n"):
            del self._buffer[:1]
        if not self._buffer:
            return False

        match = _HEADER_SEP.search(self._buffer)
        head = bytes(self._buffer[: match.start()]) if match else bytes(self._buffer)

        headers: dict[str, str] = {}
        position = 0
        while position <= len(head):
            separator = _LINE_SEP.search(head, position)
            line_start = position
            line = head[position : separator.start()] if separator else head[position:]
            position = separator.end() if separator else len(head) + 1
            stripped = line.strip()
            if not stripped or stripped.startswith(b"--"):
                continue
            if _looks_like_body(stripped):
                # Firmware skipped the blank separator: this line is already payload.
                del self._buffer[:line_start]
                self._headers = headers
                self._expected = None
                self._in_body = True
                return True
            key, _, value = stripped.partition(b":")
            if value:
                name = key.decode("ascii", "replace").strip().lower()
                headers[name] = value.decode("utf-8", "replace").strip()

        if not match:
            return False  # header block still incomplete

        del self._buffer[: match.end()]
        self._headers = headers
        self._in_body = True
        length = headers.get("content-length")
        try:
            self._expected = int(length) if length is not None else None
        except ValueError:
            self._expected = None
        if self._boundary is None:
            token = self.boundary_from_content_type(headers.get("content-type"))
            if token:
                self.set_boundary(token)
        return True

    def _consume_body(self) -> DahuaRawEvent | _Incomplete | None:
        if self._expected is not None:
            if len(self._buffer) < self._expected:
                return _INCOMPLETE
            body = bytes(self._buffer[: self._expected])
            del self._buffer[: self._expected]
        else:
            # No Content-Length: the part ends at the next boundary.
            marker = self._boundary or b"\r\n--"
            idx = self._buffer.find(marker)
            if idx == -1:
                return _INCOMPLETE
            body = bytes(self._buffer[:idx])
            del self._buffer[:idx]

        self._in_body = False
        headers, self._headers = self._headers, {}
        self._expected = None
        return self._build(body, headers)

    def _build(self, body: bytes, headers: dict[str, str] | None = None) -> DahuaRawEvent | None:
        text = body.decode("utf-8", "replace").strip()
        if not text:
            return None
        return parse_event_payload(text, headers or {})


def parse_event_payload(text: str, headers: dict[str, str] | None = None) -> DahuaRawEvent | None:
    """Parse one `Code=...;action=...;index=...;data={...}` payload."""
    text = text.strip()
    if not text or "Code=" not in text:
        return None

    data: dict[str, Any] = {}
    prefix = text
    marker = _find_data_marker(text)
    if marker is not None:
        prefix = text[:marker].rstrip(";")
        blob = text[marker + len("data=") :].strip()
        if blob:
            try:
                parsed = json.loads(blob)
                data = parsed if isinstance(parsed, dict) else {"value": parsed}
            except json.JSONDecodeError:
                log.debug("dahua event data is not valid json: %.200s", blob)
                data = {"_unparsed": blob}

    fields: dict[str, str] = {}
    for token in prefix.split(";"):
        key, sep, value = token.partition("=")
        if sep:
            fields[key.strip()] = value.strip()

    code = fields.get("Code")
    if not code:
        return None

    index: int | None
    try:
        index = int(fields["index"])
    except (KeyError, ValueError):
        index = None

    return DahuaRawEvent(
        code=code,
        action=fields.get("action", ""),
        index=index,
        data=data,
        raw=text,
        headers=headers or {},
    )


def _find_data_marker(text: str) -> int | None:
    """Index of the `data=` token that starts the JSON blob (not one inside it)."""
    if text.startswith("data="):
        return 0
    idx = text.find(";data=")
    return idx + 1 if idx != -1 else None


def normalize_event(
    event: DahuaRawEvent,
    *,
    event_index_base: int = 0,
    logical_channel_base: int = 1,
    channel_map: dict[int, int] | None = None,
    occurred_at: datetime | None = None,
) -> NormalizedEvent:
    """Turn a raw Dahua event into the vendor-neutral cloud payload.

    ``event_index_base`` is what the *device* uses in its `index=` field (0 on every
    firmware verified so far, but confirmed per device by the probe);
    ``logical_channel_base`` is what Nightshift shows to users (1).
    ``channel_map`` overrides the arithmetic entirely when onboarding discovered an
    irregular mapping.
    """
    code = event.code
    normalized_type = EVENT_CODE_MAP.get(code)
    is_known_code = normalized_type is not None
    if normalized_type is None:
        normalized_type = f"vendor_unknown:{code}"
        log.info("unmapped dahua event code=%s action=%s", code, event.action)

    # Belt and braces: the stream layer already drops these, so nothing should reach
    # here - but if a future code path normalizes one, it must not carry face payload.
    raw_data = event.data
    if is_blocked_code(code):
        normalized_type = "blocked:face_event"
        raw_data = {}

    action_key = (event.action or "").strip().lower()
    action = ACTION_MAP.get(action_key, EventAction.UNKNOWN)
    is_known_action = action_key in ACTION_MAP
    if not is_known_action and action_key:
        log.info("unknown dahua event action=%s (code=%s) - kept", event.action, code)

    logical: int | None = None
    if event.index is not None:
        if channel_map and event.index in channel_map:
            logical = channel_map[event.index]
        else:
            logical = event.index - event_index_base + logical_channel_base

    return NormalizedEvent(
        event_type=normalized_type,
        vendor=VENDOR,
        vendor_event_code=code,
        vendor_action=event.action,
        action=action,
        vendor_channel_index=event.index,
        logical_channel=logical,
        occurred_at=occurred_at or event.received_at or datetime.now(UTC),
        raw_data=raw_data,
        detected_object_type=extract_object_type(event.data),
        is_known_code=is_known_code,
        is_known_action=is_known_action,
    )


def extract_object_type(data: dict[str, Any]) -> str | None:
    """Best-effort object classification from the payload (`Human`, `Vehicle`, ...).

    Returns ``None`` when the firmware did not classify - we never guess.
    """
    # Key casing differs between firmwares ("Object" vs "object").
    lowered = {key.lower(): value for key, value in data.items() if isinstance(key, str)}
    for key in _OBJECT_KEYS:
        value = lowered.get(key.lower())
        if isinstance(value, dict):
            obj_type = value.get("ObjectType") or value.get("Type")
            if isinstance(obj_type, str) and obj_type:
                return obj_type
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    obj_type = item.get("ObjectType") or item.get("Type")
                    if isinstance(obj_type, str) and obj_type:
                        return obj_type
    obj_types = lowered.get("objecttypes")
    if isinstance(obj_types, list) and obj_types and isinstance(obj_types[0], str):
        return obj_types[0]
    return None
