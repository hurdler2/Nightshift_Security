"""Zones, risk scoring and rule evaluation (spec §10, §11.3)."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from app.modules.rules.engine import (
    Action,
    AiFallback,
    Decision,
    EvaluationContext,
    Rule,
    evaluate,
    evaluate_all,
)
from app.modules.rules.risk import (
    DEFAULT_WEIGHTS,
    RiskFactor,
    Severity,
    compute_risk,
    severity_for,
)
from app.modules.rules.schedule import AFTER_HOURS, ALWAYS, Schedule
from app.modules.rules.zones import (
    InvalidZone,
    Zone,
    ZoneType,
    ground_anchor,
    match_any,
    match_zones,
)

IST = "Europe/Istanbul"
NIGHT = datetime(2026, 9, 14, 23, 14, tzinfo=UTC)  # 02:14 local Tuesday
NOON = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)  # 12:00 local Monday

RESTRICTED = Zone(
    name="Depo Yasak",
    type=ZoneType.RESTRICTED,
    points=[(0.5, 0.2), (0.95, 0.2), (0.95, 0.9), (0.5, 0.9)],
)
ROADSIDE = Zone(
    name="Yol", type=ZoneType.IGNORE, points=[(0.0, 0.0), (0.3, 0.0), (0.3, 1.0), (0.0, 1.0)]
)

#: A person standing inside the restricted polygon (ground point ~0.7, 0.8).
BOX_IN_RESTRICTED = (0.6, 0.4, 0.8, 0.8)
#: A person on the road, which we deliberately ignore.
BOX_ON_ROAD = (0.05, 0.4, 0.2, 0.8)
#: A person in open yard, in no zone at all.
BOX_NEUTRAL = (0.35, 0.4, 0.45, 0.8)


class TestZones:
    def test_ground_anchor_is_bottom_centre(self):
        """Head position would put a person in a zone too early at every camera angle."""
        assert ground_anchor((0.2, 0.2, 0.4, 0.6)) == pytest.approx((0.3, 0.6))

    def test_detection_inside_restricted(self):
        match = match_zones(BOX_IN_RESTRICTED, [RESTRICTED, ROADSIDE])
        assert match.restricted
        assert match.names == ["Depo Yasak"]

    def test_detection_in_ignore_zone(self):
        match = match_zones(BOX_ON_ROAD, [RESTRICTED, ROADSIDE])
        assert match.is_ignored
        assert not match.is_elevated

    def test_no_zone_hit(self):
        assert match_zones(BOX_NEUTRAL, [RESTRICTED, ROADSIDE]).zones == []

    def test_ignore_only_wins_when_everything_is_ignored(self):
        """One person on the road does not excuse another inside the compound."""
        both = match_any([BOX_ON_ROAD, BOX_IN_RESTRICTED], [RESTRICTED, ROADSIDE])
        assert not both.is_ignored
        assert both.restricted

        only_road = match_any([BOX_ON_ROAD], [RESTRICTED, ROADSIDE])
        assert only_road.is_ignored

    def test_rejects_pixel_coordinates(self):
        with pytest.raises(InvalidZone, match="normalized"):
            Zone(name="bad", type=ZoneType.NORMAL, points=[(0, 0), (1920, 0), (1920, 1080)])

    def test_rejects_degenerate_polygon(self):
        with pytest.raises(InvalidZone, match="at least 3"):
            Zone(name="bad", type=ZoneType.NORMAL, points=[(0.1, 0.1), (0.2, 0.2)])


class TestRisk:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0, Severity.INFO),
            (29, Severity.INFO),
            (30, Severity.LOW),
            (49, Severity.LOW),
            (50, Severity.MEDIUM),
            (69, Severity.MEDIUM),
            (70, Severity.HIGH),
            (84, Severity.HIGH),
            (85, Severity.CRITICAL),
            (100, Severity.CRITICAL),
        ],
    )
    def test_severity_bands(self, score, expected):
        assert severity_for(score) is expected

    def test_factors_add_up(self):
        risk = compute_risk(
            [
                RiskFactor.PERSON_DETECTED,
                RiskFactor.OUTSIDE_BUSINESS_HOURS,
                RiskFactor.RESTRICTED_ZONE,
            ]
        )
        assert risk.score == 65
        assert risk.severity is Severity.MEDIUM

    def test_duplicate_factors_count_once(self):
        """Two people in one restricted zone is one restricted-zone fact."""
        once = compute_risk([RiskFactor.RESTRICTED_ZONE])
        twice = compute_risk([RiskFactor.RESTRICTED_ZONE, RiskFactor.RESTRICTED_ZONE])
        assert once.score == twice.score

    def test_score_is_clamped(self):
        everything = list(DEFAULT_WEIGHTS)
        assert 0 <= compute_risk(everything).score <= 100

    def test_low_confidence_subtracts(self):
        with_low = compute_risk([RiskFactor.PERSON_DETECTED, RiskFactor.LOW_AI_CONFIDENCE])
        assert with_low.score == 0  # 10 - 15, clamped

    def test_reasons_are_human_readable_and_ordered(self):
        risk = compute_risk([RiskFactor.PERSON_DETECTED, RiskFactor.RESTRICTED_ZONE])
        assert risk.labels[0] == "Yasak bölge"  # biggest contributor first
        assert "İnsan algılandı" in risk.labels

    def test_custom_weights_override(self):
        risk = compute_risk([RiskFactor.PERSON_DETECTED], weights={RiskFactor.PERSON_DETECTED: 90})
        assert risk.score == 90


NIGHT_RULE = Rule(
    name="Gece insan alarmı",
    event_types=frozenset({"person_detected"}),
    schedule=AFTER_HOURS,
    min_ai_confidence=0.8,
)


def context(**overrides) -> EvaluationContext:
    defaults = {
        "event_type": "person_detected",
        "occurred_at": NIGHT,
        "site_timezone": IST,
        "camera_id": "cam-1",
        "zones": [RESTRICTED, ROADSIDE],
        "detection_boxes": [BOX_IN_RESTRICTED],
        "ai_confidence": 0.93,
    }
    defaults.update(overrides)
    return EvaluationContext(**defaults)


class TestRuleEvaluation:
    def test_the_spec_scenario_fires(self):
        """§20 PHASE 6 exit criterion: night + restricted zone + human = alarm.

        Lands at 65/MEDIUM on the spec's default weights (person 10 + after hours 25
        + restricted zone 30). Whether the flagship scenario should read HIGH is a
        calibration decision for pilot data, not something to hard-code here.
        """
        decision = evaluate(NIGHT_RULE, context())
        assert decision.matched
        assert decision.risk.score == 65
        assert decision.severity is Severity.MEDIUM
        assert Action.PUSH in decision.actions
        assert "Yasak bölge" in decision.reasons

    def test_compounding_evidence_reaches_high(self):
        """A repeat sighting on top of the base scenario crosses into HIGH."""
        decision = evaluate(NIGHT_RULE, context(repeated=True))
        assert decision.risk.score == 75
        assert decision.severity is Severity.HIGH

    def test_same_event_in_daytime_does_not(self):
        assert not evaluate(NIGHT_RULE, context(occurred_at=NOON)).matched

    def test_neutral_area_at_night_is_lower_severity(self):
        decision = evaluate(NIGHT_RULE, context(detection_boxes=[BOX_NEUTRAL]))
        assert decision.matched
        assert decision.severity is Severity.LOW

    def test_ignore_zone_blocks_the_alarm(self):
        decision = evaluate(NIGHT_RULE, context(detection_boxes=[BOX_ON_ROAD]))
        assert not decision.matched
        assert "IGNORE" in decision.reasons[0]

    def test_wrong_event_type(self):
        assert not evaluate(NIGHT_RULE, context(event_type="video_loss")).matched

    def test_disabled_rule(self):
        from dataclasses import replace

        assert not evaluate(replace(NIGHT_RULE, enabled=False), context()).matched

    def test_camera_filter(self):
        from dataclasses import replace

        scoped = replace(NIGHT_RULE, camera_ids=frozenset({"cam-9"}))
        assert not evaluate(scoped, context()).matched

    def test_zone_filter(self):
        from dataclasses import replace

        scoped = replace(NIGHT_RULE, zone_names=frozenset({"Depo Yasak"}))
        assert evaluate(scoped, context()).matched
        assert not evaluate(scoped, context(detection_boxes=[BOX_NEUTRAL])).matched

    def test_low_confidence_is_filtered_out(self):
        decision = evaluate(NIGHT_RULE, context(ai_confidence=0.4))
        assert not decision.matched
        assert "confidence" in decision.reasons[0]

    def test_critical_adds_escalation(self):
        decision = evaluate(
            NIGHT_RULE,
            context(recent_tamper=True, repeated=True, device_silent=True),
        )
        assert decision.severity is Severity.CRITICAL
        assert Action.ESCALATE in decision.actions


class TestAiFallback:
    def test_default_alerts_on_the_vendor_event(self):
        """Spec §11.3: losing our classifier must not lose the break-in."""
        decision = evaluate(NIGHT_RULE, context(ai_available=False, ai_confidence=None))
        assert decision.matched
        assert any("AI unavailable" in r for r in decision.reasons)

    def test_queue_only_holds_it_back(self):
        from dataclasses import replace

        rule = replace(NIGHT_RULE, if_ai_unavailable=AiFallback.QUEUE_ONLY)
        decision = evaluate(rule, context(ai_available=False, ai_confidence=None))
        assert not decision.matched
        assert Action.LOG_ONLY in decision.actions

    def test_drop_discards(self):
        from dataclasses import replace

        rule = replace(NIGHT_RULE, if_ai_unavailable=AiFallback.DROP)
        assert not evaluate(rule, context(ai_available=False)).matched

    def test_confidence_gate_is_skipped_when_ai_is_down(self):
        """A stale confidence must not silently filter an unverified event."""
        decision = evaluate(NIGHT_RULE, context(ai_available=False, ai_confidence=0.1))
        assert decision.matched


class TestMinSeverity:
    def test_below_threshold_is_logged_not_alarmed(self):
        from dataclasses import replace

        rule = replace(NIGHT_RULE, min_severity=Severity.CRITICAL)
        decision = evaluate(rule, context())
        assert not decision.matched
        assert Action.LOG_ONLY in decision.actions
        assert "severity below" in decision.reasons[-1]


class TestEvaluateAll:
    def test_loudest_rule_wins(self):
        broad = Rule(
            name="Her şey",
            event_types=frozenset({"person_detected"}),
            schedule=ALWAYS,
            zone_names=frozenset(),
        )
        decision = evaluate_all([broad, NIGHT_RULE], context())
        assert decision.matched
        assert decision.risk.score == max(
            evaluate(broad, context()).risk.score,
            evaluate(NIGHT_RULE, context()).risk.score,
        )

    def test_no_rules_configured(self):
        decision = evaluate_all([], context())
        assert not decision.matched
        assert "no rules" in decision.reasons[0]

    def test_explain_is_readable(self):
        text: str = evaluate_all([NIGHT_RULE], context()).explain()
        assert "ALARM" in text
        assert "Gece insan alarmı" in text


def test_decision_type_is_exported():
    assert Decision is not None
    assert Schedule(timezone=IST, start=time(0, 0), end=time(0, 0)).is_always_on
