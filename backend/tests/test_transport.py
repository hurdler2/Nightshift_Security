"""Push delivery: what gets retried, what gets pruned, what never gets sent.

The provider APIs are mocked, but everything on our side of the wire is real — the
retry classification, the payload shape and the delivery rows. A push that silently
fails is the one failure mode a security product cannot have, so these tests are
mostly about failure, not success.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.integrations.apns.client import ApnsCredentials, ApnsTransport
from app.integrations.apns.client import build_message as build_apns
from app.integrations.fcm.client import FcmTransport, ServiceAccount
from app.integrations.fcm.client import build_message as build_fcm
from app.modules.notifications.payload import build_push
from app.modules.notifications.transport import (
    DeliveryStatus,
    Platform,
    PushDispatcher,
    Recipient,
    TokenUnregistered,
    TransportError,
    send_with_retry,
)
from app.modules.rules.risk import Severity

PAYLOAD = build_push(
    event_id=uuid4(),
    alarm_id=uuid4(),
    severity=Severity.HIGH,
    site_name="Beton Tesisi",
    camera_name="Depo Arka",
    event_type="person_detected",
    occurred_at=datetime(2026, 9, 15, 2, 14, tzinfo=UTC),
    risk_score=78,
    reasons=["Gece saati", "Yasak bölge"],
    has_snapshot=True,
)


class FakeTransport:
    """Records calls and replays a scripted sequence of outcomes."""

    def __init__(self, platform: Platform, outcomes: list) -> None:
        self.platform = platform
        self._outcomes = list(outcomes)
        self.calls: list[str] = []
        self.closed = False

    async def send(self, token: str, payload) -> str | None:
        self.calls.append(token)
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


async def no_sleep(_: float) -> None:
    """Retry delays are correctness, not something the tests should wait through."""


class TestRetryPolicy:
    @pytest.mark.asyncio
    async def test_a_successful_send_is_not_retried(self):
        transport = FakeTransport(Platform.ANDROID, ["projects/x/messages/1"])
        result = await send_with_retry(transport, "tok", PAYLOAD, sleeper=no_sleep)
        assert result.status is DeliveryStatus.SENT
        assert result.attempts == 1
        assert result.provider_id == "projects/x/messages/1"

    @pytest.mark.asyncio
    async def test_a_transient_failure_is_retried_then_succeeds(self):
        transport = FakeTransport(
            Platform.ANDROID, [TransportError("503"), TransportError("503"), "ok"]
        )
        result = await send_with_retry(transport, "tok", PAYLOAD, sleeper=no_sleep)
        assert result.status is DeliveryStatus.SENT
        assert result.attempts == 3

    @pytest.mark.asyncio
    async def test_a_permanent_failure_is_not_retried(self):
        """A malformed message will be malformed the second time too."""
        transport = FakeTransport(
            Platform.ANDROID, [TransportError("bad payload", retryable=False), "ok"]
        )
        result = await send_with_retry(transport, "tok", PAYLOAD, sleeper=no_sleep)
        assert result.status is DeliveryStatus.FAILED
        assert result.attempts == 1
        assert len(transport.calls) == 1

    @pytest.mark.asyncio
    async def test_an_unregistered_token_reports_itself_for_pruning(self):
        transport = FakeTransport(Platform.IOS, [TokenUnregistered()])
        result = await send_with_retry(transport, "tok", PAYLOAD, sleeper=no_sleep)
        assert result.status is DeliveryStatus.UNREGISTERED

    @pytest.mark.asyncio
    async def test_retries_are_bounded(self):
        transport = FakeTransport(Platform.ANDROID, [TransportError("503")] * 10)
        result = await send_with_retry(transport, "tok", PAYLOAD, max_attempts=3, sleeper=no_sleep)
        assert result.status is DeliveryStatus.FAILED
        assert len(transport.calls) == 3

    @pytest.mark.asyncio
    async def test_an_unexpected_exception_does_not_escape(self):
        """A bug in a transport must not take down the alarm path for everyone else."""
        transport = FakeTransport(Platform.ANDROID, [ValueError("boom")])
        result = await send_with_retry(transport, "tok", PAYLOAD, sleeper=no_sleep)
        assert result.status is DeliveryStatus.FAILED
        assert "boom" in result.detail


class TestDispatcher:
    @pytest.mark.asyncio
    async def test_a_platform_with_no_transport_is_reported_not_ignored(self):
        dispatcher = PushDispatcher([FakeTransport(Platform.ANDROID, ["ok"])])
        recipient = Recipient(
            user_id=uuid4(), token="tok", platform=Platform.IOS, device_id=uuid4()
        )
        result = await dispatcher._send_one(recipient, PAYLOAD)
        assert result.status is DeliveryStatus.UNCONFIGURED

    @pytest.mark.asyncio
    async def test_closing_closes_every_transport(self):
        android = FakeTransport(Platform.ANDROID, [])
        ios = FakeTransport(Platform.IOS, [])
        await PushDispatcher([android, ios]).aclose()
        assert android.closed and ios.closed


class TestPayloadShape:
    def test_fcm_data_values_are_all_strings(self):
        """FCM rejects the whole message if any data value is not a string."""
        message = build_fcm("tok", PAYLOAD)
        assert all(isinstance(v, str) for v in message["data"].values())
        assert message["android"]["priority"] == "high"
        assert message["data"]["deep_link"].startswith("nightshift://alarms/")

    def test_apns_custom_keys_sit_beside_aps(self):
        message = build_apns(PAYLOAD)
        assert "alert" in message["aps"]
        assert message["aps"]["interruption-level"] == "critical"
        assert message["event_id"] == str(PAYLOAD.event_id)

    def test_neither_transport_can_carry_a_stream_url(self):
        """`build_push` refuses unsafe fields; make sure serialising cannot add one."""
        for blob in (str(build_fcm("tok", PAYLOAD)), str(build_apns(PAYLOAD))):
            assert "rtsp://" not in blob
            assert "password" not in blob.lower()


ACCOUNT = ServiceAccount(
    project_id="nightshift",
    client_email="push@nightshift.iam.gserviceaccount.com",
    private_key="unused-in-these-tests",
)


def fcm_transport(handler) -> FcmTransport:
    transport = FcmTransport(ACCOUNT, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    transport._token = "cached-access-token"
    transport._token_expires_at = 9e9
    return transport


class TestFcmErrorMapping:
    @pytest.mark.asyncio
    async def test_unregistered_maps_to_pruning(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}},
            )

        with pytest.raises(TokenUnregistered):
            await fcm_transport(handler).send("tok", PAYLOAD)

    @pytest.mark.asyncio
    async def test_server_error_is_retryable(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})

        with pytest.raises(TransportError) as excinfo:
            await fcm_transport(handler).send("tok", PAYLOAD)
        assert excinfo.value.retryable is True

    @pytest.mark.asyncio
    async def test_a_bad_request_is_not_retryable(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"status": "INVALID_REQUEST"}})

        with pytest.raises(TransportError) as excinfo:
            await fcm_transport(handler).send("tok", PAYLOAD)
        assert excinfo.value.retryable is False

    @pytest.mark.asyncio
    async def test_auth_failure_drops_the_cached_token(self):
        """Otherwise one expired access token would break pushes for a full hour."""

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})

        transport = fcm_transport(handler)
        with pytest.raises(TransportError):
            await transport.send("tok", PAYLOAD)
        assert transport._token is None


CREDENTIALS = ApnsCredentials(
    key_id="ABC123", team_id="TEAM123", private_key="unused", topic="com.nightshift.app"
)


def apns_transport(handler) -> ApnsTransport:
    transport = ApnsTransport(
        CREDENTIALS, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    transport._token = "cached-provider-token"
    transport._token_minted_at = 9e9
    return transport


class TestApnsErrorMapping:
    @pytest.mark.asyncio
    async def test_bad_device_token_maps_to_pruning(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"reason": "BadDeviceToken"})

        with pytest.raises(TokenUnregistered):
            await apns_transport(handler).send("tok", PAYLOAD)

    @pytest.mark.asyncio
    async def test_expired_provider_token_is_retryable_and_clears_the_cache(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"reason": "ExpiredProviderToken"})

        transport = apns_transport(handler)
        with pytest.raises(TransportError) as excinfo:
            await transport.send("tok", PAYLOAD)
        assert excinfo.value.retryable is True
        assert transport._token is None

    @pytest.mark.asyncio
    async def test_the_alarm_headers_are_the_urgent_ones(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, headers={"apns-id": "abc"})

        assert await apns_transport(handler).send("tok", PAYLOAD) == "abc"
        assert seen["apns-priority"] == "10"
        assert seen["apns-push-type"] == "alert"
        assert seen["apns-topic"] == "com.nightshift.app"
