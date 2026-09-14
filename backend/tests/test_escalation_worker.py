"""An unanswered alarm climbing, and every way it must stop climbing.

Real database, real alarm rows, fake transports. The case worth the most here is the
acknowledgement that lands *while* a later step is already due — the one a naive timer
gets wrong and then wakes the company owner for an alarm the guard already handled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.db import models
from app.modules.notifications.transport import Platform, PushDispatcher
from app.workers.escalation import ROLE_FALLBACK, sweep_once
from tests.conftest import requires_postgres
from tests.test_intake import seed_site
from tests.test_notifier import add_user
from tests.test_transport import FakeTransport

pytestmark = requires_postgres


def open_alarm(session_factory, seed: dict, *, minutes_ago: float, severity: str = "HIGH"):
    """An alarm nobody has acknowledged, opened this long ago."""

    async def _create():
        opened = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        async with session_factory() as session:
            event = models.Event(
                id=uuid4(),
                tenant_id=seed["tenant_id"],
                site_id=seed["site_id"],
                device_id=seed["device_id"],
                event_type="person_detected",
                occurred_at=opened,
                dedup_key=f"k-{uuid4().hex[:8]}",
                severity=severity,
                risk_score=78,
                risk_reasons=["Gece saati", "Yasak bölge"],
                status="ALERTED",
            )
            session.add(event)
            await session.flush()
            alarm = models.Alarm(
                id=uuid4(),
                tenant_id=seed["tenant_id"],
                site_id=seed["site_id"],
                event_id=event.id,
                severity=severity,
                status="ALERTED",
                opened_at=opened,
            )
            session.add(alarm)
            await session.commit()
            return alarm.id

    return asyncio.run(_create())


def acknowledge(session_factory, alarm_id, user_id=None):
    async def _ack():
        async with session_factory() as session:
            alarm = await session.get(models.Alarm, alarm_id)
            alarm.acknowledged_at = datetime.now(UTC)
            alarm.acknowledged_by = user_id
            alarm.status = "ACKNOWLEDGED"
            await session.commit()

    asyncio.run(_ack())


def steps_delivered(session_factory, alarm_id) -> list[int]:
    async def _load():
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(models.AlarmDelivery.escalation_step)
                        .where(models.AlarmDelivery.alarm_id == alarm_id)
                        .order_by(models.AlarmDelivery.escalation_step)
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)

    return asyncio.run(_load())


def dispatcher(outcomes: int = 20) -> tuple[PushDispatcher, FakeTransport]:
    transport = FakeTransport(Platform.ANDROID, ["ok"] * outcomes)
    return PushDispatcher([transport]), transport


class TestClimbing:
    def test_a_fresh_alarm_delivers_the_first_step(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        alarm_id = open_alarm(session_factory, seed, minutes_ago=0)

        push, transport = dispatcher()
        outcome = asyncio.run(sweep_once(push, session_factory))

        assert [step for _, step, _ in outcome.escalated] == [0]
        assert len(transport.calls) == 1
        assert steps_delivered(session_factory, alarm_id) == [0]

    def test_an_unanswered_alarm_climbs_to_the_next_role(self, session_factory, clean_db):
        """Two minutes without an acknowledgement: the site manager is woken."""
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        add_user(session_factory, seed, role="SITE_MANAGER")
        alarm_id = open_alarm(session_factory, seed, minutes_ago=2)

        push, _ = dispatcher()
        outcome = asyncio.run(sweep_once(push, session_factory))

        assert [step for _, step, _ in outcome.escalated] == [0, 1]
        assert steps_delivered(session_factory, alarm_id) == [0, 1]

    def test_a_critical_alarm_climbs_faster(self, session_factory, clean_db):
        """CRITICAL runs 0/30/90/240 seconds, so two minutes is already three steps.

        The same two minutes on a HIGH alarm (0/60/180) is only two — which is the
        whole point of the separate policy.
        """
        seed = seed_site(session_factory)
        for role in ("GUARD", "SITE_MANAGER", "SECURITY_MANAGER", "OWNER"):
            add_user(session_factory, seed, role=role)
        critical = open_alarm(session_factory, seed, minutes_ago=2, severity="CRITICAL")
        high = open_alarm(session_factory, seed, minutes_ago=2, severity="HIGH")

        push, _ = dispatcher()
        asyncio.run(sweep_once(push, session_factory))

        assert steps_delivered(session_factory, critical) == [0, 1, 2]
        assert steps_delivered(session_factory, high) == [0, 1]

    def test_the_whole_critical_chain_ends_at_the_owner(self, session_factory, clean_db):
        """Four minutes unanswered on a CRITICAL alarm reaches the last step."""
        seed = seed_site(session_factory)
        for role in ("GUARD", "SITE_MANAGER", "SECURITY_MANAGER", "OWNER"):
            add_user(session_factory, seed, role=role)
        alarm_id = open_alarm(session_factory, seed, minutes_ago=4, severity="CRITICAL")

        push, _ = dispatcher()
        asyncio.run(sweep_once(push, session_factory))

        assert steps_delivered(session_factory, alarm_id) == [0, 1, 2, 3]

    def test_a_step_is_never_delivered_twice(self, session_factory, clean_db):
        """A restarted worker must not re-page everyone it already paged."""
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        alarm_id = open_alarm(session_factory, seed, minutes_ago=0)

        push, transport = dispatcher()
        asyncio.run(sweep_once(push, session_factory))
        second = asyncio.run(sweep_once(push, session_factory))

        assert second.quiet
        assert len(transport.calls) == 1
        assert steps_delivered(session_factory, alarm_id) == [0]


class TestStopping:
    def test_an_acknowledgement_stops_the_chain(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        add_user(session_factory, seed, role="SITE_MANAGER")
        alarm_id = open_alarm(session_factory, seed, minutes_ago=0)

        push, transport = dispatcher()
        asyncio.run(sweep_once(push, session_factory))
        acknowledge(session_factory, alarm_id)

        # Time passes well past the second step.
        later = datetime.now(UTC) + timedelta(minutes=10)
        outcome = asyncio.run(sweep_once(push, session_factory, now=later))

        assert outcome.quiet
        assert len(transport.calls) == 1

    def test_an_acknowledgement_that_arrives_while_a_step_is_due_still_stops_it(
        self, session_factory, clean_db
    ):
        """The chain is rebuilt from the row each sweep, so there is no stale timer."""
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        add_user(session_factory, seed, role="SECURITY_MANAGER")
        alarm_id = open_alarm(session_factory, seed, minutes_ago=5)
        acknowledge(session_factory, alarm_id)

        push, transport = dispatcher()
        outcome = asyncio.run(sweep_once(push, session_factory))

        assert outcome.quiet
        assert transport.calls == []
        assert steps_delivered(session_factory, alarm_id) == []

    def test_a_resolved_alarm_does_not_climb(self, session_factory, clean_db):
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        alarm_id = open_alarm(session_factory, seed, minutes_ago=5)

        async def resolve():
            async with session_factory() as session:
                alarm = await session.get(models.Alarm, alarm_id)
                alarm.resolved_at = datetime.now(UTC)
                alarm.status = "RESOLVED"
                await session.commit()

        asyncio.run(resolve())
        push, transport = dispatcher()
        assert asyncio.run(sweep_once(push, session_factory)).quiet
        assert transport.calls == []

    def test_an_ancient_alarm_is_left_alone(self, session_factory, clean_db):
        """A fourth notification three hours later helps nobody."""
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        open_alarm(session_factory, seed, minutes_ago=200)

        push, transport = dispatcher()
        assert asyncio.run(sweep_once(push, session_factory)).quiet
        assert transport.calls == []


class TestReachability:
    def test_a_step_with_nobody_in_that_role_falls_through(
        self, session_factory, clean_db
    ):
        """A customer with no SITE_MANAGER must not have a dead-end at step two."""
        seed = seed_site(session_factory)
        add_user(session_factory, seed, role="GUARD")
        manager = add_user(session_factory, seed, role="SECURITY_MANAGER")
        alarm_id = open_alarm(session_factory, seed, minutes_ago=2)

        push, transport = dispatcher()
        asyncio.run(sweep_once(push, session_factory))

        assert steps_delivered(session_factory, alarm_id) == [0, 1]
        # Step 1 (SITE_MANAGER) reached the security manager instead of nobody.
        assert manager["token"] in transport.calls

    def test_an_alarm_nobody_can_receive_does_not_spin(self, session_factory, clean_db):
        """No phones at all: the step is spent, not retried every ten seconds."""
        seed = seed_site(session_factory)
        alarm_id = open_alarm(session_factory, seed, minutes_ago=0)

        push, transport = dispatcher()
        first = asyncio.run(sweep_once(push, session_factory))
        second = asyncio.run(sweep_once(push, session_factory))

        assert transport.calls == []
        assert first.quiet and second.quiet
        assert steps_delivered(session_factory, alarm_id) == []

    def test_another_tenants_alarm_is_never_escalated_to_our_phones(
        self, session_factory, clean_db
    ):
        mine = seed_site(session_factory)
        theirs = seed_site(session_factory)
        ours = add_user(session_factory, mine, role="GUARD")
        add_user(session_factory, theirs, role="GUARD")
        open_alarm(session_factory, theirs, minutes_ago=0)

        push, transport = dispatcher()
        asyncio.run(sweep_once(push, session_factory))

        assert ours["token"] not in transport.calls
        assert len(transport.calls) == 1


class TestFallbackTable:
    def test_every_role_eventually_reaches_the_owner(self):
        """A chain that dead-ends is a chain that silently drops an alarm."""
        for role, fallbacks in ROLE_FALLBACK.items():
            assert role == "OWNER" or fallbacks[-1] == "OWNER"
