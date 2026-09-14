"""The whole chain, with nothing faked but the recorder.

A real SMTP server on a loopback port, credentials loaded from a real database the way
the worker loads them, a message sent with smtplib, and rows to check afterwards. If
this passes, the only thing standing between the product and a live site is the body
format of the recorder's mail.
"""

from __future__ import annotations

import asyncio
import contextlib
import smtplib
import socket
from uuid import uuid4

from aiosmtpd.controller import Controller

from app.core.security import hash_password, verify_password
from app.db import models
from app.modules.ingest.smtp_server import AlarmMailHandler, CachedAccountStore
from app.services.intake import MailIntake
from app.workers.mail_ingest import AccountRefresher, load_accounts
from tests.conftest import requires_postgres
from tests.test_ingest import build_mail
from tests.test_intake import HUMAN_MAIL, count, seed_site

pytestmark = requires_postgres

SMTP_PASSWORD = "device-secret-42"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def add_account(session_factory, seed: dict, *, username: str, active: bool = True) -> None:
    async def _add():
        async with session_factory() as session:
            session.add(
                models.DeviceSmtpAccount(
                    id=uuid4(),
                    tenant_id=seed["tenant_id"],
                    device_id=seed["device_id"],
                    username=username,
                    password_hash=hash_password(SMTP_PASSWORD),
                    recipient="alarm@nightshift.local",
                    is_active=active,
                )
            )
            await session.commit()

    asyncio.run(_add())


def send(port: int, *, username: str, password: str = SMTP_PASSWORD, body: str = HUMAN_MAIL) -> str:
    """Deliver one message the way a recorder does. Returns the server's reply."""
    with smtplib.SMTP("127.0.0.1", port, timeout=10) as client:
        client.ehlo("xvr.local")
        client.login(username, password)
        code, reply = client.docmd(
            "MAIL", "FROM:<xvr@site-a.local>"
        )
        assert code == 250, reply
        client.docmd("RCPT", "TO:<alarm@nightshift.local>")
        code, reply = client.docmd("DATA")
        raw = build_mail(body, message_id=f"<{uuid4().hex}@xvr.local>")
        client.send(raw + b"\r\n.\r\n")
        code, reply = client.getreply()
        return f"{code} {reply.decode()}"


class TestWorkerWiring:
    def test_the_snapshot_only_carries_active_accounts(self, session_factory, clean_db):
        """A rotated credential must stop working, not linger in memory."""
        seed = seed_site(session_factory)
        add_account(session_factory, seed, username="dev-live")
        add_account(session_factory, seed, username="dev-rotated", active=False)

        async def snapshot():
            async with session_factory() as session:
                return await load_accounts(session)

        accounts = asyncio.run(snapshot())
        assert set(accounts) == {"dev-live"}
        identity, secret = accounts["dev-live"]
        assert identity.device_id == seed["device_id"]
        assert verify_password(SMTP_PASSWORD, secret)  # the hash, never the password

    def test_a_device_with_no_verified_profile_keeps_raw_samples(
        self, session_factory, clean_db
    ):
        """Until the real mail format is proven, every message is kept for analysis."""
        seed = seed_site(session_factory)
        add_account(session_factory, seed, username="dev-unproven")

        async def snapshot():
            async with session_factory() as session:
                return await load_accounts(session)

        identity, _ = asyncio.run(snapshot())["dev-unproven"]
        assert identity.capture_samples is True

    def test_the_refresher_survives_a_database_failure(self, session_factory, clean_db):
        """A blip must not start rejecting every recorder on every site."""
        seed = seed_site(session_factory)
        add_account(session_factory, seed, username="dev-live")

        store = CachedAccountStore(verifier=verify_password)
        refresher = AccountRefresher(store, session_factory)
        assert asyncio.run(refresher.refresh_once()) == 1

        class Broken:
            def __call__(self):
                raise RuntimeError("database is down")

        broken = AccountRefresher(store, Broken())
        with contextlib.suppress(RuntimeError):
            asyncio.run(broken.refresh_once())
        # The previous snapshot is still serving.
        assert store.authenticate("dev-live", SMTP_PASSWORD) is not None


class TestEndToEnd:
    def test_a_recorders_mail_becomes_an_alarm_row(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        add_account(session_factory, seed, username="dev-santiye-a")

        store = CachedAccountStore(verifier=verify_password)
        refresher = AccountRefresher(store, session_factory)
        assert asyncio.run(refresher.refresh_once()) == 1

        intake = MailIntake(session_factory)
        handler = AlarmMailHandler(store, intake.handle)
        controller = Controller(
            handler,
            hostname="127.0.0.1",
            port=_free_port(),
            authenticator=handler.authenticator,
            auth_require_tls=False,
        )
        controller.start()
        try:
            reply = send(controller.port, username="dev-santiye-a")
        finally:
            controller.stop()

        assert reply.startswith("250")
        assert count(session_factory, models.EmailMessage) == 1
        assert count(session_factory, models.Event) == 1

    def test_a_wrong_password_delivers_nothing(self, session_factory, clean_db):
        """The device identity comes from AUTH, so a failed AUTH ends the story."""
        seed = seed_site(session_factory)
        add_account(session_factory, seed, username="dev-santiye-a")

        store = CachedAccountStore(verifier=verify_password)
        asyncio.run(AccountRefresher(store, session_factory).refresh_once())

        intake = MailIntake(session_factory)
        handler = AlarmMailHandler(store, intake.handle)
        controller = Controller(
            handler,
            hostname="127.0.0.1",
            port=_free_port(),
            authenticator=handler.authenticator,
            auth_require_tls=False,
        )
        controller.start()
        try:
            with smtplib.SMTP("127.0.0.1", controller.port, timeout=10) as client:
                client.ehlo("xvr.local")
                try:
                    client.login("dev-santiye-a", "wrong-password")
                    logged_in = True
                except smtplib.SMTPAuthenticationError:
                    logged_in = False
        finally:
            controller.stop()

        assert logged_in is False
        assert count(session_factory, models.EmailMessage) == 0
        assert count(session_factory, models.Event) == 0
