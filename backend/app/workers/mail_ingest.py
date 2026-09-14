"""The alarm mail server as a running process (spec §7).

`python -m app.workers.mail_ingest` starts the MTA the recorders send to. It is a
separate process from the API on purpose: a burst of alarm mail must not slow down a
guard acknowledging an alarm, and the two scale differently.

The credential snapshot deserves explaining. aiosmtpd calls the authenticator
synchronously, so it cannot await a query; a background task therefore reloads the
active accounts every minute into memory and the authenticator verifies against that.
The consequences are stated rather than hidden:

* A newly issued account works within one refresh interval, not instantly.
* A *revoked* account also keeps working for up to one interval, which is why
  revocation is a rotation (the recorder's old password stops matching the row) and
  not something a burglar can exploit — the credential only grants the right to send
  us mail from a device we already know.

TLS is required by default. Alarm mail carries a site name, a camera name and a
snapshot; it does not cross the public internet in the clear.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import ssl

from aiosmtpd.controller import Controller
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.security import verify_password
from app.db import models
from app.db.session import SessionFactory
from app.modules.ingest.pipeline import DeviceIdentity
from app.modules.ingest.smtp_server import AlarmMailHandler, CachedAccountStore
from app.services.intake import MailIntake

log = logging.getLogger(__name__)


async def load_accounts(
    session: AsyncSession,
) -> dict[str, tuple[DeviceIdentity, str]]:
    """Every active SMTP account with the device it speaks for.

    The stored value is an Argon2 hash, so the snapshot's "secret" is that hash and
    the verifier is the password check — the plaintext is never in memory.
    """
    rows = (
        await session.execute(
            select(models.DeviceSmtpAccount, models.Device, models.FirmwareEmailProfile)
            .join(models.Device, models.Device.id == models.DeviceSmtpAccount.device_id)
            .outerjoin(
                models.FirmwareEmailProfile,
                models.FirmwareEmailProfile.id == models.Device.email_profile_id,
            )
            .where(models.DeviceSmtpAccount.is_active.is_(True))
        )
    ).all()

    accounts: dict[str, tuple[DeviceIdentity, str]] = {}
    for account, device, profile in rows:
        identity = DeviceIdentity(
            device_id=device.id,
            tenant_id=device.tenant_id,
            site_id=device.site_id,
            smtp_username=account.username,
            # Keep the raw message until a profile row says the parsing is proven.
            capture_samples=profile is None or not profile.verified,
        )
        accounts[account.username] = (identity, account.password_hash)
    return accounts


class AccountRefresher:
    """Keeps the in-memory credential snapshot close enough to the database."""

    def __init__(
        self,
        store: CachedAccountStore,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        interval_seconds: int = 60,
    ) -> None:
        self._store = store
        self._sessions = session_factory
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    async def refresh_once(self) -> int:
        async with self._sessions() as session:
            accounts = await load_accounts(session)
        self._store.replace_all(accounts)
        return len(accounts)

    async def run(self) -> None:
        while True:
            try:
                count = await self.refresh_once()
                log.debug("credential snapshot refreshed: %d active accounts", count)
            except Exception:
                # Keep serving with the previous snapshot: a database blip must not
                # start rejecting every recorder on every site.
                log.exception("credential refresh failed; keeping the previous snapshot")
            await asyncio.sleep(self._interval)

    async def start(self) -> None:
        count = await self.refresh_once()  # fail loudly at boot, not silently at 03:00
        log.info("loaded %d active device accounts", count)
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


def build_tls_context(settings: Settings) -> ssl.SSLContext | None:
    if not settings.smtp_tls_cert_file or not settings.smtp_tls_key_file:
        if settings.is_production and settings.smtp_require_tls:
            raise RuntimeError(
                "SMTP_TLS_CERT_FILE and SMTP_TLS_KEY_FILE are required in production"
            )
        log.warning("starting the mail server without TLS; development only")
        return None
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(settings.smtp_tls_cert_file, settings.smtp_tls_key_file)
    return context


async def serve(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    store = CachedAccountStore(verifier=verify_password)
    refresher = AccountRefresher(
        store, SessionFactory, interval_seconds=settings.smtp_account_refresh_seconds
    )
    await refresher.start()

    intake = MailIntake(
        SessionFactory,
        media_store=_media_store(settings),
        notifier=_notifier(settings),
    )
    handler = AlarmMailHandler(store, intake.handle)
    tls = build_tls_context(settings)

    controller = Controller(
        handler,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        authenticator=handler.authenticator,
        auth_required=True,
        # Credentials in the clear are only tolerable in development.
        auth_require_tls=tls is not None,
        tls_context=tls,
    )
    controller.start()
    log.info("alarm mail server listening on %s:%d", settings.smtp_host, settings.smtp_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        log.info("shutting down the alarm mail server")
        controller.stop()
        await refresher.stop()


def _notifier(settings: Settings):
    """Build the push notifier, or run without one.

    A missing FCM/APNs credential is a deployment gap, not a reason to stop accepting
    alarm mail: the events and alarms still land and the app still shows them live.
    """
    from app.modules.notifications.transport import PushDispatcher
    from app.services.notifier import PushNotifier

    transports = []
    if settings.fcm_service_account_file:
        from app.integrations.fcm.client import FcmTransport, ServiceAccount

        transports.append(
            FcmTransport(ServiceAccount.from_file(settings.fcm_service_account_file))
        )
    if settings.apns_key_file and settings.apns_key_id and settings.apns_team_id:
        from app.integrations.apns.client import ApnsCredentials, ApnsTransport

        transports.append(
            ApnsTransport(
                ApnsCredentials.from_p8_file(
                    settings.apns_key_file,
                    key_id=settings.apns_key_id,
                    team_id=settings.apns_team_id,
                    topic=settings.apns_topic,
                    use_sandbox=settings.apns_use_sandbox,
                )
            )
        )

    if not transports:
        log.warning("no push transport is configured; alarms will not reach phones")
        return None
    log.info("push transports ready: %s", ", ".join(t.platform.value for t in transports))
    return PushNotifier(SessionFactory, PushDispatcher(transports))


def _media_store(settings: Settings):
    from app.integrations.object_storage.media_store import MediaStore, build_client

    try:
        return MediaStore(
            build_client(
                endpoint=settings.s3_endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                region=settings.s3_region,
                path_style=settings.s3_use_path_style,
            ),
            settings.s3_bucket,
            presign_ttl_seconds=settings.s3_presign_ttl_seconds,
        )
    except Exception:
        # Alarms without pictures beat no alarms: run on and log the misconfiguration.
        log.exception("object storage is unavailable; snapshots will not be stored")
        return None


def main() -> None:  # pragma: no cover - process entrypoint
    asyncio.run(serve())


if __name__ == "__main__":  # pragma: no cover
    main()
