"""What silence means, and how loudly to say it.

No database and no clock here: `plan_sweep` is a pure function over the fleet's
last-seen times, which is exactly what makes the cases that matter — everything dark,
one thing dark, something coming back — cheap to state and impossible to get wrong by
accident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.modules.devices.watchdog import SilenceState
from app.services.silence_sweep import DeviceSnapshot, plan_sweep

NOW = datetime(2026, 9, 15, 2, 30, tzinfo=UTC)  # 05:30 Istanbul, still armed
NOON = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)  # 12:00 Istanbul
SITE = uuid4()
TENANT = uuid4()


def device(
    *,
    minutes_quiet: float | None,
    name: str = "XVR",
    site_id=SITE,
    stored: str = SilenceState.OK.value,
) -> DeviceSnapshot:
    return DeviceSnapshot(
        device_id=uuid4(),
        site_id=site_id,
        tenant_id=TENANT,
        name=name,
        last_message_at=(
            None if minutes_quiet is None else NOW - timedelta(minutes=minutes_quiet)
        ),
        stored_state=stored,
        # 30 minutes expected: LATE past 30, SILENT past 45 (1.5x tolerance).
        expected_interval_seconds=1800,
    )


class TestQuietFleet:
    def test_a_reporting_fleet_produces_nothing(self):
        result = plan_sweep([device(minutes_quiet=5), device(minutes_quiet=10)], now=NOW)
        assert result.quiet

    def test_a_late_recorder_is_not_alarmed_on(self):
        """One missed message is a lost mail, not a theft."""
        result = plan_sweep([device(minutes_quiet=35), device(minutes_quiet=2)], now=NOW)
        assert result.alarms == []
        assert set(result.state_changes.values()) == {SilenceState.LATE.value}


class TestOneDarkRecorder:
    def test_a_single_silent_recorder_raises_its_own_alarm(self):
        dark = device(minutes_quiet=90, name="SANTIYE-A-XVR")
        result = plan_sweep([dark, device(minutes_quiet=3), device(minutes_quiet=4)], now=NOW)

        assert len(result.alarms) == 1
        alarm = result.alarms[0]
        assert alarm.event_type == "device_silent"
        assert alarm.device_id == dark.device_id
        assert alarm.is_security_relevant is True  # night, and the others are fine
        assert "stolen or unplugged" in alarm.reason

    def test_the_alarm_is_raised_once_not_every_sweep(self):
        """Otherwise people learn to ignore it, which is worse than not raising it."""
        already = device(minutes_quiet=300, stored=SilenceState.SILENT.value)
        result = plan_sweep([already, device(minutes_quiet=3)], now=NOW)
        assert result.alarms == []
        assert result.state_changes == {}

    def test_daytime_silence_is_still_reported(self):
        """Quieter, but never swallowed: a recorder taken at noon is still taken."""
        result = plan_sweep(
            [device(minutes_quiet=90), device(minutes_quiet=2)],
            now=NOON,
        )
        assert len(result.alarms) == 1
        assert result.alarms[0].is_security_relevant is False


class TestSiteOutage:
    def test_a_whole_site_going_dark_is_one_alarm(self):
        """Fourteen recorders on one breaker must not mean fourteen notifications."""
        fleet = [device(minutes_quiet=90, name=f"XVR-{i}") for i in range(14)]
        result = plan_sweep(fleet, now=NOW)

        assert len(result.alarms) == 1
        alarm = result.alarms[0]
        assert alarm.event_type == "site_offline"
        assert alarm.device_id is None
        assert len(alarm.device_ids) == 14

    def test_a_lone_recorder_on_a_site_is_a_device_alarm_not_an_outage(self):
        """One box is the whole site: "they all went quiet" would be misleading."""
        result = plan_sweep([device(minutes_quiet=90, name="TEK-XVR")], now=NOW)
        assert [a.event_type for a in result.alarms] == ["device_silent"]

    def test_a_partial_outage_names_the_recorders(self):
        """Below the threshold each dark box is individually suspicious."""
        fleet = [device(minutes_quiet=90, name=f"DARK-{i}") for i in range(2)]
        fleet += [device(minutes_quiet=2, name=f"LIVE-{i}") for i in range(6)]
        result = plan_sweep(fleet, now=NOW)

        assert [a.event_type for a in result.alarms] == ["device_silent", "device_silent"]

    def test_sites_are_judged_independently(self):
        other = uuid4()
        fleet = [device(minutes_quiet=90, site_id=SITE), device(minutes_quiet=90, site_id=SITE)]
        fleet += [device(minutes_quiet=3, site_id=other), device(minutes_quiet=3, site_id=other)]
        result = plan_sweep(fleet, now=NOW)

        assert len(result.alarms) == 1
        assert result.alarms[0].site_id == SITE


class TestRecovery:
    def test_a_recorder_coming_back_is_recorded_without_an_alarm(self):
        back = device(minutes_quiet=1, stored=SilenceState.SILENT.value)
        result = plan_sweep([back], now=NOW)

        assert result.alarms == []
        assert result.recovered == [back.device_id]
        assert result.state_changes[back.device_id] == SilenceState.OK.value

    def test_a_recorder_that_never_reported_is_not_treated_as_a_theft(self):
        """Commissioned but never heard from is an installation problem."""
        fresh = device(minutes_quiet=None, stored=SilenceState.NEVER_SEEN.value)
        result = plan_sweep([fresh, device(minutes_quiet=2)], now=NOW)
        assert result.alarms == []
