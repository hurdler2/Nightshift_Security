"""Rule evaluation: event + context -> decision (spec §10, §11.3).

The engine answers one question — *does this event become an alarm, and how loud?* —
and shows its work, so an operator can see why they were woken and an analyst can see
why they were not.

Deliberate asymmetry: when the AI is unavailable, the default is still to alert on the
vendor event (spec §11.3). Missing a break-in because our own classifier was down is
not an acceptable failure mode; an extra alarm is.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from app.modules.rules.risk import RiskFactor, RiskScore, Severity, compute_risk
from app.modules.rules.schedule import ALWAYS, Schedule, is_outside_business_hours
from app.modules.rules.zones import Zone, ZoneMatch, match_any


class Action(enum.StrEnum):
    PUSH = "push"
    SNAPSHOT = "snapshot"
    ESCALATE = "escalate"
    LOG_ONLY = "log_only"


class AiFallback(enum.StrEnum):
    """What to do when the AI verdict is missing (spec §11.3)."""

    ALERT_ON_VENDOR_EVENT = "ALERT_ON_VENDOR_EVENT"
    QUEUE_ONLY = "QUEUE_ONLY"
    DROP = "DROP"


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    event_types: frozenset[str]
    schedule: Schedule = ALWAYS
    #: Empty means every camera on the site.
    camera_ids: frozenset[str] = frozenset()
    #: Empty means the whole frame; otherwise the detection must land in one of these.
    zone_names: frozenset[str] = frozenset()
    min_ai_confidence: float = 0.0
    actions: frozenset[Action] = frozenset({Action.PUSH, Action.SNAPSHOT})
    if_ai_unavailable: AiFallback = AiFallback.ALERT_ON_VENDOR_EVENT
    #: Alarms below this never notify anyone; they stay in the timeline.
    min_severity: Severity = Severity.LOW
    enabled: bool = True
    weights: dict[RiskFactor, int] = field(default_factory=dict)


@dataclass(slots=True)
class EvaluationContext:
    """Everything the engine needs that is not part of the rule itself."""

    event_type: str
    occurred_at: datetime
    site_timezone: str
    camera_id: str = ""
    zones: list[Zone] = field(default_factory=list)
    #: Normalized boxes from the AI service; empty means no detection was returned.
    detection_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    ai_confidence: float | None = None
    ai_available: bool = True
    #: Set when the recorder reported tampering on this camera recently.
    recent_tamper: bool = False
    #: Set when this is not the first sighting in the current dedup window.
    repeated: bool = False
    #: Set when the device watchdog has this recorder marked silent (spec §13.3).
    device_silent: bool = False
    #: Vendor-side perimeter rule (tripwire/intrusion) rather than plain SMD.
    perimeter_event: bool = False


@dataclass(slots=True)
class Decision:
    matched: bool
    rule_name: str | None
    actions: frozenset[Action]
    risk: RiskScore
    zone_match: ZoneMatch
    reasons: list[str] = field(default_factory=list)

    @property
    def severity(self) -> Severity:
        return self.risk.severity

    @property
    def should_notify(self) -> bool:
        return self.matched and Action.PUSH in self.actions

    def explain(self) -> str:
        head = f"{self.rule_name or 'no rule'} -> {'ALARM' if self.matched else 'no alarm'}"
        return f"{head} ({self.risk.score}/{self.risk.severity.value}): " + "; ".join(self.reasons)


def _no_match(reason: str, zone_match: ZoneMatch | None = None) -> Decision:
    return Decision(
        matched=False,
        rule_name=None,
        actions=frozenset(),
        risk=compute_risk([]),
        zone_match=zone_match or ZoneMatch(),
        reasons=[reason],
    )


def evaluate(rule: Rule, context: EvaluationContext) -> Decision:
    """Apply one rule. Order matters: cheap disqualifiers first, scoring last."""
    reasons: list[str] = []

    if not rule.enabled:
        return _no_match("rule disabled")
    if context.event_type not in rule.event_types:
        return _no_match(f"event type {context.event_type} not in rule")
    if rule.camera_ids and context.camera_id not in rule.camera_ids:
        return _no_match("camera not in rule")
    if not rule.schedule.is_active(context.occurred_at):
        return _no_match(f"outside schedule {rule.schedule.describe()}")

    zone_match = match_any(context.detection_boxes, context.zones)
    if zone_match.is_ignored:
        return _no_match("detection is inside an IGNORE zone", zone_match)
    if rule.zone_names and not (rule.zone_names & set(zone_match.names)):
        return _no_match("detection is outside the rule's zones", zone_match)

    # -- AI gate ------------------------------------------------------------
    if not context.ai_available:
        if rule.if_ai_unavailable is AiFallback.DROP:
            return _no_match("AI unavailable and rule says DROP", zone_match)
        if rule.if_ai_unavailable is AiFallback.QUEUE_ONLY:
            return Decision(
                matched=False,
                rule_name=rule.name,
                actions=frozenset({Action.LOG_ONLY}),
                risk=compute_risk([], weights=rule.weights),
                zone_match=zone_match,
                reasons=["AI unavailable; queued for later verification"],
            )
        reasons.append("AI unavailable; alerting on the vendor event")
    elif context.ai_confidence is not None and context.ai_confidence < rule.min_ai_confidence:
        return _no_match(
            f"AI confidence {context.ai_confidence:.2f} below {rule.min_ai_confidence:.2f}",
            zone_match,
        )

    # -- scoring ------------------------------------------------------------
    factors = _collect_factors(rule, context, zone_match)
    risk = compute_risk(factors, weights=rule.weights)
    reasons.extend(risk.labels)

    if _below(risk.severity, rule.min_severity):
        return Decision(
            matched=False,
            rule_name=rule.name,
            actions=frozenset({Action.LOG_ONLY}),
            risk=risk,
            zone_match=zone_match,
            reasons=[*reasons, f"severity below {rule.min_severity.value}"],
        )

    actions = set(rule.actions)
    if risk.severity is Severity.CRITICAL:
        actions.add(Action.ESCALATE)

    return Decision(
        matched=True,
        rule_name=rule.name,
        actions=frozenset(actions),
        risk=risk,
        zone_match=zone_match,
        reasons=reasons,
    )


def _collect_factors(
    rule: Rule, context: EvaluationContext, zone_match: ZoneMatch
) -> list[RiskFactor]:
    factors: list[RiskFactor] = []

    if context.event_type in {"person_detected", "line_crossing", "zone_intrusion"}:
        factors.append(RiskFactor.PERSON_DETECTED)
    if context.event_type == "vehicle_detected":
        factors.append(RiskFactor.VEHICLE_DETECTED)
    if context.perimeter_event or context.event_type in {"line_crossing", "zone_intrusion"}:
        factors.append(RiskFactor.PERIMETER_RULE_TRIGGERED)

    if is_outside_business_hours(context.occurred_at, context.site_timezone):
        factors.append(RiskFactor.OUTSIDE_BUSINESS_HOURS)

    if zone_match.restricted:
        factors.append(
            RiskFactor.VEHICLE_IN_RESTRICTED_ZONE
            if context.event_type == "vehicle_detected"
            else RiskFactor.RESTRICTED_ZONE
        )

    if context.repeated:
        factors.append(RiskFactor.REPEATED_ENTRY)
    if context.recent_tamper:
        factors.append(RiskFactor.CAMERA_TAMPER_NEAR_EVENT)
    if context.device_silent:
        factors.append(RiskFactor.DEVICE_SILENT)
    if not context.ai_available:
        factors.append(RiskFactor.AI_UNAVAILABLE)
    elif context.ai_confidence is not None and context.ai_confidence < 0.6:
        factors.append(RiskFactor.LOW_AI_CONFIDENCE)

    return factors


_SEVERITY_ORDER = [
    Severity.INFO,
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
]


def _below(actual: Severity, minimum: Severity) -> bool:
    return _SEVERITY_ORDER.index(actual) < _SEVERITY_ORDER.index(minimum)


def evaluate_all(rules: list[Rule], context: EvaluationContext) -> Decision:
    """Evaluate every rule and keep the loudest outcome.

    A site has overlapping rules on purpose (a broad after-hours rule plus a narrow
    restricted-zone one); the strongest match wins rather than the first.
    """
    best: Decision | None = None
    for rule in rules:
        decision = evaluate(rule, context)
        if best is None:
            best = decision
            continue
        if (decision.matched, decision.risk.score) > (best.matched, best.risk.score):
            best = decision
    return best or _no_match("no rules configured")
