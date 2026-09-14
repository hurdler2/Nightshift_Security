"""The sweep against a real database, and the endpoints an installer uses.

`test_silence_sweep.py` proves the decisions. This proves they turn into rows: an
alarm that a guard can actually open, a state that survives the next sweep, and the
three commissioning endpoints answering honestly when nothing has arrived yet.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import models
from app.services.persistence import touch_device_heartbeat
from app.workers.watchdog import sweep_once
from tests.conftest import requires_postgres
from tests.test_api import auth, login
from tests.test_commissioning import make_device, make_site, seed_account
from tests.test_intake import count, seed_site

pytestmark = requires_postgres


def set_last_seen(session_factory, device_id, *, minutes_ago: float | None, state: str = "OK"):
    async def _set():
        async with session_factory() as session:
            row = await session.get(models.DeviceSilenceState, device_id)
            device = await session.get(models.Device, device_id)
            if row is None:
                row = models.DeviceSilenceState(
                    device_id=device_id, tenant_id=device.tenant_id
                )
                session.add(row)
            row.last_message_at = (
                None if minutes_ago is None else datetime.now(UTC) - timedelta(minutes=minutes_ago)
            )
            row.state = state
            await session.commit()

    asyncio.run(_set())


def events_of(session_factory, event_type: str):
    async def _load():
        async with session_factory() as session:
            return (
                (
                    await session.execute(
                        select(models.Event).where(models.Event.event_type == event_type)
                    )
                )
                .scalars()
                .all()
            )

    return asyncio.run(_load())


class TestHeartbeat:
    def test_a_delivered_message_marks_the_recorder_alive(self, session_factory, clean_db):
        """The watchdog's only input: any mail at all, parsed or not."""
        seed = seed_site(session_factory)

        async def touch_and_read():
            async with session_factory() as session:
                await touch_device_heartbeat(session, seed["tenant_id"], seed["device_id"])
                await session.commit()
            async with session_factory() as session:
                return await session.get(models.DeviceSilenceState, seed["device_id"])

        row = asyncio.run(touch_and_read())
        assert row.state == "OK"
        assert row.last_message_at is not None

    def test_coming_back_from_silence_is_timestamped(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        set_last_seen(session_factory, seed["device_id"], minutes_ago=300, state="SILENT")

        async def touch_and_read():
            async with session_factory() as session:
                await touch_device_heartbeat(session, seed["tenant_id"], seed["device_id"])
                await session.commit()
            async with session_factory() as session:
                return await session.get(models.DeviceSilenceState, seed["device_id"])

        row = asyncio.run(touch_and_read())
        assert row.state == "OK"
        assert row.state_changed_at is not None


class TestSweep:
    def test_a_silent_recorder_becomes_an_alarm_a_guard_can_open(
        self, session_factory, clean_db
    ):
        seed = seed_site(session_factory)
        set_last_seen(session_factory, seed["device_id"], minutes_ago=120)

        result = asyncio.run(sweep_once(session_factory))
        assert len(result.alarms) == 1

        events = events_of(session_factory, "device_silent")
        assert len(events) == 1
        assert events[0].severity == "CRITICAL"
        assert count(session_factory, models.Alarm) == 1

    def test_the_second_sweep_does_not_alarm_again(self, session_factory, clean_db):
        """The state row is what stops a dark recorder alarming every minute."""
        seed = seed_site(session_factory)
        set_last_seen(session_factory, seed["device_id"], minutes_ago=120)

        asyncio.run(sweep_once(session_factory))
        second = asyncio.run(sweep_once(session_factory))

        assert second.alarms == []
        assert count(session_factory, models.Alarm) == 1

    def test_a_healthy_fleet_writes_nothing(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        set_last_seen(session_factory, seed["device_id"], minutes_ago=2)

        result = asyncio.run(sweep_once(session_factory))
        assert result.quiet
        assert count(session_factory, models.Event) == 0

    def test_the_state_row_records_the_silence(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        set_last_seen(session_factory, seed["device_id"], minutes_ago=120)
        asyncio.run(sweep_once(session_factory))

        async def state():
            async with session_factory() as session:
                return await session.get(models.DeviceSilenceState, seed["device_id"])

        row = asyncio.run(state())
        assert row.state == "SILENT"
        assert row.state_changed_at is not None

    def test_another_tenants_fleet_is_swept_independently(self, session_factory, clean_db):
        mine = seed_site(session_factory)
        theirs = seed_site(session_factory)
        set_last_seen(session_factory, mine["device_id"], minutes_ago=120)
        set_last_seen(session_factory, theirs["device_id"], minutes_ago=2)

        asyncio.run(sweep_once(session_factory))
        events = events_of(session_factory, "device_silent")
        assert [e.tenant_id for e in events] == [mine["tenant_id"]]


class TestCommissioningEndpoints:
    def test_silence_state_answers_before_anything_has_arrived(
        self, client, session_factory
    ):
        """A freshly installed recorder is NEVER_SEEN, not healthy."""
        account = seed_account(session_factory)
        head = auth(login(client, account["email"]))
        device = make_device(client, head, make_site(client, head)["id"])

        response = client.get(f"/v1/devices/{device['id']}/silence-state", headers=head)
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "NEVER_SEEN"
        assert body["last_message_at"] is None
        assert body["is_security_relevant"] is False

    def test_test_alarm_says_what_is_wrong_when_nothing_arrived(
        self, client, session_factory
    ):
        """The commissioning answer has to be actionable, not just false."""
        account = seed_account(session_factory)
        head = auth(login(client, account["email"]))
        device = make_device(client, head, make_site(client, head)["id"])

        body = client.post(f"/v1/devices/{device['id']}/test-alarm", headers=head).json()
        assert body["verified"] is False
        assert "587" in body["detail"]

    def test_email_samples_are_empty_until_a_recorder_sends_one(
        self, client, session_factory
    ):
        account = seed_account(session_factory)
        head = auth(login(client, account["email"]))
        device = make_device(client, head, make_site(client, head)["id"])

        response = client.get(f"/v1/devices/{device['id']}/email-samples", headers=head)
        assert response.status_code == 200
        assert response.json() == []

    def test_a_guard_cannot_read_raw_samples(self, client, session_factory):
        """Raw messages are device internals; they are not guard-facing."""
        account = seed_account(session_factory)
        head = auth(login(client, account["email"]))
        device = make_device(client, head, make_site(client, head)["id"])

        guard = seed_account(session_factory, role="GUARD")
        guard_head = auth(login(client, guard["email"]))
        response = client.get(f"/v1/devices/{device['id']}/email-samples", headers=guard_head)
        assert response.status_code == 403
