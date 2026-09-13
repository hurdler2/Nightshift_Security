"""Dahua NetSDK fallback adapter (spec §6).

The CGI adapter is primary. This fallback exists for firmwares where SMD events do
not arrive over `eventManager.cgi` — a real risk (spec §62.2/62.3) that we can only
settle against the pilot devices.

Scope when implemented (verified NetSDK function families):

    CLIENT_Init, CLIENT_LoginEx2, CLIENT_SetAutoReconnect, CLIENT_SetDVRMessCallBack,
    CLIENT_StartListenEx, CLIENT_StopListen, CLIENT_RealPlayEx, CLIENT_SnapPictureEx,
    CLIENT_DownloadByTimeEx, CLIENT_QueryRecordFile, CLIENT_Logout, CLIENT_Cleanup

It stays behind a feature flag and must expose exactly the `DeviceAdapter` surface,
so nothing above the edge can tell which adapter served a request.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

FEATURE_FLAG = "EDGE_ENABLE_DAHUA_NETSDK"


class DahuaNetSdkUnavailable(RuntimeError):
    """Raised when the NetSDK adapter is requested but not built."""


def is_available() -> bool:
    """The vendor shared library is not bundled; the CGI path is the supported one."""
    return False


class DahuaNetSdkAdapter:
    """Placeholder that fails loudly instead of pretending to work."""

    vendor = "dahua"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise DahuaNetSdkUnavailable(
            "Dahua NetSDK fallback is not implemented yet. Use the CGI adapter, and if a "
            "pilot device fails to deliver SMD events over CGI, escalate before enabling "
            f"{FEATURE_FLAG}."
        )


# TODO(V1-NONBLOCKER): implement the NetSDK fallback only if a pilot XVR is proven to
# withhold SmartMotionHuman over CGI. Requires the vendor .so/.dll per architecture,
# a ctypes binding layer, and a licence review before shipping in the SaaS image.
