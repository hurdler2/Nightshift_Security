"""Dedup, lifecycle, escalation, suppression, watchdog and push payloads.

These are the parts that decide whether a human is woken up, so the tests lean on the
failure modes rather than the happy path: duplicate storms, an acknowledgement racing
a timer, an outage that would mass-suppress a theft, and a payload that could leak a
stream URL onto a lock screen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.alarms.escalation import (
    CRITICAL_POLICY,
    DEFAULT_POLICY,
    EscalationPolicy,
    EscalationState,
    EscalationStep,
    policy_for,
)
from app.modules.alarms.suppression import (
    AlarmLevel,
    OutageState,
    level_for,
    should_suppress,
)
from app.modules.devices.watchdog import (
    SilenceState,
    WatchdogConfig,
    evaluate_silence,
)
from app.modules.events.dedup import DedupWindow
from app.modules.events.lifecycle import (
    EventLifecycle,
    EventStatus,
    IllegalTransition,
    ResolutionCode,
)
from app.modules.notifications.payload import UnsafePayload, build_push
from app.modules.rules.risk import Severity

T0 = datetime(2026, 9, 14, 23, 14, 0, tzinfo=UTC)
IST = "Europe/Istanbul"


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


class TestDedup:
    def test_first_event_opens_a_window(self):
        window = DedupWindow()
        decision = window.check("k", T0)
        assert decision.should_alert
        assert decision.occurrence == 1

    def test_repeats_inside_the_window_are_folded(self):
        """One person loitering for a minute is one alarm, not twelve."""
        window = DedupWindow(window_seconds=60)
        assert window.check("k", T0).should_alert
        for seconds in (5, 10, 30, 59):
            assert not window.check("k", at(seconds)).should_alert
        assert window.peek("k").count == 5

    def test_the_count_is_kept_not_thrown_away(self):
        """ "12 sightings in 4 minutes" is the signal that someone is still there."""
        window = DedupWindow(window_seconds=60)
        window.check("k", T0)
        last = window.check("k", at(30))
        assert last.total_in_window == 2
        assert last.window_started_at == T0

    def test_a_new_window_opens_after_expiry(self):
        window = DedupWindow(window_seconds=60)
        window.check("k", T0)
        assert window.check("k", at(60)).should_alert

    def test_different_keys_are_independent(self):
        window = DedupWindow()
        assert window.check("cam-1", T0).should_alert
        assert window.check("cam-2", T0).should_alert

    def test_per_call_window_override(self):
        window = DedupWindow(window_seconds=60)
        window.check("k", T0)
        assert window.check("k", at(20), window_seconds=10).should_alert

    def test_purge_drops_closed_windows(self):
        window = DedupWindow()
        window.check("k", T0)
        assert window.purge(at(3600)) == 1
        assert len(window) == 0


class TestLifecycle:
    def test_normal_path(self):
        life = EventLifecycle()
        life.transition(EventStatus.VERIFYING, at(1))
        life.transition(EventStatus.VERIFIED, at(2))
        life.transition(EventStatus.ALERTED, at(3))
        life.transition(EventStatus.ACKNOWLEDGED, at(20))
        life.transition(EventStatus.RESOLVED, at(300))
        assert life.is_terminal
        assert len(life.history) == 5

    def test_ai_down_path_skips_verification(self):
        """Spec §11.3 lets an event go straight to ALERTED on the vendor event."""
        life = EventLifecycle()
        life.transition(EventStatus.ALERTED, at(1))
        assert life.status is EventStatus.ALERTED

    def test_illegal_transition_is_refused(self):
        life = EventLifecycle()
        life.transition(EventStatus.ALERTED, at(1))
        life.transition(EventStatus.RESOLVED, at(2))
        with pytest.raises(IllegalTransition):
            life.transition(EventStatus.NEW, at(3))

    def test_terminal_states_are_final(self):
        life = EventLifecycle()
        life.transition(EventStatus.FALSE_POSITIVE, at(1))
        with pytest.raises(IllegalTransition):
            life.transition(EventStatus.ALERTED, at(2))

    def test_acknowledgement_latency(self):
        life = EventLifecycle()
        life.transition(EventStatus.ALERTED, T0)
        life.transition(EventStatus.ACKNOWLEDGED, at(42), actor_id=uuid4())
        assert life.acknowledgement_latency_seconds == 42

    def test_latency_is_none_before_acknowledgement(self):
        life = EventLifecycle()
        life.transition(EventStatus.ALERTED, T0)
        assert life.acknowledgement_latency_seconds is None

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (ResolutionCode.TRUE_SECURITY_INCIDENT, True),
            (ResolutionCode.SUSPICIOUS_ACTIVITY, True),
            (ResolutionCode.ANIMAL, False),
            (ResolutionCode.FALSE_POSITIVE, False),
        ],
    )
    def test_resolution_codes_feed_the_tuning_set(self, code, expected):
        assert code.is_true_positive is expected


class TestEscalation:
    def test_steps_become_due_over_time(self):
        state = EscalationState(opened_at=T0)
        assert [s.role for s in state.due_steps(T0)] == ["GUARD"]
        for step in state.due_steps(T0):
            state.mark_delivered(step)
        assert state.due_steps(at(30)) == []
        assert [s.role for s in state.due_steps(at(60))] == ["SITE_MANAGER"]

    def test_acknowledgement_stops_the_chain(self):
        state = EscalationState(opened_at=T0)
        state.acknowledge(at(20))
        assert state.due_steps(at(9999)) == []
        assert state.next_due_at(at(20)) is None

    def test_acknowledgement_wins_a_race_with_an_overdue_step(self):
        """The owner must not be paged for an alarm the guard already handled."""
        state = EscalationState(opened_at=T0)
        state.acknowledge(at(200))
        assert state.due_steps(at(200)) == []

    def test_second_acknowledgement_is_a_no_op(self):
        state = EscalationState(opened_at=T0)
        assert state.acknowledge(at(10)) is True
        assert state.acknowledge(at(20)) is False
        assert state.acknowledged_at == at(10)

    def test_resolution_also_stops_it(self):
        state = EscalationState(opened_at=T0)
        state.resolve(at(30))
        assert state.due_steps(at(9999)) == []

    def test_delivered_steps_are_not_repeated(self):
        state = EscalationState(opened_at=T0)
        for step in state.due_steps(at(300)):
            state.mark_delivered(step)
        assert state.due_steps(at(600)) == []
        assert state.is_exhausted

    def test_next_due_at_drives_the_scheduler(self):
        state = EscalationState(opened_at=T0)
        for step in state.due_steps(T0):
            state.mark_delivered(step)
        assert state.next_due_at(T0) == at(60)

    def test_critical_severity_uses_the_faster_chain(self):
        assert policy_for(Severity.CRITICAL) is CRITICAL_POLICY
        assert policy_for(Severity.HIGH) is DEFAULT_POLICY
        assert CRITICAL_POLICY.steps[1].after_seconds < DEFAULT_POLICY.steps[1].after_seconds

    def test_policies_must_be_ordered(self):
        with pytest.raises(ValueError, match="ordered"):
            EscalationPolicy(
                name="bad",
                steps=(
                    EscalationStep(after_seconds=60, role="A"),
                    EscalationStep(after_seconds=10, role="B"),
                ),
            )

    def test_latency_is_recorded(self):
        state = EscalationState(opened_at=T0)
        state.acknowledge(at(45))
        assert state.acknowledgement_latency_seconds == 45


class TestSuppression:
    def test_site_outage_swallows_channel_faults(self):
        """One site alarm, not seventy camera alarms (spec §13)."""
        outages = OutageState(offline_sites={"site-a"})
        decision = should_suppress(
            event_type="video_loss", site_id="site-a", device_id="dvr-1", outages=outages
        )
        assert decision.suppressed
        assert decision.covered_by is AlarmLevel.SITE

    def test_silent_device_swallows_its_own_channel_faults(self):
        outages = OutageState(silent_devices={"dvr-1"})
        assert should_suppress(
            event_type="video_loss", site_id="site-a", device_id="dvr-1", outages=outages
        ).suppressed

    def test_other_devices_are_unaffected(self):
        outages = OutageState(silent_devices={"dvr-1"})
        assert not should_suppress(
            event_type="video_loss", site_id="site-a", device_id="dvr-2", outages=outages
        ).suppressed

    @pytest.mark.parametrize(
        "event_type",
        ["person_detected", "vehicle_detected", "zone_intrusion", "video_tampering"],
    )
    def test_security_events_are_never_suppressed(self, event_type):
        """Suppression exists to cut noise; it must never hide a break-in (spec §13.1)."""
        outages = OutageState(offline_sites={"site-a"}, silent_devices={"dvr-1"})
        decision = should_suppress(
            event_type=event_type, site_id="site-a", device_id="dvr-1", outages=outages
        )
        assert not decision.suppressed

    def test_the_site_alarm_itself_is_not_suppressed(self):
        outages = OutageState(offline_sites={"site-a"})
        assert not should_suppress(
            event_type="site_offline", site_id="site-a", device_id="dvr-1", outages=outages
        ).suppressed

    def test_nothing_is_suppressed_without_an_outage(self):
        assert not should_suppress(
            event_type="video_loss",
            site_id="site-a",
            device_id="dvr-1",
            outages=OutageState(),
        ).suppressed

    def test_levels(self):
        assert level_for("site_offline") is AlarmLevel.SITE
        assert level_for("storage_failure") is AlarmLevel.DEVICE
        assert level_for("video_loss") is AlarmLevel.CHANNEL
        assert level_for("something_new") is AlarmLevel.CHANNEL


class TestWatchdog:
    CONFIG = WatchdogConfig(expected_interval_seconds=1800)  # 30 minutes

    def test_recent_message_is_ok(self):
        verdict = evaluate_silence(
            last_message_at=at(-600), now=T0, config=self.CONFIG, site_timezone=IST
        )
        assert verdict.state is SilenceState.OK
        assert not verdict.should_alarm

    def test_one_missed_message_is_only_late(self):
        verdict = evaluate_silence(
            last_message_at=at(-2000), now=T0, config=self.CONFIG, site_timezone=IST
        )
        assert verdict.state is SilenceState.LATE
        assert not verdict.should_alarm

    def test_beyond_grace_is_silent(self):
        verdict = evaluate_silence(
            last_message_at=at(-4000), now=T0, config=self.CONFIG, site_timezone=IST
        )
        assert verdict.state is SilenceState.SILENT
        assert verdict.should_alarm

    def test_silence_at_night_is_a_security_event(self):
        """T0 is 02:14 local - a recorder going quiet then may be being stolen."""
        verdict = evaluate_silence(
            last_message_at=at(-4000), now=T0, config=self.CONFIG, site_timezone=IST
        )
        assert verdict.is_security_relevant
        assert "theft" in verdict.reason

    def test_silence_during_the_day_is_a_fault(self):
        noon = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)  # 12:00 local
        verdict = evaluate_silence(
            last_message_at=noon - timedelta(seconds=4000),
            now=noon,
            config=self.CONFIG,
            site_timezone=IST,
        )
        assert verdict.state is SilenceState.SILENT
        assert not verdict.is_security_relevant
        assert "fault" in verdict.reason

    def test_never_seen(self):
        verdict = evaluate_silence(last_message_at=None, now=T0, config=self.CONFIG)
        assert verdict.state is SilenceState.NEVER_SEEN
        assert verdict.should_alarm

    def test_six_hour_interval_is_far_too_slow_to_catch_a_theft(self):
        """Documents why spec §13.1 tightened the interval to 30-60 minutes."""
        slow = WatchdogConfig(expected_interval_seconds=6 * 3600)
        stolen_two_hours_ago = evaluate_silence(
            last_message_at=at(-7200), now=T0, config=slow, site_timezone=IST
        )
        assert stolen_two_hours_ago.state is SilenceState.OK  # still "healthy"

        fast = evaluate_silence(
            last_message_at=at(-7200), now=T0, config=self.CONFIG, site_timezone=IST
        )
        assert fast.state is SilenceState.SILENT


class TestPushPayload:
    def build(self, **overrides):
        defaults = {
            "event_id": uuid4(),
            "alarm_id": uuid4(),
            "severity": Severity.HIGH,
            "site_name": "Beton Tesisi",
            "camera_name": "Depo Arka",
            "event_type": "person_detected",
            "occurred_at": T0,
            "risk_score": 75,
            "reasons": ["Yasak bölge", "Mesai dışı saat"],
            "has_snapshot": True,
        }
        defaults.update(overrides)
        return build_push(**defaults)

    def test_shape(self):
        payload = self.build().to_dict()
        assert payload["type"] == "security_alert"
        assert payload["severity"] == "HIGH"
        assert payload["title"] == "İnsan algılandı"
        assert "Beton Tesisi" in payload["body"]
        assert payload["risk_score"] == 75

    def test_critical_is_marked_in_the_title(self):
        assert self.build(severity=Severity.CRITICAL).title.startswith("KRİTİK")

    def test_deep_link_targets_the_alarm(self):
        push = self.build()
        assert push.deep_link == f"nightshift://alarms/{push.alarm_id}"

    def test_ai_unavailable_is_stated_not_hidden(self):
        """Spec §11.3 wording: do not imply a verification that did not happen."""
        assert "AI doğrulama" in self.build(ai_unavailable=True).body

    def test_no_credentials_or_stream_urls_can_be_sent(self):
        push = self.build(camera_name="rtsp://user:pass@10.0.0.5/cam")
        with pytest.raises(UnsafePayload):
            push.to_dict()

    def test_password_text_is_refused(self):
        push = self.build(site_name="site password=hunter2")
        with pytest.raises(UnsafePayload):
            push.to_dict()

    def test_unknown_event_type_still_gets_a_title(self):
        assert self.build(event_type="something_new").title == "Alarm"
