"""HTTPS client for the edge -> cloud API (spec §39).

Outbound only: the gateway never listens for inbound connections, which is what makes
the deployment CGNAT-safe (spec §9).

PHASE 2 implements enrollment + heartbeat + config pull; PHASE 4 event/media upload.
"""

from __future__ import annotations

ENROLL_PATH = "/internal/edge/v1/enroll"
HEARTBEAT_PATH = "/internal/edge/v1/heartbeat"
EVENTS_PATH = "/internal/edge/v1/events"
MEDIA_PRESIGN_PATH = "/internal/edge/v1/media/presign"
MEDIA_COMPLETE_PATH = "/internal/edge/v1/media/complete"
DEVICE_HEALTH_PATH = "/internal/edge/v1/device-health"
CONFIG_PATH = "/internal/edge/v1/config"
COMMANDS_WS_PATH = "/internal/edge/v1/commands"


# TODO(V1-BLOCKER): PHASE 2 — machine-identity auth (enrollment code -> device token),
# token rotation, heartbeat with config_revision, and exponential backoff on failure.
