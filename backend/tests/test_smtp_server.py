"""SMTP endpoint behaviour: authentication, limits, and refusal semantics.

These run a real aiosmtpd server on a loopback port and talk to it with smtplib, so
they exercise the actual protocol path a recorder will take — including the part that
matters most: an unauthenticated sender gets nowhere.
"""

from __future__ import annotations

import smtplib
import socket
from datetime import UTC
from uuid import uuid4

import pytest
from aiosmtpd.controller import Controller

from app.modules.ingest.pipeline import DeviceIdentity, IngestResult, ParseStatus
from app.modules.ingest.smtp_server import AlarmMailHandler, RateLimiter
from tests.test_ingest import DAHUA_BODY, build_mail

DEVICE = DeviceIdentity(
    device_id=uuid4(),
    tenant_id=uuid4(),
    site_id=uuid4(),
    smtp_username="dev-site-a",
)
PASSWORD = "correct-horse-battery-staple"


class FakeAccounts:
    """One valid account; everything else is rejected."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def authenticate(self, username: str, password: str) -> DeviceIdentity | None:
        self.attempts.append(username)
        if username == DEVICE.smtp_username and password == PASSWORD:
            return DEVICE
        return None


def _free_port() -> int:
    """aiosmtpd does not report back an OS-assigned port, so reserve one first."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def server():
    received: list[IngestResult] = []

    async def sink(result: IngestResult) -> None:
        received.append(result)

    accounts = FakeAccounts()
    handler = AlarmMailHandler(accounts, sink, rate_limiter=RateLimiter(limit_per_minute=3))
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=_free_port(),
        authenticator=handler.authenticator,
        auth_require_tls=False,
    )
    controller.start()
    try:
        yield controller, received, accounts
    finally:
        controller.stop()


def send(controller, *, user=DEVICE.smtp_username, password=PASSWORD, raw=None, login=True):
    raw = raw if raw is not None else build_mail()
    with smtplib.SMTP(controller.hostname, controller.port, timeout=10) as client:
        client.ehlo("recorder.local")
        if login:
            client.login(user, password)
        client.sendmail("xvr@site.local", ["alarm@in.nightshift.app"], raw)


class TestAuthentication:
    def test_valid_credentials_deliver_an_event(self, server):
        controller, received, _ = server
        send(controller)
        assert len(received) == 1
        assert received[0].event.event_type == "person_detected"
        assert received[0].event.device_id == DEVICE.device_id

    def test_wrong_password_is_rejected(self, server):
        controller, received, _ = server
        with pytest.raises(smtplib.SMTPAuthenticationError):
            send(controller, password="wrong")
        assert received == []

    def test_unknown_user_is_rejected(self, server):
        controller, received, _ = server
        with pytest.raises(smtplib.SMTPAuthenticationError):
            send(controller, user="dev-somebody-else")
        assert received == []

    def test_unauthenticated_sender_cannot_inject_an_alarm(self, server):
        """The whole identity model rests on this (spec §7.1)."""
        controller, received, _ = server
        with pytest.raises(smtplib.SMTPSenderRefused) as excinfo:
            send(controller, login=False)
        assert excinfo.value.smtp_code == 530
        assert received == []


class TestLimits:
    def test_oversized_message_is_refused(self, server):
        controller, received, _ = server
        handler_limit = 16 * 1024 * 1024
        huge = build_mail(attach=b"\xff\xd8\xff" + b"\x00" * (handler_limit + 1024))
        with pytest.raises(smtplib.SMTPDataError) as excinfo:
            send(controller, raw=huge)
        assert excinfo.value.smtp_code == 552
        assert received == []

    def test_rate_limit_asks_for_a_retry_rather_than_discarding(self, server):
        """451 keeps the alarm alive on the recorder instead of dropping it."""
        controller, received, _ = server
        for _ in range(3):
            send(controller)
        with pytest.raises(smtplib.SMTPDataError) as excinfo:
            send(controller)
        assert excinfo.value.smtp_code == 451
        assert len(received) == 3


class TestDelivery:
    def test_unparseable_body_is_still_accepted_and_delivered(self, server):
        controller, received, _ = server
        send(controller, raw=build_mail("firmware wording we have never seen"))
        assert len(received) == 1
        assert received[0].status is ParseStatus.UNPARSED
        assert received[0].event is not None

    def test_face_alarm_is_accepted_at_smtp_but_carries_nothing(self, server):
        """Refusing at SMTP would make the recorder retry forever; we accept and drop."""
        controller, received, _ = server
        face = build_mail("Alarm Event: Face Detection\nAlarm Input Channel No.: 1\n")
        send(controller, raw=face)
        assert len(received) == 1
        assert received[0].blocked is True
        assert received[0].event is None
        assert received[0].media == []

    def test_snapshot_survives_the_round_trip(self, server):
        controller, received, _ = server
        send(controller, raw=build_mail(DAHUA_BODY))
        assert received[0].media
        assert received[0].media[0].content.startswith(b"\xff\xd8\xff")


class TestRateLimiter:
    def test_window_resets(self):
        from datetime import datetime, timedelta

        limiter = RateLimiter(limit_per_minute=2)
        start = datetime(2026, 9, 12, 22, 0, 0, tzinfo=UTC)
        assert limiter.allow("d", now=start)
        assert limiter.allow("d", now=start)
        assert not limiter.allow("d", now=start + timedelta(seconds=30))
        assert limiter.allow("d", now=start + timedelta(seconds=61))

    def test_devices_are_limited_independently(self):
        limiter = RateLimiter(limit_per_minute=1)
        assert limiter.allow("device-a")
        assert not limiter.allow("device-a")
        assert limiter.allow("device-b")
