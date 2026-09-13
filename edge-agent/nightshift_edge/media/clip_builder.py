"""Event clip production (spec §5, §56).

Pull the playback window off the XVR and remux to a mobile-friendly MP4. Prefer
`-c copy`: transcoding 70 cameras' worth of clips is the fastest way to burn the CPU
budget, and H.264 passthrough plays natively on both mobile platforms.
"""

from __future__ import annotations

DEFAULT_PRE_EVENT_SECONDS = 10
DEFAULT_POST_EVENT_SECONDS = 20


# TODO(V1-BLOCKER): PHASE 8 — ffmpeg remux of the playback RTSP window into
# fragmented MP4, faststart, then upload through the media outbox. Fall back to the
# media-file download adapter, then the edge rolling buffer (spec §5 fallback order).
