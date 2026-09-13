"""Risk scoring and severity bands (spec §10.3).

Weights are configuration, never constants in business logic: the right numbers for a
fenced concrete plant are not the right numbers for an open storage yard, and they get
tuned from resolution feedback (spec §11.4).

Every score carries its reasons, because "91/100" tells a guard nothing at 02:00 while
"restricted zone + after hours + repeat sighting" tells them what to expect.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Severity(enum.StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


#: Spec §10.3 bands, as (inclusive lower bound, severity), highest first.
SEVERITY_BANDS: tuple[tuple[int, Severity], ...] = (
    (85, Severity.CRITICAL),
    (70, Severity.HIGH),
    (50, Severity.MEDIUM),
    (30, Severity.LOW),
    (0, Severity.INFO),
)


def severity_for(score: int) -> Severity:
    for threshold, severity in SEVERITY_BANDS:
        if score >= threshold:
            return severity
    return Severity.INFO  # pragma: no cover - the 0 band always matches


class RiskFactor(enum.StrEnum):
    PERSON_DETECTED = "person_detected"
    VEHICLE_DETECTED = "vehicle_detected"
    OUTSIDE_BUSINESS_HOURS = "outside_business_hours"
    RESTRICTED_ZONE = "restricted_zone"
    PERIMETER_RULE_TRIGGERED = "perimeter_rule_triggered"
    REPEATED_ENTRY = "repeated_entry"
    CAMERA_TAMPER_NEAR_EVENT = "camera_tamper_near_event"
    VEHICLE_IN_RESTRICTED_ZONE = "vehicle_in_restricted_zone"
    LOW_AI_CONFIDENCE = "low_ai_confidence"
    AI_UNAVAILABLE = "ai_unavailable"
    DEVICE_SILENT = "device_silent"


#: Defaults from spec §10.3. Overridden per tenant, per site, or per rule.
DEFAULT_WEIGHTS: dict[RiskFactor, int] = {
    RiskFactor.PERSON_DETECTED: 10,
    RiskFactor.VEHICLE_DETECTED: 5,
    RiskFactor.OUTSIDE_BUSINESS_HOURS: 25,
    RiskFactor.RESTRICTED_ZONE: 30,
    RiskFactor.PERIMETER_RULE_TRIGGERED: 15,
    RiskFactor.REPEATED_ENTRY: 10,
    RiskFactor.CAMERA_TAMPER_NEAR_EVENT: 20,
    RiskFactor.VEHICLE_IN_RESTRICTED_ZONE: 20,
    RiskFactor.LOW_AI_CONFIDENCE: -15,
    # Not knowing is not the same as nothing happening: the vendor event still stands,
    # so this is a small nudge rather than a penalty (spec §11.3).
    RiskFactor.AI_UNAVAILABLE: 0,
    # The recorder going quiet during an incident is itself suspicious (spec §13.1).
    RiskFactor.DEVICE_SILENT: 25,
}

HUMAN_READABLE: dict[RiskFactor, str] = {
    RiskFactor.PERSON_DETECTED: "İnsan algılandı",
    RiskFactor.VEHICLE_DETECTED: "Araç algılandı",
    RiskFactor.OUTSIDE_BUSINESS_HOURS: "Mesai dışı saat",
    RiskFactor.RESTRICTED_ZONE: "Yasak bölge",
    RiskFactor.PERIMETER_RULE_TRIGGERED: "Çevre kuralı tetiklendi",
    RiskFactor.REPEATED_ENTRY: "Tekrarlanan giriş",
    RiskFactor.CAMERA_TAMPER_NEAR_EVENT: "Yakın zamanda kamera müdahalesi",
    RiskFactor.VEHICLE_IN_RESTRICTED_ZONE: "Yasak bölgede araç",
    RiskFactor.LOW_AI_CONFIDENCE: "AI güveni düşük",
    RiskFactor.AI_UNAVAILABLE: "AI doğrulaması yapılamadı",
    RiskFactor.DEVICE_SILENT: "Cihaz sessizleşti",
}


@dataclass(slots=True)
class RiskReason:
    factor: RiskFactor
    points: int

    @property
    def label(self) -> str:
        return HUMAN_READABLE.get(self.factor, self.factor.value)


@dataclass(slots=True)
class RiskScore:
    score: int
    reasons: list[RiskReason] = field(default_factory=list)

    @property
    def severity(self) -> Severity:
        return severity_for(self.score)

    @property
    def labels(self) -> list[str]:
        """Ordered by contribution, for the alarm detail screen."""
        return [r.label for r in sorted(self.reasons, key=lambda r: -abs(r.points))]

    def to_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "severity": self.severity.value,
            "reasons": [
                {"factor": r.factor.value, "points": r.points, "label": r.label}
                for r in self.reasons
            ],
        }


def compute_risk(
    factors: list[RiskFactor],
    *,
    weights: dict[RiskFactor, int] | None = None,
) -> RiskScore:
    """Sum the weights of the factors that fired, clamped to 0..100.

    Duplicate factors count once: two people in the same restricted zone is one
    restricted-zone fact, not two.
    """
    table = {**DEFAULT_WEIGHTS, **(weights or {})}
    seen: list[RiskFactor] = []
    for factor in factors:
        if factor not in seen:
            seen.append(factor)

    reasons = [RiskReason(factor=f, points=table.get(f, 0)) for f in seen]
    total = sum(r.points for r in reasons)
    return RiskScore(score=max(0, min(100, total)), reasons=reasons)
