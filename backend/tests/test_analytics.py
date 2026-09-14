"""The pilot report's numbers, and the ones it refuses to invent.

Spec §22.2 lists what a week on a real site has to measure. These tests check the
arithmetic, but mostly they check the honesty: an empty week reports no rate rather
than a flattering zero, a false-positive rate is computed against resolved alarms
only, and the metrics no query can answer are named instead of quietly dropped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.db import models
from app.modules.analytics.router import NOT_MEASURABLE
from tests.conftest import requires_postgres
from tests.test_api import auth, login
from tests.test_commissioning import seed_account
from tests.test_intake import seed_site

pytestmark = requires_postgres


def attach_user(session_factory, seed: dict, *, role: str = "ADMIN") -> dict:
    """A login for the tenant a seeded site belongs to."""
    from app.core.security import hash_password
    from tests.test_api import PASSWORD

    async def _add():
        async with session_factory() as session:
            user = models.User(
                id=uuid4(),
                tenant_id=seed["tenant_id"],
                email=f"{uuid4().hex[:8]}@example.com",
                password_hash=hash_password(PASSWORD),
                role=role,
            )
            session.add(user)
            await session.commit()
            return {"email": user.email, "user_id": user.id}

    return asyncio.run(_add())


def add_alarm(
    session_factory,
    seed: dict,
    *,
    minutes_ago: float = 10,
    severity: str = "HIGH",
    resolution: str | None = None,
    ack_after_seconds: float | None = None,
    with_media: bool = False,
    occurrence_count: int = 1,
) -> dict:
    """One event, its alarm, and optionally how it was answered and closed."""

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
                risk_score=70,
                status="ALERTED",
                occurrence_count=occurrence_count,
            )
            session.add(event)
            await session.flush()

            if with_media:
                session.add(
                    models.MediaAsset(
                        id=uuid4(),
                        tenant_id=seed["tenant_id"],
                        event_id=event.id,
                        kind="snapshot",
                        storage_key=f"k/{uuid4().hex}.jpg",
                        content_type="image/jpeg",
                        size_bytes=250_000,
                        sha256=uuid4().hex,
                    )
                )

            alarm = models.Alarm(
                id=uuid4(),
                tenant_id=seed["tenant_id"],
                site_id=seed["site_id"],
                event_id=event.id,
                severity=severity,
                status="ALERTED",
                opened_at=opened,
            )
            if ack_after_seconds is not None:
                alarm.acknowledged_at = opened + timedelta(seconds=ack_after_seconds)
                alarm.status = "ACKNOWLEDGED"
            if resolution is not None:
                alarm.resolved_at = opened + timedelta(seconds=(ack_after_seconds or 0) + 60)
                alarm.resolution_code = resolution
                alarm.status = "RESOLVED"
                if alarm.acknowledged_at is None:
                    alarm.acknowledged_at = alarm.resolved_at
            session.add(alarm)
            await session.commit()
            return {"event_id": event.id, "alarm_id": alarm.id}

    return asyncio.run(_create())


def summary(client, head, **params) -> dict:
    response = client.get("/v1/analytics/summary", headers=head, params=params)
    assert response.status_code == 200, response.text
    return response.json()


class TestEmptyWindow:
    def test_a_quiet_week_reports_no_rates_rather_than_zeroes(
        self, client, session_factory, clean_db
    ):
        """A rate of 0.0 reads as "perfect"; None reads as "nothing happened"."""
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))

        body = summary(client, head)
        assert body["volume"]["events"] == 0
        assert body["quality"]["false_positive_rate"] is None
        assert body["delivery"]["delivery_rate"] is None
        assert body["response"]["ack_seconds_p50"] is None

    def test_the_unmeasurable_metrics_are_named_not_omitted(
        self, client, session_factory, clean_db
    ):
        """Spec §22.2 asks for numbers we cannot produce; say so, don't approximate."""
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))

        body = summary(client, head)
        assert set(body["not_measurable"]) == set(NOT_MEASURABLE)
        assert "footage" in body["not_measurable"]["missed_events"]


class TestVolume:
    def test_events_alarms_and_snapshots_are_counted(
        self, client, session_factory, clean_db
    ):
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))
        add_alarm(session_factory, seed, with_media=True)
        add_alarm(session_factory, seed, with_media=False)

        volume = summary(client, head)["volume"]
        assert volume["events"] == 2
        assert volume["alarms"] == 2
        assert volume["media_stored"] == 1
        assert volume["media_bytes"] == 250_000
        assert volume["snapshot_attachment_rate"] == 0.5

    def test_deduplicated_repeats_are_reported_separately(
        self, client, session_factory, clean_db
    ):
        """The dedup window's work is invisible otherwise, and it is most of the volume."""
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))
        add_alarm(session_factory, seed, occurrence_count=5)

        volume = summary(client, head)["volume"]
        assert volume["events"] == 1
        assert volume["duplicates_suppressed"] == 4

    def test_events_outside_the_window_are_excluded(self, client, session_factory, clean_db):
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))
        add_alarm(session_factory, seed, minutes_ago=60 * 24 * 30)
        add_alarm(session_factory, seed, minutes_ago=5)

        assert summary(client, head, days=7)["volume"]["events"] == 1
        assert summary(client, head, days=60)["volume"]["events"] == 2


class TestQuality:
    def test_the_false_positive_rate_counts_only_resolved_alarms(
        self, client, session_factory, clean_db
    ):
        """Counting unanswered alarms as correct flatters the number exactly when
        people have stopped closing them."""
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))
        add_alarm(session_factory, seed, resolution="FALSE_POSITIVE")
        add_alarm(session_factory, seed, resolution="TRUE_SECURITY_INCIDENT")
        add_alarm(session_factory, seed)  # still open

        quality = summary(client, head)["quality"]
        assert quality["resolved"] == 2
        assert quality["unresolved"] == 1
        assert quality["false_positive_rate"] == 0.5

    def test_suspicious_activity_counts_as_a_true_positive(
        self, client, session_factory, clean_db
    ):
        """It is the code a guard uses when someone was there but did not break in."""
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))
        add_alarm(session_factory, seed, resolution="SUSPICIOUS_ACTIVITY")

        quality = summary(client, head)["quality"]
        assert quality["true_positive"] == 1
        assert quality["false_positive"] == 0

    def test_weather_and_animals_are_reported_by_code(
        self, client, session_factory, clean_db
    ):
        """The breakdown is the AI tuning dataset (spec §11.4), so it is not lumped."""
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))
        add_alarm(session_factory, seed, resolution="ANIMAL")
        add_alarm(session_factory, seed, resolution="WEATHER")
        add_alarm(session_factory, seed, resolution="WEATHER")

        codes = summary(client, head)["quality"]["by_resolution_code"]
        assert codes == {"ANIMAL": 1, "WEATHER": 2}


class TestResponse:
    def test_acknowledgement_latency_is_measured(self, client, session_factory, clean_db):
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))
        add_alarm(session_factory, seed, ack_after_seconds=30)
        add_alarm(session_factory, seed, ack_after_seconds=90)

        response = summary(client, head)["response"]
        assert response["acknowledged"] == 2
        assert response["never_acknowledged"] == 0
        assert 30 <= response["ack_seconds_p50"] <= 90

    def test_alarms_nobody_answered_are_counted(self, client, session_factory, clean_db):
        """The most important number on the page: alarms that reached nobody."""
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))
        add_alarm(session_factory, seed)
        add_alarm(session_factory, seed, ack_after_seconds=10)

        response = summary(client, head)["response"]
        assert response["never_acknowledged"] == 1


class TestHealth:
    def test_watchdog_events_and_current_states_are_reported(
        self, client, session_factory, clean_db
    ):
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))

        async def make_silent():
            async with session_factory() as session:
                session.add(
                    models.Event(
                        id=uuid4(),
                        tenant_id=seed["tenant_id"],
                        site_id=seed["site_id"],
                        device_id=seed["device_id"],
                        event_type="device_silent",
                        occurred_at=datetime.now(UTC) - timedelta(minutes=5),
                        dedup_key="device_silent:x",
                        severity="CRITICAL",
                        risk_score=90,
                        status="ALERTED",
                    )
                )
                session.add(
                    models.DeviceSilenceState(
                        device_id=seed["device_id"],
                        tenant_id=seed["tenant_id"],
                        last_message_at=datetime.now(UTC) - timedelta(hours=3),
                        state="SILENT",
                    )
                )
                await session.commit()

        asyncio.run(make_silent())

        health = summary(client, head)["health"]
        assert health["device_silent_events"] == 1
        assert health["devices_currently_silent"] == 1


class TestDaily:
    def test_days_come_back_oldest_first(self, client, session_factory, clean_db):
        seed = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, seed)["email"]))
        add_alarm(session_factory, seed, minutes_ago=60 * 24 * 2)
        add_alarm(session_factory, seed, minutes_ago=30)

        rows = client.get("/v1/analytics/daily", headers=head, params={"days": 7}).json()
        assert len(rows) == 2
        assert rows[0]["day"] < rows[1]["day"]


class TestAccess:
    def test_a_guard_cannot_read_the_pilot_report(self, client, session_factory, clean_db):
        """Analytics is a management view; a guard sees alarms, not aggregates."""
        head = auth(login(client, seed_account(session_factory, role="GUARD")["email"]))
        assert client.get("/v1/analytics/summary", headers=head).status_code == 403

    def test_a_billing_admin_sees_volume_but_owns_no_site(
        self, client, session_factory, clean_db
    ):
        """Billing holds ANALYTICS_VIEW: they need volume for invoices, not cameras."""
        head = auth(login(client, seed_account(session_factory, role="BILLING_ADMIN")["email"]))
        response = client.get("/v1/analytics/summary", headers=head)
        assert response.status_code == 200
        assert response.json()["volume"]["events"] == 0

    def test_another_tenants_numbers_never_leak(self, client, session_factory, clean_db):
        mine = seed_site(session_factory)
        theirs = seed_site(session_factory)
        head = auth(login(client, attach_user(session_factory, mine)["email"]))
        add_alarm(session_factory, theirs)
        add_alarm(session_factory, theirs)

        assert summary(client, head)["volume"]["events"] == 0
