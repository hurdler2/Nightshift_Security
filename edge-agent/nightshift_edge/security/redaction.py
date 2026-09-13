"""Credential scrubbing for logs, errors and telemetry.

Spec §33: XVR credentials must never appear in a log line, a cloud payload, or a
mobile response. RTSP URLs embed `user:pass@` by construction, so every path that
can print a URL routes through :func:`redact_url`.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***"

#: `scheme://user:pass@host` — the RTSP/HTTP userinfo section.
_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^/@\s:]+)(:(?P<pw>[^/@\s]*))?@"
)
#: `password=...`, `pwd=...`, `secret=...` in a query string or key=value dump.
_QUERY_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|authorization|apikey|api_key)=([^&;\s\"']*)"
)
#: `-u user:pass` style CLI arguments.
_CURL_USER_RE = re.compile(r"(?i)(-u\s+|--user\s+)(['\"]?)([^\s'\"]+)")

_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
    "apikey",
    "credentials",
    "enrollment_token",
    "edge_secret_key",
}


def redact_url(url: str) -> str:
    """Strip userinfo and secret query parameters from a URL."""
    if not url:
        return url
    safe = _USERINFO_RE.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", url)
    return _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", safe)


def redact_text(text: str) -> str:
    """Best-effort scrub of an arbitrary string (error messages, command lines)."""
    if not text:
        return text
    safe = _USERINFO_RE.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)
    safe = _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", safe)
    return _CURL_USER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", safe)


def redact_mapping(data: Any, *, _depth: int = 0) -> Any:
    """Recursively replace sensitive values in dict/list structures."""
    if _depth > 12:
        return data
    if isinstance(data, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS
                else redact_mapping(value, _depth=_depth + 1)
            )
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return type(data)(redact_mapping(item, _depth=_depth + 1) for item in data)
    if isinstance(data, str):
        return redact_text(data)
    return data


def redact_command(argv: list[str]) -> list[str]:
    """Sanitize an argv list before logging it (ffprobe/ffmpeg carry RTSP creds)."""
    return [redact_text(redact_url(arg)) for arg in argv]
