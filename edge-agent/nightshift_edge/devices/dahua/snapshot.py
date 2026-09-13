"""Snapshot capture via `/cgi-bin/snapshot.cgi`.

Channel numbering warning (spec §3.1): the HTTP API v2.63 document says the snapshot
channel is 1-based, but 0-based behaviour exists in the wild. Nothing here hard-codes
a base - :func:`detect_snapshot_channel_base` probes the device and the result is
persisted in ``device_channel_map``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nightshift_edge.devices.dahua.cgi_client import (
    DahuaCgiClient,
    DahuaError,
    DahuaUnsupportedError,
)

log = logging.getLogger(__name__)

SNAPSHOT_PATH = "snapshot.cgi"
JPEG_MAGIC = b"\xff\xd8\xff"
#: Anything smaller is a firmware error page or a black/unconnected channel stub.
MIN_JPEG_BYTES = 1024


class SnapshotError(DahuaError):
    """Snapshot could not be captured for this channel."""


@dataclass(slots=True)
class Snapshot:
    channel: int
    vendor_channel: int
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


def is_jpeg(payload: bytes) -> bool:
    return len(payload) >= MIN_JPEG_BYTES and payload.startswith(JPEG_MAGIC)


async def fetch_snapshot(
    client: DahuaCgiClient,
    vendor_channel: int,
    *,
    logical_channel: int | None = None,
    subtype: int | None = None,
) -> Snapshot:
    """Fetch one JPEG. Raises :class:`SnapshotError` unless real JPEG bytes arrive."""
    params: dict[str, int] = {"channel": vendor_channel}
    if subtype is not None:
        params["subtype"] = subtype
    try:
        payload = await client.get_bytes(SNAPSHOT_PATH, params)
    except DahuaUnsupportedError as exc:
        raise SnapshotError(f"snapshot.cgi unsupported: {exc}") from exc

    if not is_jpeg(payload):
        head = payload[:64].decode("utf-8", "replace").strip()
        raise SnapshotError(
            f"channel {vendor_channel} returned {len(payload)} bytes, not JPEG ({head!r})"
        )
    return Snapshot(
        channel=logical_channel if logical_channel is not None else vendor_channel,
        vendor_channel=vendor_channel,
        content=payload,
    )


async def try_snapshot(client: DahuaCgiClient, vendor_channel: int) -> tuple[bool, int, str | None]:
    """Non-raising probe helper: ``(ok, byte_count, error)``."""
    try:
        snap = await fetch_snapshot(client, vendor_channel)
    except (SnapshotError, DahuaError) as exc:
        return False, 0, str(exc)
    return True, snap.size, None


async def detect_snapshot_channel_base(
    client: DahuaCgiClient,
    *,
    logical_channel: int = 1,
) -> tuple[int | None, dict[int, tuple[bool, int, str | None]]]:
    """Decide whether the device numbers snapshot channels from 1 or from 0.

    Tries ``logical_channel`` first (the documented 1-based case), then
    ``logical_channel - 1``. Returns ``(base, attempts)`` where ``base`` is ``1``,
    ``0``, or ``None`` when neither worked - never a guess (spec §0.3).
    """
    attempts: dict[int, tuple[bool, int, str | None]] = {}

    for candidate_base, vendor_channel in ((1, logical_channel), (0, logical_channel - 1)):
        if vendor_channel < 0:
            continue
        result = await try_snapshot(client, vendor_channel)
        attempts[vendor_channel] = result
        if result[0]:
            log.info(
                "snapshot channel base detected: %d (logical %d -> vendor %d)",
                candidate_base,
                logical_channel,
                vendor_channel,
            )
            return candidate_base, attempts

    log.warning(
        "snapshot channel base could not be determined for logical channel %d",
        logical_channel,
    )
    return None, attempts


def vendor_snapshot_channel(logical_channel: int, base: int) -> int:
    """Map a Nightshift logical channel (1-based) to the device's snapshot channel."""
    return logical_channel - 1 + base
