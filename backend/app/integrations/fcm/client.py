"""Firebase Cloud Messaging HTTP v1 transport (spec §11.1).

HTTP v1 rather than the legacy server-key API: the legacy endpoint is retired, and v1
is the only one that reports per-token errors precisely enough to prune dead tokens.

Access tokens are minted from the service account directly (a signed JWT exchanged at
Google's token endpoint) rather than pulling in `google-auth`, which would add a large
dependency for the one call we make. The token is cached until shortly before it
expires; minting one per push would double the latency of every alarm.

The alarm itself travels in ``data`` only, with ``notification`` filled in for the
lock screen. Android delivers data-only messages to the app even when it is killed,
which is what makes the in-app siren possible.
"""

from __future__ import annotations

import json
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

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
#: Google issues one-hour tokens; refresh early so a push never races the expiry.
TOKEN_REFRESH_MARGIN_SECONDS = 300

#: FCM's own names for "this token is dead" (v1 error codes and HTTP status).
_DEAD_TOKEN_CODES = frozenset({"UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND"})


@dataclass(frozen=True, slots=True)
class ServiceAccount:
    project_id: str
    client_email: str
    private_key: str

    @classmethod
    def from_file(cls, path: str | Path) -> ServiceAccount:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServiceAccount:
        try:
            return cls(
                project_id=data["project_id"],
                client_email=data["client_email"],
                private_key=data["private_key"],
            )
        except KeyError as exc:  # a truncated secret is a deployment error, not a push error
            raise ValueError(f"service account is missing {exc.args[0]!r}") from exc


class FcmTransport:
    """Sends one notification per call; the dispatcher handles fan-out and retries."""

    platform = Platform.ANDROID

    def __init__(
        self,
        account: ServiceAccount,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 8.0,
    ) -> None:
        self._account = account
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def endpoint(self) -> str:
        return f"https://fcm.googleapis.com/v1/projects/{self._account.project_id}/messages:send"

    async def send(self, token: str, payload: PushPayload) -> str | None:
        access_token = await self._access_token()
        body = {"message": build_message(token, payload)}

        try:
            response = await self._client.post(
                self.endpoint,
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"fcm request failed: {exc}", retryable=True) from exc

        if response.status_code == 200:
            return response.json().get("name")

        detail = _error_code(response)

        if response.status_code in (401, 403):
            # Our credentials, not the token: drop the cached one and let the retry mint
            # a fresh one rather than failing every push for the next hour.
            self._token = None
            raise TransportError(f"fcm auth rejected: {detail}", retryable=True)
        if response.status_code == 404 or detail in _DEAD_TOKEN_CODES:
            raise TokenUnregistered(f"fcm: {detail}")
        if response.status_code == 429 or response.status_code >= 500:
            raise TransportError(f"fcm unavailable ({response.status_code}): {detail}")

        # 400 with anything else means we built a bad message; retrying will not help.
        raise TransportError(f"fcm rejected the message: {detail}", retryable=False)

    async def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token

        assertion = jwt.encode(
            {
                "iss": self._account.client_email,
                "scope": SCOPE,
                "aud": TOKEN_URL,
                "iat": int(now),
                "exp": int(now) + 3600,
            },
            self._account.private_key,
            algorithm="RS256",
        )
        try:
            response = await self._client.post(
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"fcm token request failed: {exc}") from exc

        if response.status_code != 200:
            raise TransportError(
                f"fcm token endpoint returned {response.status_code}",
                # A clock skew or a revoked key gives 400 here; retrying a revoked key
                # is pointless, but we cannot tell the two apart, so retry cheaply.
                retryable=response.status_code >= 500 or response.status_code == 400,
            )

        data = response.json()
        self._token = data["access_token"]
        self._token_expires_at = now + float(data.get("expires_in", 3600))
        return self._token

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def build_message(token: str, payload: PushPayload) -> dict[str, Any]:
    """FCM v1 message body. Data values must be strings — FCM rejects anything else."""
    data = {key: _as_string(value) for key, value in payload.to_dict().items()}
    data["deep_link"] = payload.deep_link

    return {
        "token": token,
        "data": data,
        "notification": {"title": payload.title, "body": payload.body},
        "android": {
            # A security alarm is the reason the app exists: it bypasses batching.
            "priority": "high",
            "notification": {
                "channel_id": "nightshift_alarms",
                "sound": "alarm",
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
                "notification_priority": "PRIORITY_MAX",
            },
        },
    }


def _as_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _error_code(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return response.text[:200]
    details = error.get("details") or []
    for detail in details:
        code = detail.get("errorCode")
        if code:
            return str(code)
    return str(error.get("status") or error.get("message") or response.status_code)


__all__ = ["FcmTransport", "ServiceAccount", "build_message"]
