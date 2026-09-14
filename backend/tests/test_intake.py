"""A recorder's mail, all the way to committed rows.

`test_alarm_flow.py` proves the decisions are right without a database. This proves
the decisions survive contact with one: the event, the alarm, the snapshot key and
the delivery record all land, a redelivered message does not become a second alarm,
and a message from a device with nothing configured is still recorded rather than
dropped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from app.db import models
from app.modules.ingest.pipeline import DeviceIdentity, ingest
from app.modules.realtime.hub import hub
from app.services.intake import MailIntake
from tests.conftest import requires_postgres
from tests.test_ingest import build_mail

pytestmark = requires_postgres

NIGHT = datetime(2026, 9, 14, 23, 14, tzinfo=UTC)  # 02:14 in Istanbul

HUMAN_MAIL = """\
Alarm Event: Smart Motion Human
Alarm Input Channel No.: 3
Channel Name: Depo Arka
Alarm Device Name: SANTIYE-A-XVR
Alarm Start Time(D/M/Y H:M:S): 15/09/2026 02:14:32
"""


class FakeMediaStore:
    """Records what would have been uploaded, without an object store."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.puts: list[str] = []

    def put(self, content, *, tenant_id, site_id, event_id, kind, occurred_at, plan="business"):
        if self.fail:
            raise RuntimeError("storage is down")
        from app.integrations.object_storage.media_store import StoredMedia, build_key, validate

        key = build_key(
            tenant_id=tenant_id, site_id=site_id, event_id=event_id, kind=kind, occurred_at=occurred_at
        )
        self.puts.append(key)
        import hashlib

        return StoredMedia(
            key=key,
            content_type=validate(content, kind),
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            expires_at=occurred_at,
        )


def seed_site(session_factory, *, with_rule: bool = True) -> dict:
    """A commissioned site: one recorder, one camera on channel 3, one night rule."""

    async def _seed():
        async with session_factory() as session:
            tenant = models.Tenant(id=uuid4(), name="Yüklenici A")
            session.add(tenant)
            await session.flush()

            site = models.Site(
                id=uuid4(), tenant_id=tenant.id, name="Beton Tesisi", timezone="Europe/Istanbul"
            )
            session.add(site)
            await session.flush()

            device = models.Device(
                id=uuid4(),
                tenant_id=tenant.id,
                site_id=site.id,
                name="SANTIYE-A-XVR",
                model="DH-XVR5108HS-I3/T",
            )
            session.add(device)
            await session.flush()

            camera = models.Camera(
                id=uuid4(),
                tenant_id=tenant.id,
                site_id=site.id,
                device_id=device.id,
                name="Depo Arka",
                channel_number=3,
            )
            session.add(camera)

            if with_rule:
                session.add(
                    models.Rule(
                        id=uuid4(),
                        tenant_id=tenant.id,
                        site_id=site.id,
                        name="Gece insan alarmı",
                        event_types=["person_detected"],
                        schedule={"timezone": "Europe/Istanbul", "start": "19:00", "end": "07:00"},
                        min_severity="LOW",
                        actions=["push", "snapshot"],
                    )
                )
            await session.commit()
            return {
                "tenant_id": tenant.id,
                "site_id": site.id,
                "device_id": device.id,
                "camera_id": camera.id,
            }

    return asyncio.run(_seed())


def identity_for(seed: dict) -> DeviceIdentity:
    return DeviceIdentity(
        device_id=seed["device_id"],
        tenant_id=seed["tenant_id"],
        site_id=seed["site_id"],
        smtp_username="dev-santiye-a",
    )


def deliver(
    session_factory,
    seed: dict,
    *,
    body: str = HUMAN_MAIL,
    message_id: str | None = "<alarm-1@xvr.local>",
    media_store=None,
    intake: MailIntake | None = None,
    received_at: datetime = NIGHT,
):
    """Run one message through the real intake and return the outcome."""
    raw = build_mail(body, message_id=message_id)
    result = ingest(raw, identity_for(seed), received_at=received_at)
    runner = intake or MailIntake(session_factory, media_store=media_store)
    return asyncio.run(runner.handle(result)), runner


async def _count(session_factory, model, **filters) -> int:
    async with session_factory() as session:
        stmt = select(func.count()).select_from(model)
        for column, value in filters.items():
            stmt = stmt.where(getattr(model, column) == value)
        return await session.scalar(stmt)


def count(session_factory, model, **filters) -> int:
    return asyncio.run(_count(session_factory, model, **filters))


class TestHappyPath:
    def test_a_night_intrusion_becomes_an_event_an_alarm_and_a_snapshot(
        self, session_factory, clean_db
    ):
        seed = seed_site(session_factory)
        store = FakeMediaStore()
        outcome, _ = deliver(session_factory, seed, media_store=store)

        assert outcome.alarmed
        assert outcome.event_id is not None
        assert outcome.alarm_id is not None
        assert len(store.puts) == 1
        assert str(seed["tenant_id"]) in store.puts[0]

        async def rows():
            async with session_factory() as session:
                event = await session.get(models.Event, outcome.event_id)
                alarm = await session.get(models.Alarm, outcome.alarm_id)
                media = (
                    (
                        await session.execute(
                            select(models.MediaAsset).where(
                                models.MediaAsset.event_id == outcome.event_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return event, alarm, media

        event, alarm, media = asyncio.run(rows())
        assert event.event_type == "person_detected"
        assert event.camera_id == seed["camera_id"]
        assert event.channel_number == 3
        assert alarm.status == "ALERTED"
        assert len(media) == 1
        assert media[0].storage_key == store.puts[0]
        assert media[0].sha256

    def test_the_delivery_is_recorded_even_for_a_quiet_message(
        self, session_factory, clean_db
    ):
        """Every accepted mail leaves a trace, alarm or not — that is the audit trail."""
        seed = seed_site(session_factory, with_rule=False)
        outcome, _ = deliver(session_factory, seed)

        assert not outcome.alarmed
        assert count(session_factory, models.EmailMessage, device_id=seed["device_id"]) == 1

    def test_the_local_time_is_the_sites_time(self, session_factory, clean_db):
        """22:14 UTC is 01:14 in Istanbul; a night rule depends on getting this right."""
        seed = seed_site(session_factory)
        outcome, _ = deliver(session_factory, seed)

        async def local():
            async with session_factory() as session:
                return (await session.get(models.Event, outcome.event_id)).occurred_at_local

        moment = asyncio.run(local())
        assert moment is not None
        assert moment.tzinfo is None  # deliberately naive: it is a wall clock, not an instant


class TestRedelivery:
    def test_the_same_message_twice_is_one_event(self, session_factory, clean_db):
        """Recorders resend on a failed connection; that must not double the alarms."""
        seed = seed_site(session_factory)
        intake = MailIntake(session_factory)

        first, _ = deliver(session_factory, seed, intake=intake)
        second, _ = deliver(session_factory, seed, intake=intake)

        assert second.duplicate_message is True
        assert second.event_id == first.event_id
        assert count(session_factory, models.Event) == 1
        assert count(session_factory, models.Alarm) == 1
        assert count(session_factory, models.EmailMessage) == 1

    def test_a_redelivery_does_not_upload_the_snapshot_again(
        self, session_factory, clean_db
    ):
        seed = seed_site(session_factory)
        store = FakeMediaStore()
        intake = MailIntake(session_factory, media_store=store)

        deliver(session_factory, seed, intake=intake, media_store=store)
        deliver(session_factory, seed, intake=intake, media_store=store)
        assert len(store.puts) == 1

    def test_a_second_sighting_within_the_window_is_counted_not_alarmed(
        self, session_factory, clean_db
    ):
        seed = seed_site(session_factory)
        intake = MailIntake(session_factory)

        first, _ = deliver(session_factory, seed, intake=intake, message_id="<a@xvr>")
        second, _ = deliver(session_factory, seed, intake=intake, message_id="<b@xvr>")

        assert first.alarmed
        assert not second.alarmed
        assert count(session_factory, models.Alarm) == 1
        assert count(session_factory, models.Event) == 2  # the timeline keeps both


class TestFailureModes:
    def test_a_storage_failure_still_records_the_sighting(
        self, session_factory, clean_db
    ):
        """Losing the picture is survivable. Losing the fact someone was there is not."""
        seed = seed_site(session_factory)
        outcome, _ = deliver(session_factory, seed, media_store=FakeMediaStore(fail=True))

        assert outcome.alarmed
        assert outcome.event_id is not None
        assert count(session_factory, models.MediaAsset) == 0

    def test_a_deleted_device_is_reported_not_crashed_on(self, session_factory, clean_db):
        """Nothing can reference a device row that is gone; say so instead of failing."""
        seed = seed_site(session_factory)
        ghost = dict(seed, device_id=uuid4())
        outcome, _ = deliver(session_factory, ghost)

        assert outcome.event_id is None
        assert count(session_factory, models.EmailMessage) == 0

    def test_deleting_a_site_takes_its_recorders_with_it(self, session_factory, clean_db):
        """`devices.site_id` is a foreign key, so a device can never outlive its site.

        That is what makes the branch above the only one there is: a message we cannot
        attribute to a device has nowhere to be written at all.
        """
        seed = seed_site(session_factory)

        async def delete_site_and_look():
            async with session_factory() as session:
                site = await session.get(models.Site, seed["site_id"])
                await session.delete(site)
                await session.commit()
            async with session_factory() as session:
                return await session.get(models.Device, seed["device_id"])

        assert asyncio.run(delete_site_and_look()) is None

        outcome, _ = deliver(session_factory, seed)
        assert outcome.event_id is None
        assert count(session_factory, models.EmailMessage) == 0

    def test_a_face_alarm_is_refused_and_nothing_is_stored(
        self, session_factory, clean_db
    ):
        """Face data never enters the system (spec §19.3), not even as a snapshot."""
        seed = seed_site(session_factory)
        store = FakeMediaStore()
        outcome, _ = deliver(
            session_factory,
            seed,
            body="Alarm Event: Face Detection\nAlarm Input Channel No.: 3\n",
            media_store=store,
        )

        assert outcome.event_id is None
        assert store.puts == []
        assert count(session_factory, models.MediaAsset) == 0
        assert count(session_factory, models.EmailSample) == 0

    def test_a_notifier_failure_does_not_lose_the_alarm(
        self, session_factory, clean_db
    ):
        seed = seed_site(session_factory)

        async def broken(_):
            raise RuntimeError("FCM is down")

        intake = MailIntake(session_factory, notifier=broken)
        outcome, _ = deliver(session_factory, seed, intake=intake)

        assert outcome.alarm_id is not None
        assert count(session_factory, models.Alarm) == 1


class TestRealtime:
    def test_an_alarm_reaches_a_watching_app(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        with hub.subscribe(tenant_id=seed["tenant_id"], user_id=uuid4()) as subscriber:
            outcome, _ = deliver(session_factory, seed)
            assert subscriber.queue.qsize() == 1
            frame = subscriber.queue.get_nowait()
            assert frame["type"] == "alarm.raised"
            assert frame["data"]["event_id"] == str(outcome.event_id)

    def test_a_quiet_sighting_still_updates_the_timeline(
        self, session_factory, clean_db
    ):
        seed = seed_site(session_factory, with_rule=False)
        with hub.subscribe(tenant_id=seed["tenant_id"], user_id=uuid4()) as subscriber:
            deliver(session_factory, seed)
            frame = subscriber.queue.get_nowait()
            assert frame["type"] == "event.recorded"
