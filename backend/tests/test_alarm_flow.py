"""End-to-end: a delivered alarm mail becomes (or does not become) a push.

This is the test that says the product works. Everything below runs the real ingest,
suppression, dedup, rule and notification code — only the recorder and the phone are
imaginary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.alarms.suppression import OutageState
from app.modules.events.dedup import DedupWindow
from app.modules.events.lifecycle import EventStatus
from app.modules.ingest.pipeline import DeviceIdentity, ingest
from app.modules.rules.engine import Action, AiFallback, Rule
from app.modules.rules.risk import Severity
from app.modules.rules.schedule import AFTER_HOURS, ALWAYS
from app.modules.rules.zones import Zone, ZoneType
from app.services.alarm_flow import (
    ALARMED,
    BLOCKED,
    DUPLICATE,
    NO_RULE_MATCH,
    SUPPRESSED,
    AiVerdict,
    SiteContext,
    process,
    summarize,
)
from tests.test_ingest import JPEG, build_mail

IST = "Europe/Istanbul"
NIGHT = datetime(2026, 9, 14, 23, 14, tzinfo=UTC)  # 02:14 local, Tuesday
NOON = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)  # 12:00 local, Monday

RESTRICTED = Zone(
    name="Depo Yasak",
    type=ZoneType.RESTRICTED,
    points=[(0.5, 0.2), (0.95, 0.2), (0.95, 0.9), (0.5, 0.9)],
)
BOX_IN_RESTRICTED = [(0.6, 0.4, 0.8, 0.8)]

HUMAN_MAIL = """\
Alarm Event: Smart Motion Human
Alarm Input Channel No.: 3
Channel Name: Depo Arka
Alarm Device Name: SANTIYE-A-XVR
Alarm Start Time(D/M/Y H:M:S): 15/09/2026 02:14:32
"""

NIGHT_RULE = Rule(
    name="Gece insan alarmı",
    event_types=frozenset({"person_detected"}),
    schedule=AFTER_HOURS,
    min_ai_confidence=0.8,
)

DEVICE = DeviceIdentity(
    device_id=uuid4(), tenant_id=uuid4(), site_id=uuid4(), smtp_username="dev-site-a"
)


def site(**overrides) -> SiteContext:
    defaults = {
        "site_name": "Beton Tesisi",
        "site_timezone": IST,
        "rules": [NIGHT_RULE],
        "cameras_by_channel": {3: ("cam-3", "Depo Arka")},
        "zones_by_camera": {"cam-3": [RESTRICTED]},
    }
    defaults.update(overrides)
    return SiteContext(**defaults)


def run(
    body: str = HUMAN_MAIL,
    *,
    context: SiteContext | None = None,
    outages: OutageState | None = None,
    dedup: DedupWindow | None = None,
    ai: AiVerdict | None = None,
    now: datetime = NIGHT,
    attach: bytes | None = JPEG,
):
    result = ingest(build_mail(body, attach=attach), DEVICE, received_at=now)
    return process(
        result,
        context or site(),
        outages=outages if outages is not None else OutageState(),
        dedup=dedup if dedup is not None else DedupWindow(),
        ai=ai or AiVerdict(confidence=0.94, boxes=BOX_IN_RESTRICTED),
        now=now,
    )


class TestHappyPath:
    def test_night_intruder_produces_a_push(self):
        """The whole product, in one assertion block."""
        outcome = run()
        assert outcome.stage == ALARMED
        assert outcome.event_status is EventStatus.ALERTED
        assert outcome.severity is Severity.MEDIUM
        assert outcome.camera_name == "Depo Arka"

        push = outcome.push.to_dict()
        assert push["title"] == "İnsan algılandı"
        assert push["site_name"] == "Beton Tesisi"
        assert push["camera_name"] == "Depo Arka"
        assert push["has_snapshot"] is True
        assert "Yasak bölge" in push["reasons"]

    def test_deep_link_points_at_the_alarm(self):
        outcome = run()
        assert outcome.push.deep_link == f"nightshift://alarms/{outcome.alarm_id}"

    def test_escalation_starts_with_the_guard(self):
        outcome = run()
        assert [s.role for s in outcome.escalation.due_steps(NIGHT)] == ["GUARD"]

    def test_the_snapshot_survives_to_the_alarm(self):
        assert run().push.has_snapshot

    def test_missing_snapshot_still_alarms(self):
        """A lost attachment must not cost the alarm."""
        outcome = run(attach=None)
        assert outcome.stage == ALARMED
        assert outcome.push.has_snapshot is False


class TestNotAlarming:
    def test_daytime_does_not_alarm(self):
        outcome = run(now=NOON)
        assert outcome.stage == NO_RULE_MATCH
        assert outcome.persist_event  # it still lands in the timeline

    def test_face_mail_stops_at_the_door(self):
        outcome = run("Alarm Event: Face Detection\nAlarm Input Channel No.: 3\n")
        assert outcome.stage == BLOCKED
        assert outcome.push is None
        assert not outcome.persist_event

    def test_low_confidence_is_filtered(self):
        outcome = run(ai=AiVerdict(confidence=0.3, boxes=BOX_IN_RESTRICTED))
        assert outcome.stage == NO_RULE_MATCH

    def test_events_are_persisted_even_when_no_alarm_fires(self):
        for outcome in (run(now=NOON), run(ai=AiVerdict(confidence=0.1))):
            assert outcome.persist_event


class TestDedup:
    def test_a_burst_produces_one_alarm(self):
        """Someone loitering for a minute wakes one person, once."""
        window = DedupWindow(window_seconds=60)
        first = run(dedup=window, now=NIGHT)
        rest = [run(dedup=window, now=NIGHT + timedelta(seconds=s)) for s in (5, 20, 45)]

        assert first.stage == ALARMED
        assert all(o.stage == DUPLICATE for o in rest)
        assert rest[-1].occurrence_count == 4
        assert all(o.persist_event for o in rest)  # the count is kept

    def test_a_new_window_alarms_again(self):
        window = DedupWindow(window_seconds=60)
        run(dedup=window, now=NIGHT)
        later = run(dedup=window, now=NIGHT + timedelta(seconds=120))
        assert later.stage == ALARMED


class TestSuppression:
    def test_site_outage_does_not_hide_an_intruder(self):
        """The theft scenario: the link is down and someone is on site (spec §13.1)."""
        outages = OutageState(offline_sites={str(DEVICE.site_id)})
        assert run(outages=outages).stage == ALARMED

    def test_site_outage_does_suppress_camera_faults(self):
        outages = OutageState(offline_sites={str(DEVICE.site_id)})
        outcome = run(
            "Alarm Event: Video Loss\nAlarm Input Channel No.: 3\n",
            context=site(
                rules=[Rule(name="Arıza", event_types=frozenset({"video_loss"}), schedule=ALWAYS)]
            ),
            outages=outages,
        )
        assert outcome.stage == SUPPRESSED
        assert outcome.persist_event


class TestAiFallback:
    def test_ai_down_still_alarms_and_says_so(self):
        """Spec §11.3: losing our classifier must not lose the break-in."""
        outcome = run(ai=AiVerdict.unavailable())
        assert outcome.stage == ALARMED
        assert "AI doğrulama" in outcome.push.body

    def test_a_drop_rule_stays_silent(self):
        from dataclasses import replace

        rule = replace(NIGHT_RULE, if_ai_unavailable=AiFallback.DROP)
        outcome = run(context=site(rules=[rule]), ai=AiVerdict.unavailable())
        assert outcome.stage == NO_RULE_MATCH


class TestUnknownFormat:
    def test_unparseable_mail_still_reaches_the_timeline(self):
        outcome = run("wording from a firmware we have never seen")
        assert outcome.stage == NO_RULE_MATCH  # unknown_alarm matches no rule
        assert outcome.persist_event

    def test_a_catch_all_rule_turns_it_into_an_alarm(self):
        """How an operator can stay covered while a profile is still unverified."""
        # min_severity must be INFO: an unknown alarm scores only the after-hours
        # factor, so the default LOW threshold would swallow it.
        catch_all = Rule(
            name="Tanınmayan alarm",
            event_types=frozenset({"unknown_alarm"}),
            schedule=AFTER_HOURS,
            min_severity=Severity.INFO,
        )
        outcome = run("wording we do not know", context=site(rules=[catch_all]))
        assert outcome.stage == ALARMED
        assert outcome.push.title == "Alarm"

    def test_unmapped_channel_still_alarms(self):
        """A channel nobody registered is exactly when someone is on site."""
        outcome = run(context=site(cameras_by_channel={}))
        assert outcome.stage == ALARMED
        assert outcome.push.camera_name == "Kanal 3"


class TestEscalationSeverity:
    def test_critical_alarms_escalate_faster(self):
        outcome = run(context=site(device_silent=True, recent_tamper_cameras={"cam-3"}))
        assert outcome.severity is Severity.CRITICAL
        assert Action.ESCALATE in outcome.decision.actions
        assert outcome.escalation.policy.name == "critical"

    def test_acknowledgement_stops_the_chain(self):
        outcome = run()
        outcome.escalation.acknowledge(NIGHT + timedelta(seconds=25))
        assert outcome.escalation.due_steps(NIGHT + timedelta(hours=1)) == []


def test_summary_line_is_readable():
    text = summarize(run())
    assert text.startswith("ALARMED")
    assert "Depo Arka" in text


@pytest.mark.parametrize("stage_run", [lambda: run(), lambda: run(now=NOON)])
def test_no_credentials_ever_reach_a_payload(stage_run):
    outcome = stage_run()
    rendered = str(outcome.push.to_dict() if outcome.push else outcome.reason)
    assert "rtsp://" not in rendered
    assert "password" not in rendered.lower()
