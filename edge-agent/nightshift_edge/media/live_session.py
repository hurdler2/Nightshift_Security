"""On-demand live sessions (spec §23).

No camera is streamed continuously: a session starts when a user asks, publishes
outbound to the media gateway, and closes on idle (60s) or the 5 minute cap.
"""

from __future__ import annotations

IDLE_TIMEOUT_SECONDS = 60
MAX_SESSION_SECONDS = 300


# TODO(V1-BLOCKER): PHASE 9 — RTSP pull from the XVR, outbound publish to MediaMTX,
# WebRTC/HLS handoff, and session teardown on idle/expiry.
