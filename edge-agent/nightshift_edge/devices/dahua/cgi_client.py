"""Async HTTP Digest client for the Dahua CGI API.

Everything the edge does against an XVR goes through here, so this is also the single
place that guarantees credentials never reach a log line (spec §33).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx

from nightshift_edge.security.redaction import redact_url

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
#: The attach stream is long-lived: no read timeout, the heartbeat watchdog owns it.
STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)


class DahuaError(Exception):
    """Base class for Dahua adapter failures."""


class DahuaAuthError(DahuaError):
    """401/403 — wrong credentials, or the account is locked out by the device."""


class DahuaUnsupportedError(DahuaError):
    """The firmware does not implement this endpoint (400/404/501, or `Error` body)."""


class DahuaTransportError(DahuaError):
    """Connect/read failure — the device is unreachable or dropped us."""


@dataclass(frozen=True, slots=True)
class DahuaCredentials:
    """XVR service account. Never serialize this into cloud payloads or logs."""

    username: str
    password: str

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"DahuaCredentials(username={self.username!r}, password='***')"


class DahuaCgiClient:
    """Thin async wrapper over `/cgi-bin/*.cgi` with Digest auth.

    Usage::

        async with DahuaCgiClient("192.168.1.108", credentials) as client:
            text = await client.get_text("magicBox.cgi", {"action": "getDeviceType"})
    """

    def __init__(
        self,
        host: str,
        credentials: DahuaCredentials,
        *,
        port: int = 80,
        scheme: str = "http",
        timeout: httpx.Timeout | None = None,
        verify_tls: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.scheme = scheme
        self._credentials = credentials
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            auth=httpx.DigestAuth(credentials.username, credentials.password),
            timeout=timeout or DEFAULT_TIMEOUT,
            verify=verify_tls,
            follow_redirects=False,
            headers={"User-Agent": "Nightshift-Edge/0.1"},
        )

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- urls ---------------------------------------------------------------
    @property
    def base_url(self) -> str:
        default_port = 443 if self.scheme == "https" else 80
        host = self.host if self.port == default_port else f"{self.host}:{self.port}"
        return f"{self.scheme}://{host}"

    def url(self, path: str) -> str:
        path = path.lstrip("/")
        if not path.startswith("cgi-bin/"):
            path = f"cgi-bin/{path}"
        return f"{self.base_url}/{path}"

    # -- requests -----------------------------------------------------------
    async def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
    ) -> httpx.Response:
        url = self.url(path)
        try:
            response = await self._client.request(method, url, params=params)
        except httpx.TimeoutException as exc:
            raise DahuaTransportError(f"timeout calling {redact_url(url)}") from exc
        except httpx.HTTPError as exc:
            raise DahuaTransportError(f"transport error calling {redact_url(url)}: {exc}") from exc
        self._raise_for_status(response, url)
        return response

    async def get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        response = await self.request(path, params)
        text = response.text
        if text.strip().lower().startswith("error"):
            raise DahuaUnsupportedError(
                f"{redact_url(self.url(path))} returned: {text.strip()[:200]}"
            )
        return text

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        response = await self.request(path, params)
        return response.content

    @asynccontextmanager
    async def stream(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        raw_query: str | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Open a long-lived streaming GET (used by the event listener).

        ``raw_query`` bypasses query encoding: Dahua's `codes=[All]` and
        `codes=[SmartMotionHuman,SmartMotionVehicle]` must keep their literal
        brackets and commas on some firmwares.
        """
        url = self.url(path)
        if raw_query:
            url = f"{url}?{raw_query}"
            params = None
        request = self._client.build_request(
            "GET",
            url,
            params=params,
            timeout=timeout or STREAM_TIMEOUT,
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise DahuaTransportError(f"timeout opening stream {redact_url(url)}") from exc
        except httpx.HTTPError as exc:
            raise DahuaTransportError(f"cannot open stream {redact_url(url)}: {exc}") from exc
        try:
            self._raise_for_status(response, url)
            yield response
        finally:
            await response.aclose()

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _raise_for_status(response: httpx.Response, url: str) -> None:
        status = response.status_code
        if status < 400:
            return
        safe_url = redact_url(url)
        if status in (401, 403):
            raise DahuaAuthError(f"digest auth rejected ({status}) for {safe_url}")
        if status in (400, 404, 405, 501):
            raise DahuaUnsupportedError(f"endpoint unsupported ({status}): {safe_url}")
        raise DahuaError(f"unexpected status {status} for {safe_url}")


def parse_kv_response(text: str) -> dict[str, str]:
    """Parse Dahua's `key=value` line format into a flat dict.

    ``table.General.Name=foo`` stays a flat key; callers pick what they need.
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result
