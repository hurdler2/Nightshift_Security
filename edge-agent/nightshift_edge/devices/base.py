"""Vendor-neutral device adapter contract.

The cloud and the mobile app must never learn which adapter served a request
(spec §6): they see logical channels, normalized events and signed media URLs only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from nightshift_edge.devices.dahua.models import (
    DeviceIdentity,
    NormalizedEvent,
    ProbeReport,
)


@runtime_checkable
class DeviceAdapter(Protocol):
    """What every recorder integration must provide."""

    vendor: str

    async def probe(self) -> ProbeReport:
        """Interrogate the device and report only verified capabilities."""
        ...

    async def identity(self) -> DeviceIdentity:
        """Manufacturer / model / serial / firmware."""
        ...

    def events(self) -> AsyncIterator[NormalizedEvent]:
        """Infinite, self-reconnecting stream of normalized events."""
        ...

    async def snapshot(self, logical_channel: int) -> bytes:
        """JPEG bytes for a logical (1-based) channel."""
        ...

    async def live_url(self, logical_channel: int, *, high_quality: bool = False) -> str:
        """Credentialed stream URL for local use only — never leaves the edge."""
        ...

    async def clip_url(
        self,
        logical_channel: int,
        occurred_at: datetime,
        *,
        pre_event_seconds: int = 10,
        post_event_seconds: int = 20,
    ) -> str:
        """Credentialed playback URL for an event clip — never leaves the edge."""
        ...
