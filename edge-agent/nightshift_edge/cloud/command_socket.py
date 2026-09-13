"""Outbound WebSocket command channel (spec §39, §60).

The cloud cannot reach the gateway; the gateway dials out and keeps the socket open,
which is how live-view requests and CONFIG_UPDATED reach a CGNAT'd site.
"""

from __future__ import annotations

COMMAND_CONFIG_UPDATED = "CONFIG_UPDATED"
COMMAND_START_LIVE = "START_LIVE_SESSION"
COMMAND_STOP_LIVE = "STOP_LIVE_SESSION"
COMMAND_PROBE_DEVICE = "PROBE_DEVICE"
COMMAND_FETCH_CLIP = "FETCH_CLIP"


# TODO(V1-BLOCKER): PHASE 2 — reconnecting WSS client with authenticated frames and
# an idempotent command handler backed by `pending_commands`.
