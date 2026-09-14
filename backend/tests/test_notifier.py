"""Who gets woken up, and what the database remembers about it.

The transports are fakes; the recipient query and the delivery rows are real, because
"the guard says he never got a notification" has to be answerable from the database.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.core.security import hash_password
from app.db import models
from app.modules.notifications.transport import (
    DeliveryStatus,
    Platform,
    PushDispatcher,
    TokenUnregistered,
    TransportError,
    recipients_for_site,
)
from app.services.intake import IntakeOutcome
from app.services.notifier import PushNotifier
from tests.conftest import requires_postgres
from tests.test_ingest import build_mail
from tests.test_intake import HUMAN_MAIL, NIGHT, identity_for, seed_site
from tests.test_transport import FakeTransport

pytestmark = requires_postgres


def add_user(session_factory, seed: dict, *, role: str, platform: str = "android") -> dict:
    """A user with one registered phone."""

    async def _add():
        async with session_factory() as session:
            user = models.User(
                id=uuid4(),
                tenant_id=seed["tenant_id"],
                email=f"{uuid4().hex[:8]}@example.com",
                password_hash=hash_password("x"),
                role=role,
            )
            session.add(user)
            await session.flush()
            device = models.PushDevice(
                id=uuid4(),
                tenant_id=seed["tenant_id"],
                user_id=user.id,
                platform=platform,
                token=f"tok-{uuid4().hex}",
            )
            session.add(device)
            await session.commit()
            return {"user_id": user.id, "token": device.token, "push_device_id": device.id}

    return asyncio.run(_add())


def outcome_for(session_factory, seed: dict) -> IntakeOutcome:
    """Run one real alarm mail through the intake so the payload is the real one."""
    from app.modules.ingest.pipeline import ingest
    from app.services.intake import MailIntake

    result = ingest(build_mail(HUMAN_MAIL), identity_for(seed), received_at=NIGHT)
    return asyncio.run(MailIntake(session_factory).handle(result))


def deliveries(session_factory, alarm_id) -> list[models.AlarmDelivery]:
    async def _load():
        async with session_factory() as session:
            return (
                (
                    await session.execute(
                        select(models.AlarmDelivery).where(
                            models.AlarmDelivery.alarm_id == alarm_id
                        )
                    )
                )
                .scalars()
                .all()
            )

    return asyncio.run(_load())


class TestRecipients:
    def test_only_people_who_may_see_alarms_are_woken(self, session_factory, clean_db):
        """A billing account does not get a perimeter alarm at 03:00."""
        seed = seed_site(session_factory)
        guard = add_user(session_factory, seed, role="GUARD")
        add_user(session_factory, seed, role="BILLING_ADMIN")

        async def _load():
            async with session_factory() as session:
                return await recipients_for_site(
                    session, tenant_id=seed["tenant_id"], site_id=seed["site_id"]
                )

        recipients = asyncio.run(_load())
        assert [r.token for r in recipients] == [guard["token"]]

    def test_an_inactive_user_is_not_woken(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        guard = add_user(session_factory, seed, role="GUARD")

        async def deactivate():
            async with session_factory() as session:
                user = await session.get(models.User, guard["user_id"])
                user.is_active = False
                await session.commit()

        asyncio.run(deactivate())

        async def _load():
            async with session_factory() as session:
                return await recipients_for_site(
                    session, tenant_id=seed["tenant_id"], site_id=seed["site_id"]
                )

        assert asyncio.run(_load()) == []

    def test_another_tenants_phones_are_never_included(self, session_factory, clean_db):
        mine = seed_site(session_factory)
        theirs = seed_site(session_factory)
        add_user(session_factory, mine, role="GUARD")
        add_user(session_factory, theirs, role="GUARD")

        async def _load():
            async with session_factory() as session:
                return await recipients_for_site(
                    session, tenant_id=mine["tenant_id"], site_id=mine["site_id"]
                )

        recipients = asyncio.run(_load())
        assert len(recipients) == 1


class TestNotifier:
    def test_a_successful_push_is_recorded(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        intake = outcome_for(session_factory, seed)
        assert intake.alarm_id is not None

        transport = FakeTransport(Platform.ANDROID, ["projects/x/messages/1"])
        notifier = PushNotifier(session_factory, PushDispatcher([transport]))
        report = asyncio.run(notifier.notify(intake))

        assert report.sent == 1
        rows = deliveries(session_factory, intake.alarm_id)
        assert len(rows) == 1
        assert rows[0].status == DeliveryStatus.SENT.value
        assert rows[0].channel == "fcm"
        assert rows[0].sent_at is not None

    def test_a_failure_is_recorded_with_its_reason(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        intake = outcome_for(session_factory, seed)

        transport = FakeTransport(
            Platform.ANDROID, [TransportError("fcm rejected", retryable=False)]
        )
        report = asyncio.run(
            PushNotifier(session_factory, PushDispatcher([transport])).notify(intake)
        )

        assert report.sent == 0
        row = deliveries(session_factory, intake.alarm_id)[0]
        assert row.status == DeliveryStatus.FAILED.value
        assert "fcm rejected" in row.error

    def test_a_dead_token_is_pruned(self, session_factory, clean_db):
        """A reinstalled app leaves a token that would otherwise fail on every alarm."""
        seed = seed_site(session_factory)
        phone = add_user(session_factory, seed, role="GUARD")
        intake = outcome_for(session_factory, seed)

        transport = FakeTransport(Platform.ANDROID, [TokenUnregistered()])
        report = asyncio.run(
            PushNotifier(session_factory, PushDispatcher([transport])).notify(intake)
        )

        assert report.pruned == 1

        async def still_there():
            async with session_factory() as session:
                return await session.get(models.PushDevice, phone["push_device_id"])

        assert asyncio.run(still_there()) is None

    def test_an_outcome_with_no_alarm_sends_nothing(self, session_factory, clean_db):
        seed = seed_site(session_factory, with_rule=False)
        add_user(session_factory, seed, role="GUARD")
        intake = outcome_for(session_factory, seed)

        transport = FakeTransport(Platform.ANDROID, ["should not be used"])
        assert asyncio.run(
            PushNotifier(session_factory, PushDispatcher([transport])).notify(intake)
        ) is None
        assert transport.calls == []

    def test_an_alarm_nobody_can_receive_still_leaves_a_trace(
        self, session_factory, clean_db
    ):
        """No phones registered: the report says so, loudly, rather than looking fine."""
        seed = seed_site(session_factory)
        intake = outcome_for(session_factory, seed)

        report = asyncio.run(
            PushNotifier(
                session_factory, PushDispatcher([FakeTransport(Platform.ANDROID, [])])
            ).notify(intake)
        )
        assert report.reached_anyone is False
        assert deliveries(session_factory, intake.alarm_id) == []
