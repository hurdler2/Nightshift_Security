"""Thin async wrapper around `ffprobe`.

Used to verify that an RTSP URL actually delivers decodable video before we tell the
cloud a camera is streamable. Every log line goes through redaction because RTSP URLs
carry `user:pass@` (spec §33).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

from nightshift_edge.security.redaction import redact_command, redact_url

log = logging.getLogger(__name__)

FFPROBE_BIN = "ffprobe"


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: str | None = None
    duration: float | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def resolution(self) -> str | None:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None


def ffprobe_available() -> bool:
    return shutil.which(FFPROBE_BIN) is not None


async def probe_stream(
    url: str,
    *,
    timeout: float = 15.0,
    transport: str = "tcp",
    analyze_seconds: float = 3.0,
) -> ProbeResult:
    """Run ffprobe against an RTSP/file URL and report the first video stream."""
    if not ffprobe_available():
        return ProbeResult(ok=False, error="ffprobe not installed")

    micros = int(analyze_seconds * 1_000_000)
    argv = [
        FFPROBE_BIN,
        "-v",
        "error",
        "-rtsp_transport",
        transport,
        "-analyzeduration",
        str(micros),
        "-probesize",
        str(max(500_000, micros)),
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate:format=duration",
        "-of",
        "json",
        url,
    ]
    log.debug("ffprobe %s", " ".join(redact_command(argv)))

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return ProbeResult(ok=False, error="ffprobe not installed")
    except OSError as exc:
        return ProbeResult(ok=False, error=f"cannot start ffprobe: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        return ProbeResult(ok=False, error=f"ffprobe timed out after {timeout:.0f}s")

    if process.returncode != 0:
        message = stderr.decode("utf-8", "replace").strip().splitlines()
        detail = message[-1] if message else f"exit {process.returncode}"
        return ProbeResult(ok=False, error=redact_url(detail)[:300])

    try:
        payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return ProbeResult(ok=False, error="ffprobe returned unparsable json")

    streams = payload.get("streams") or []
    if not streams:
        return ProbeResult(ok=False, error="no video stream found", raw=payload)

    stream = streams[0]
    duration_raw = (payload.get("format") or {}).get("duration")
    try:
        duration = float(duration_raw) if duration_raw not in (None, "N/A") else None
    except (TypeError, ValueError):
        duration = None

    return ProbeResult(
        ok=True,
        codec=stream.get("codec_name"),
        width=stream.get("width"),
        height=stream.get("height"),
        fps=stream.get("avg_frame_rate"),
        duration=duration,
        raw=payload,
    )
