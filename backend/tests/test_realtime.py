"""The live channel: who may listen, and what happens when a phone falls behind.

The handshake tests matter most. A WebSocket that accepts a token from a query string
is the easiest place in an API to accidentally accept the wrong kind of token, so the
refusals are tested at least as carefully as the acceptance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.modules.auth.tokens import create_access_token, issue_tokens
from app.modules.realtime.hub import (
    QUEUE_LIMIT,
    Hub,
    alarm_status_frame,
    event_frame,
)

TENANT = uuid4()
OTHER_TENANT = uuid4()
USER = uuid4()


def frame(n: int = 0) -> dict:
    return {"type": "test", "data": {"n": n}}


class TestHub:
    def test_a_frame_reaches_only_its_own_tenant(self):
        """The whole multi-tenant story fails here if it fails anywhere."""
        hub = Hub()
        with hub.subscribe(tenant_id=TENANT, user_id=USER) as mine, hub.subscribe(
            tenant_id=OTHER_TENANT, user_id=uuid4()
        ) as theirs:
            assert hub.publish(TENANT, frame()) == 1
            assert mine.queue.qsize() == 1
            assert theirs.queue.qsize() == 0

    def test_publishing_to_nobody_is_not_an_error(self):
        assert Hub().publish(TENANT, frame()) == 0

    def test_leaving_removes_the_subscriber(self):
        hub = Hub()
        with hub.subscribe(tenant_id=TENANT, user_id=USER):
            assert hub.subscriber_count(TENANT) == 1
        assert hub.subscriber_count(TENANT) == 0
        assert hub.subscriber_count() == 0

    def test_a_slow_client_is_dropped_rather_than_blocking_the_alarm_path(self):
        hub = Hub()
        with hub.subscribe(tenant_id=TENANT, user_id=USER) as subscriber:
            for n in range(QUEUE_LIMIT + 5):
                hub.publish(TENANT, frame(n))
            assert subscriber.queue.qsize() == QUEUE_LIMIT
            assert subscriber.dropped == 5

    def test_one_slow_client_does_not_starve_the_others(self):
        hub = Hub()
        with hub.subscribe(tenant_id=TENANT, user_id=USER) as slow, hub.subscribe(
            tenant_id=TENANT, user_id=uuid4()
        ) as fast:
            for n in range(QUEUE_LIMIT):
                hub.publish(TENANT, frame(n))
            while not fast.queue.empty():  # the fast client keeps reading
                fast.queue.get_nowait()
            assert hub.publish(TENANT, frame(999)) == 1
            assert slow.dropped == 1


class TestFrames:
    def test_frames_carry_a_type_and_a_timestamp(self):
        f = alarm_status_frame(alarm_id=uuid4(), status="ACKNOWLEDGED", actor_id=USER)
        assert f["type"] == "alarm.updated"
        datetime.fromisoformat(f["sent_at"])  # parses, so clients can order frames

    def test_an_event_frame_survives_a_missing_camera(self):
        """A channel with no camera row still belongs on the timeline."""
        f = event_frame(
            event_id=uuid4(),
            event_type="person_detected",
            severity="MEDIUM",
            camera_name=None,
            occurred_at=datetime.now(UTC),
        )
        assert f["data"]["camera_name"] is None


def access(role: str = "GUARD", *, tenant_id=TENANT) -> str:
    return create_access_token(
        user_id=USER,
        tenant_id=tenant_id,
        role=role,
        secret=get_settings().jwt_secret,
        ttl_minutes=10,
    )


class TestHandshake:
    """Uses the real app; the socket is closed before any database work happens."""

    def test_no_token_is_refused(self, client):
        # starlette raises when the server refuses the handshake
        with pytest.raises(Exception), client.websocket_connect("/v1/realtime"):  # noqa: B017
            pass

    def test_a_garbage_token_is_refused(self, client):
        # starlette raises when the server refuses the handshake
        with pytest.raises(Exception), client.websocket_connect("/v1/realtime?access_token=not-a-jwt"):  # noqa: B017
            pass

    def test_a_refresh_token_is_not_accepted_as_an_access_token(self, client):
        """The client holds both; only the short-lived one may open a live socket."""
        tokens = issue_tokens(
            user_id=USER, tenant_id=TENANT, role="GUARD", secret=get_settings().jwt_secret
        )
        # starlette raises when the server refuses the handshake
        with pytest.raises(Exception), client.websocket_connect(f"/v1/realtime?access_token={tokens.refresh_token}"):  # noqa: B017
            pass

    def test_an_expired_token_is_refused(self, client):
        token = create_access_token(
            user_id=USER,
            tenant_id=TENANT,
            role="GUARD",
            secret=get_settings().jwt_secret,
            ttl_minutes=10,
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        # starlette raises when the server refuses the handshake
        with pytest.raises(Exception), client.websocket_connect(f"/v1/realtime?access_token={token}"):  # noqa: B017
            pass

    def test_a_billing_account_cannot_watch_the_alarm_feed(self, client):
        # starlette raises when the server refuses the handshake
        with pytest.raises(Exception), client.websocket_connect(f"/v1/realtime?access_token={access('BILLING_ADMIN')}"):  # noqa: B017
            pass

    def test_a_guard_gets_a_hello_and_then_live_alarms(self, client):
        from app.modules.realtime.hub import hub

        with client.websocket_connect(f"/v1/realtime?access_token={access()}") as socket:
            hello = socket.receive_json()
            assert hello["type"] == "hello"
            assert hello["data"]["resume"] == "refetch"

            alarm_id = uuid4()
            hub.publish(TENANT, alarm_status_frame(alarm_id=alarm_id, status="ALERTED", actor_id=None))
            frame = socket.receive_json()
            assert frame["type"] == "alarm.updated"
            assert frame["data"]["alarm_id"] == str(alarm_id)

    def test_another_tenants_alarm_never_arrives(self, client):
        from app.modules.realtime.hub import hub

        with client.websocket_connect(f"/v1/realtime?access_token={access()}") as socket:
            socket.receive_json()  # hello
            hub.publish(OTHER_TENANT, alarm_status_frame(alarm_id=uuid4(), status="ALERTED", actor_id=None))
            mine = alarm_status_frame(alarm_id=uuid4(), status="ALERTED", actor_id=None)
            hub.publish(TENANT, mine)
            received = socket.receive_json()
            assert received["data"]["alarm_id"] == mine["data"]["alarm_id"]
