"""Dahua XVR/NVR integration (CGI primary, NetSDK fallback)."""

from nightshift_edge.devices.dahua.models import (
    DahuaRawEvent,
    NormalizedEvent,
    ProbeReport,
)

__all__ = ["DahuaRawEvent", "NormalizedEvent", "ProbeReport"]
