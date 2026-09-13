"""Authenticated SMTP endpoint for DVR alarm mail (spec §7.1, §7.5).

The recorders are SMTP clients, so we are their SMTP server. That is what makes the
device identity trustworthy: it comes from `AUTH`, not from anything in the message.

Ports (spec §7.5): 587 STARTTLS primary, 465 implicit TLS, 2525 fallback for networks
that block both. Port 25 is never used — mobile operators block it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aiosmtpd.smtp import SMTP, AuthResult, Envelope, LoginPassword, Session

from app.modules.ingest.pipeline import DeviceIdentity, IngestResult, ingest

log = logging.getLogger(__name__)

#: Recorders send one small JPEG per alarm; anything larger is not ours.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
#: Per-device ceiling. A recorder in an alarm storm must not drown the pipeline.
DEFAULT_RATE_LIMIT_PER_MINUTE = 60


class AccountStore(Protocol):
    """Looks up a device by its SMTP username and verifies the password.

    **Synchronous by necessity.** aiosmtpd calls the authenticator inside the event
    loop without awaiting it, so a coroutine here is silently treated as a successful
    login with no identity attached. The database-backed implementation therefore
    serves from an in-memory snapshot (:class:`CachedAccountStore`) refreshed by a
    background task, rather than querying per connection.
    """

    def authenticate(self, username: str, password: str) -> DeviceIdentity | None: ...


ResultHandler = Callable[[IngestResult], Awaitable[None]]


@dataclass(slots=True)
class RateLimiter:
    """Fixed-window counter per device. Rejections are logged, never silent."""

    limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    _windows: dict[str, tuple[datetime, int]] = field(default_factory=dict)

    def allow(self, key: str, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        start, count = self._windows.get(key, (now, 0))
        if now - start >= timedelta(minutes=1):
            self._windows[key] = (now, 1)
            return True
        if count >= self.limit_per_minute:
            self._windows[key] = (start, count + 1)
            return False
        self._windows[key] = (start, count + 1)
        return True


class AlarmMailHandler:
    """aiosmtpd handler: authenticate, accept, hand the message to the pipeline."""

    def __init__(
        self,
        accounts: AccountStore,
        on_result: ResultHandler,
        *,
        rate_limiter: RateLimiter | None = None,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ) -> None:
        self._accounts = accounts
        self._on_result = on_result
        self._rate_limiter = rate_limiter or RateLimiter()
        self._max_message_bytes = max_message_bytes

    # -- authentication -----------------------------------------------------
    def authenticator(
        self, server: SMTP, session: Session, envelope: Envelope, mechanism: str, auth_data
    ) -> AuthResult:
        """Called by aiosmtpd for every supported mechanism (LOGIN, PLAIN).

        Must stay synchronous: aiosmtpd does not await this, and a returned coroutine
        is not an ``AuthResult``, which sends the connection down the legacy path and
        authenticates it with no device attached.
        """
        if not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False, handled=False)
        username = _as_text(auth_data.login)
        password = _as_text(auth_data.password)
        identity = self._accounts.authenticate(username, password)
        if identity is None:
            log.warning("smtp auth rejected for user %r (%s)", username, mechanism)
            return AuthResult(success=False, handled=False)
        log.info("smtp auth accepted for device %s", identity.device_id)
        # aiosmtpd stores this on the session; the rest of the flow reads the device
        # from there and never from the message.
        return AuthResult(success=True, auth_data=identity)

    # -- message flow -------------------------------------------------------
    async def handle_MAIL(
        self, server: SMTP, session: Session, envelope: Envelope, address: str, options
    ) -> str:
        if not isinstance(getattr(session, "auth_data", None), DeviceIdentity):
            return "530 5.7.0 Authentication required"
        envelope.mail_from = address
        envelope.mail_options.extend(options)
        return "250 OK"

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        identity: DeviceIdentity | None = getattr(session, "auth_data", None)
        if not isinstance(identity, DeviceIdentity):
            return "530 5.7.0 Authentication required"

        raw = envelope.original_content or envelope.content or b""
        if isinstance(raw, str):  # pragma: no cover - aiosmtpd decodes in 7bit mode
            raw = raw.encode("utf-8", "replace")

        if len(raw) > self._max_message_bytes:
            log.warning(
                "rejected oversized message from device %s (%d bytes)",
                identity.device_id,
                len(raw),
            )
            return "552 5.3.4 Message too large"

        if not self._rate_limiter.allow(str(identity.device_id)):
            # 451 asks the recorder to retry later rather than discarding the alarm.
            log.warning("rate limit hit for device %s", identity.device_id)
            return "451 4.7.1 Rate limit exceeded, retry later"

        try:
            result = ingest(raw, identity)
        except Exception:
            log.exception("ingest failed for device %s", identity.device_id)
            return "451 4.3.0 Temporary processing failure"

        try:
            await self._on_result(result)
        except Exception:
            log.exception("ingest sink failed for device %s", identity.device_id)
            return "451 4.3.0 Temporary storage failure"

        log.info(
            "accepted alarm mail device=%s status=%s type=%s channel=%s images=%d",
            identity.device_id,
            result.status.value,
            result.event.event_type if result.event else "-",
            result.event.channel_number if result.event else "-",
            len(result.media),
        )
        return "250 Message accepted for delivery"


class CachedAccountStore:
    """Snapshot of `device_smtp_accounts`, verified without touching the database.

    Accounts change only at commissioning or rotation, so a periodically refreshed
    snapshot is both correct enough and the only shape the SMTP layer can use.
    """

    def __init__(self, verifier: Callable[[str, str], bool] | None = None) -> None:
        self._accounts: dict[str, tuple[DeviceIdentity, str]] = {}
        self._verify = verifier or _constant_time_equals

    def replace_all(self, accounts: dict[str, tuple[DeviceIdentity, str]]) -> None:
        """Atomically swap the snapshot (called by the refresh task)."""
        self._accounts = dict(accounts)

    def authenticate(self, username: str, password: str) -> DeviceIdentity | None:
        entry = self._accounts.get(username)
        if entry is None:
            # Still spend the verification cost so a missing user and a wrong password
            # are not distinguishable by timing.
            self._verify(password, "$unused$")
            return None
        identity, secret = entry
        return identity if self._verify(password, secret) else None

    def __len__(self) -> int:
        return len(self._accounts)


def _constant_time_equals(password: str, secret: str) -> bool:
    import hmac

    return hmac.compare_digest(password, secret)


def _as_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


# TODO(V1-BLOCKER): PHASE 2 deployment — run this behind TLS on 587/465/2525 with a
# real certificate, feed CachedAccountStore from `device_smtp_accounts` on a refresh
# task, swap the verifier for Argon2, and push accepted results onto RabbitMQ instead
# of calling the sink inline.
