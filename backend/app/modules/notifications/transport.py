"""Push transport interface, retry policy and delivery bookkeeping (spec §11.2).

One interface, two implementations (FCM and APNs), so the dispatcher never branches on
platform beyond picking a transport. The rules encoded here are the ones that decide
whether a guard's phone actually buzzes at 03:00:

* **Failures are classified, not just counted.** A 503 is retried; a 400 is not. An
  unregistered token is not an error at all — it is a phone that was reinstalled, and
  the right response is to delete the row, not to retry it forever.
* **Retries are bounded and jittered.** Every recorder on a site can alarm at once; a
  fixed backoff would make every worker retry in lockstep.
* **Every attempt is recorded.** "The guard says he got no notification" has to be
  answerable from the database, which is what `alarm_deliveries` is for.
* **The payload is built once**, by `payload.build_push`, which already refuses to
  send an RTSP URL or a password. Transports serialise it; they never compose it.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.modules.notifications.payload import PushPayload

log = logging.getLogger(__name__)

#: Attempts per token, including the first. Four attempts over ~7s of backoff: past
#: that the alarm is stale and escalation (spec §12) is the better answer than retrying.
MAX_ATTEMPTS = 4
#: Base seconds for exponential backoff: 0.5, 1, 2 (each with jitter).
BACKOFF_BASE_SECONDS = 0.5
#: A single send must not hold the escalation loop.
SEND_TIMEOUT_SECONDS = 10.0


class Platform(StrEnum):
    IOS = "ios"
    ANDROID = "android"


class DeliveryStatus(StrEnum):
    SENT = "SENT"
    FAILED = "FAILED"
    #: The token is gone (app uninstalled, token rotated). Prune it, do not retry.
    UNREGISTERED = "UNREGISTERED"
    #: No transport is configured for this platform; a deployment problem, logged loudly.
    UNCONFIGURED = "UNCONFIGURED"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: DeliveryStatus
    detail: str | None = None
    attempts: int = 1
    #: Provider-side id, when one is returned; useful when chasing a lost push.
    provider_id: str | None = None

    @property
    def sent(self) -> bool:
        return self.status is DeliveryStatus.SENT


class TransportError(RuntimeError):
    """Raised by a transport. ``retryable`` decides whether we try again."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class TokenUnregistered(TransportError):
    """The provider says this token no longer exists."""

    def __init__(self, message: str = "token unregistered") -> None:
        super().__init__(message, retryable=False)


class PushTransport(Protocol):
    """What FCM and APNs both look like from the dispatcher's side."""

    platform: Platform

    async def send(self, token: str, payload: PushPayload) -> str | None:
        """Deliver one notification, or raise :class:`TransportError`.

        Returns a provider message id when the provider gives one.
        """
        ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class Recipient:
    user_id: UUID
    token: str
    platform: Platform
    device_id: UUID


@dataclass(slots=True)
class DispatchReport:
    """The outcome of one alarm's fan-out, for logging and for the escalation loop."""

    alarm_id: UUID
    sent: int = 0
    failed: int = 0
    pruned: int = 0
    results: list[tuple[Recipient, DeliveryResult]] = field(default_factory=list)

    @property
    def reached_anyone(self) -> bool:
        return self.sent > 0


async def send_with_retry(
    transport: PushTransport,
    token: str,
    payload: PushPayload,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    sleeper=asyncio.sleep,
) -> DeliveryResult:
    """Send one notification, retrying only what is worth retrying."""
    last_detail: str | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                provider_id = await transport.send(token, payload)
        except TokenUnregistered as exc:
            return DeliveryResult(
                status=DeliveryStatus.UNREGISTERED, detail=str(exc), attempts=attempt
            )
        except TransportError as exc:
            last_detail = str(exc)
            if not exc.retryable or attempt == max_attempts:
                return DeliveryResult(
                    status=DeliveryStatus.FAILED, detail=last_detail, attempts=attempt
                )
        except TimeoutError:
            last_detail = f"timed out after {SEND_TIMEOUT_SECONDS:g}s"
            if attempt == max_attempts:
                return DeliveryResult(
                    status=DeliveryStatus.FAILED, detail=last_detail, attempts=attempt
                )
        except Exception as exc:
            log.exception("push transport raised an unexpected error")
            return DeliveryResult(
                status=DeliveryStatus.FAILED, detail=repr(exc), attempts=attempt
            )
        else:
            return DeliveryResult(
                status=DeliveryStatus.SENT, attempts=attempt, provider_id=provider_id
            )

        # Jittered so a site-wide alarm storm does not retry in lockstep.
        delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        await sleeper(delay * (0.5 + random.random()))  # noqa: S311 - jitter, not crypto

    return DeliveryResult(  # pragma: no cover - the loop always returns first
        status=DeliveryStatus.FAILED, detail=last_detail, attempts=max_attempts
    )


class PushDispatcher:
    """Fans one alarm out to every phone that should hear about it."""

    def __init__(self, transports: Sequence[PushTransport]) -> None:
        self._by_platform: dict[Platform, PushTransport] = {
            transport.platform: transport for transport in transports
        }

    @property
    def platforms(self) -> frozenset[Platform]:
        return frozenset(self._by_platform)

    async def dispatch(
        self,
        session: AsyncSession,
        *,
        alarm_id: UUID,
        tenant_id: UUID,
        payload: PushPayload,
        recipients: Sequence[Recipient],
        escalation_step: int = 0,
    ) -> DispatchReport:
        report = DispatchReport(alarm_id=alarm_id)
        if not recipients:
            log.warning("alarm %s has no push recipients", alarm_id)
            return report

        # Concurrent: ten guards must not wait behind each other's retries.
        results = await asyncio.gather(
            *(self._send_one(recipient, payload) for recipient in recipients)
        )

        for recipient, result in zip(recipients, results, strict=True):
            report.results.append((recipient, result))
            session.add(
                models.AlarmDelivery(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    alarm_id=alarm_id,
                    user_id=recipient.user_id,
                    channel="apns" if recipient.platform is Platform.IOS else "fcm",
                    escalation_step=escalation_step,
                    status=result.status.value,
                    sent_at=datetime.now(UTC) if result.sent else None,
                    error=result.detail[:1000] if result.detail else None,
                )
            )
            if result.sent:
                report.sent += 1
            elif result.status is DeliveryStatus.UNREGISTERED:
                # The phone is gone. Keeping the row would retry it on every alarm.
                await session.delete(await session.get(models.PushDevice, recipient.device_id))
                report.pruned += 1
            else:
                report.failed += 1

        await session.flush()
        log.info(
            "alarm %s push fan-out: sent=%d failed=%d pruned=%d",
            alarm_id,
            report.sent,
            report.failed,
            report.pruned,
        )
        return report

    async def _send_one(self, recipient: Recipient, payload: PushPayload) -> DeliveryResult:
        transport = self._by_platform.get(recipient.platform)
        if transport is None:
            log.error("no transport configured for platform %s", recipient.platform)
            return DeliveryResult(
                status=DeliveryStatus.UNCONFIGURED,
                detail=f"no transport for {recipient.platform.value}",
                attempts=0,
            )
        return await send_with_retry(transport, recipient.token, payload)

    async def aclose(self) -> None:
        for transport in self._by_platform.values():
            await transport.aclose()


async def recipients_for_site(
    session: AsyncSession, *, tenant_id: UUID, site_id: UUID
) -> list[Recipient]:
    """Every registered phone in the tenant that may see this site's alarms.

    Site-level targeting is a tenant-wide broadcast for now: V1 customers run one
    site per tenant, and a missed alarm costs more than an extra notification. When
    per-site membership lands (spec §16), this is the only query that changes.
    """
    from app.core.rbac import Permission, permissions_for

    rows = (
        (
            await session.execute(
                select(models.PushDevice, models.User)
                .join(models.User, models.User.id == models.PushDevice.user_id)
                .where(
                    models.PushDevice.tenant_id == tenant_id,
                    models.User.is_active.is_(True),
                )
            )
        )
        .all()
    )

    recipients: list[Recipient] = []
    for device, user in rows:
        if Permission.ALARMS_VIEW not in permissions_for(user.role):
            # A billing-only account does not get woken up by a perimeter alarm.
            continue
        try:
            platform = Platform(device.platform.lower())
        except ValueError:
            log.warning("push device %s has unknown platform %r", device.id, device.platform)
            continue
        recipients.append(
            Recipient(
                user_id=user.id, token=device.token, platform=platform, device_id=device.id
            )
        )
    return recipients


__all__ = [
    "DeliveryResult",
    "DeliveryStatus",
    "DispatchReport",
    "Platform",
    "PushDispatcher",
    "PushTransport",
    "Recipient",
    "TokenUnregistered",
    "TransportError",
    "recipients_for_site",
    "send_with_retry",
]
