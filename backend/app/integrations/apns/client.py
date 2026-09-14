"""Apple Push Notification service transport, token-based (spec §11.1).

Token auth rather than certificates: a `.p8` key does not expire, so nobody has to
remember to renew a certificate the night it silently stops waking guards up.

Two Apple rules shape this file:

* **HTTP/2 is mandatory.** APNs speaks nothing else, hence `httpx` with `http2=True`.
* **The provider token must be refreshed, but not too often.** Apple rejects tokens
  older than one hour and also rejects a provider that mints them faster than once
  every 20 minutes, so we cache and refresh at 45.

Alarms go out as `alert` push type at priority 10 with `interruption-level: critical`
requested. Apple only honours a critical alert if the app carries the entitlement; if
it does not, iOS degrades it to a normal alert rather than dropping it, so asking is
free. `apns-collapse-id` is deliberately *not* set: two intrusions a minute apart are
two events, and collapsing them would hide one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt

from app.modules.notifications.payload import PushPayload
from app.modules.notifications.transport import (
    Platform,
    TokenUnregistered,
    TransportError,
)

log = logging.getLogger(__name__)

PRODUCTION_HOST = "https://api.push.apple.com"
SANDBOX_HOST = "https://api.sandbox.push.apple.com"
#: Apple invalidates provider tokens after 60 minutes and throttles minting below 20.
TOKEN_TTL_SECONDS = 45 * 60

#: Apple's reasons for "this token is dead"; anything else is transient or our bug.
_DEAD_TOKEN_REASONS = frozenset({"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"})


@dataclass(frozen=True, slots=True)
class ApnsCredentials:
    """The four values from the Apple Developer portal."""

    key_id: str
    team_id: str
    private_key: str
    topic: str  # the app's bundle id
    use_sandbox: bool = False

    @classmethod
    def from_p8_file(
        cls, path: str | Path, *, key_id: str, team_id: str, topic: str, use_sandbox: bool = False
    ) -> ApnsCredentials:
        return cls(
            key_id=key_id,
            team_id=team_id,
            private_key=Path(path).read_text(encoding="utf-8"),
            topic=topic,
            use_sandbox=use_sandbox,
        )

    @property
    def host(self) -> str:
        return SANDBOX_HOST if self.use_sandbox else PRODUCTION_HOST


class ApnsTransport:
    """Sends one notification per call; the dispatcher handles fan-out and retries."""

    platform = Platform.IOS

    def __init__(
        self,
        credentials: ApnsCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 8.0,
    ) -> None:
        self._credentials = credentials
        self._client = client or httpx.AsyncClient(http2=True, timeout=timeout)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_minted_at: float = 0.0

    async def send(self, token: str, payload: PushPayload) -> str | None:
        url = f"{self._credentials.host}/3/device/{token}"
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": self._credentials.topic,
            "apns-push-type": "alert",
            "apns-priority": "10",  # deliver immediately; this is why the app exists
            "apns-expiration": str(int(time.time()) + 3600),
        }

        try:
            response = await self._client.post(url, json=build_message(payload), headers=headers)
        except httpx.HTTPError as exc:
            raise TransportError(f"apns request failed: {exc}", retryable=True) from exc

        if response.status_code == 200:
            return response.headers.get("apns-id")

        reason = _reason(response)

        if reason in _DEAD_TOKEN_REASONS:
            raise TokenUnregistered(f"apns: {reason}")
        if response.status_code in (403, 401) or reason in (
            "ExpiredProviderToken",
            "InvalidProviderToken",
        ):
            # Our token, not the device's: force a fresh one on the next attempt.
            self._token = None
            raise TransportError(f"apns auth rejected: {reason}", retryable=True)
        if response.status_code == 429 or response.status_code >= 500:
            raise TransportError(f"apns unavailable ({response.status_code}): {reason}")

        raise TransportError(f"apns rejected the message: {reason}", retryable=False)

    def _provider_token(self) -> str:
        now = time.time()
        if self._token and now - self._token_minted_at < TOKEN_TTL_SECONDS:
            return self._token
        self._token = jwt.encode(
            {"iss": self._credentials.team_id, "iat": int(now)},
            self._credentials.private_key,
            algorithm="ES256",
            headers={"kid": self._credentials.key_id},
        )
        self._token_minted_at = now
        return self._token

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def build_message(payload: PushPayload) -> dict[str, Any]:
    """APNs payload. Custom keys sit beside `aps`, never inside it."""
    data = payload.to_dict()
    return {
        "aps": {
            "alert": {"title": payload.title, "body": payload.body},
            "sound": "alarm.caf",
            "interruption-level": "critical",
            "mutable-content": 1,  # lets the app attach the snapshot before showing it
            "thread-id": f"site-{payload.site_name}",
        },
        **data,
        "deep_link": payload.deep_link,
    }


def _reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("reason", response.status_code))
    except ValueError:
        return response.text[:200] or str(response.status_code)


__all__ = ["ApnsCredentials", "ApnsTransport", "build_message"]
