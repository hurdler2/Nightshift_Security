"""End-to-end alarm flow: one delivered mail to one push notification.

    ingest result → suppression → dedup → rule evaluation → alarm → push

This module owns the *order* of those steps and nothing else; each decision belongs to
the module that already implements it. Written as a pure function over a context
object so the whole chain can be exercised without a database, a mail server or a
phone (see `tests/test_alarm_flow.py`).

Order matters and is not arbitrary:

1. **Suppression first.** During a site outage there is no point scoring seventy
   camera faults — but security events are exempt, so a theft still gets through
   (spec §13).
2. **Dedup second.** A duplicate is persisted and counted, never alarmed on. Scoring
   it first would waste an AI call per duplicate.
3. **Rules last**, because only they need the full picture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.modules.alarms.escalation import EscalationState, policy_for
from app.modules.alarms.suppression import OutageState, should_suppress
from app.modules.events.dedup import DedupWindow
from app.modules.events.lifecycle import EventStatus
from app.modules.ingest.pipeline import IngestResult, ParseStatus
from app.modules.notifications.payload import PushPayload, build_push
from app.modules.rules.engine import Action, Decision, EvaluationContext, Rule, evaluate_all
from app.modules.rules.risk import Severity
from app.modules.rules.zones import Zone


@dataclass(slots=True)
class AiVerdict:
    """What the AI service said about the snapshot, if it answered at all."""

    available: bool = True
    confidence: float | None = None
    boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    model: str | None = None

    @classmethod
    def unavailable(cls) -> AiVerdict:
        return cls(available=False)


@dataclass(slots=True)
class SiteContext:
    """Everything the flow needs from the database, resolved before it runs."""

    site_name: str
    site_timezone: str
    rules: list[Rule] = field(default_factory=list)
    #: channel number -> (camera id, camera name)
    cameras_by_channel: dict[int, tuple[str, str]] = field(default_factory=dict)
    #: camera id -> zones
    zones_by_camera: dict[str, list[Zone]] = field(default_factory=dict)
    device_silent: bool = False
    recent_tamper_cameras: set[str] = field(default_factory=set)


class FlowStage(str):
    """Where the flow stopped. Kept as plain strings for logs and metrics."""


BLOCKED = "blocked"
NO_EVENT = "no_event"
SUPPRESSED = "suppressed"
DUPLICATE = "duplicate"
NO_RULE_MATCH = "no_rule_match"
ALARMED = "alarmed"


@dataclass(slots=True)
class FlowOutcome:
    stage: str
    reason: str
    event_status: EventStatus | None = None
    #: Persist the event even when no alarm is raised: the timeline is the product.
    persist_event: bool = False
    occurrence_count: int = 1
    decision: Decision | None = None
    alarm_id: UUID | None = None
    escalation: EscalationState | None = None
    push: PushPayload | None = None
    camera_id: str | None = None
    camera_name: str | None = None

    @property
    def alarmed(self) -> bool:
        return self.stage == ALARMED

    @property
    def severity(self) -> Severity | None:
        return self.decision.severity if self.decision else None


def process(
    result: IngestResult,
    context: SiteContext,
    *,
    outages: OutageState,
    dedup: DedupWindow,
    ai: AiVerdict | None = None,
    now: datetime | None = None,
) -> FlowOutcome:
    """Run one ingested message through to a push payload."""
    if result.blocked:
        # Face refusal already stripped the payload upstream (spec §19.3).
        return FlowOutcome(stage=BLOCKED, reason="face-related alarm refused at ingest")

    event = result.event
    if event is None:  # pragma: no cover - only reachable if ingest changes shape
        return FlowOutcome(stage=NO_EVENT, reason="ingest produced no event")

    moment = now or event.received_at
    camera_id, camera_name = _resolve_camera(context, event.channel_number)

    suppression = should_suppress(
        event_type=event.event_type,
        site_id=str(event.site_id),
        device_id=str(event.device_id),
        outages=outages,
    )
    if suppression.suppressed:
        return FlowOutcome(
            stage=SUPPRESSED,
            reason=suppression.reason,
            event_status=EventStatus.SUPPRESSED,
            persist_event=True,
            camera_id=camera_id,
            camera_name=camera_name,
        )

    dedup_decision = dedup.check(event.dedup_key, moment)
    if dedup_decision.is_duplicate:
        return FlowOutcome(
            stage=DUPLICATE,
            reason=(
                f"folded into the window opened at "
                f"{dedup_decision.window_started_at:%H:%M:%S} "
                f"({dedup_decision.total_in_window} sightings)"
            ),
            event_status=EventStatus.SUPPRESSED,
            persist_event=True,
            occurrence_count=dedup_decision.total_in_window,
            camera_id=camera_id,
            camera_name=camera_name,
        )

    verdict = ai or AiVerdict.unavailable()
    decision = evaluate_all(
        context.rules,
        EvaluationContext(
            event_type=event.event_type,
            occurred_at=_event_instant(event, moment),
            site_timezone=context.site_timezone,
            camera_id=camera_id or "",
            zones=context.zones_by_camera.get(camera_id or "", []),
            detection_boxes=verdict.boxes,
            ai_confidence=verdict.confidence,
            ai_available=verdict.available,
            recent_tamper=camera_id in context.recent_tamper_cameras if camera_id else False,
            repeated=False,
            device_silent=context.device_silent,
            perimeter_event=event.event_type in {"line_crossing", "zone_intrusion"},
        ),
    )

    if not decision.matched:
        return FlowOutcome(
            stage=NO_RULE_MATCH,
            reason=decision.explain(),
            event_status=EventStatus.NEW,
            persist_event=True,
            decision=decision,
            camera_id=camera_id,
            camera_name=camera_name,
        )

    alarm_id = uuid4()
    escalation = EscalationState(opened_at=moment, policy=policy_for(decision.severity))

    push = None
    if Action.PUSH in decision.actions:
        push = build_push(
            event_id=uuid4(),
            alarm_id=alarm_id,
            severity=decision.severity,
            site_name=context.site_name,
            camera_name=camera_name or f"Kanal {event.channel_number or '?'}",
            event_type=event.event_type,
            occurred_at=_event_instant(event, moment),
            risk_score=decision.risk.score,
            reasons=decision.risk.labels,
            has_snapshot=bool(result.media),
            ai_unavailable=not verdict.available,
        )

    return FlowOutcome(
        stage=ALARMED,
        reason=decision.explain(),
        event_status=EventStatus.ALERTED,
        persist_event=True,
        decision=decision,
        alarm_id=alarm_id,
        escalation=escalation,
        push=push,
        camera_id=camera_id,
        camera_name=camera_name,
    )


def _resolve_camera(context: SiteContext, channel: int | None) -> tuple[str | None, str | None]:
    """Map the recorder's channel number to a camera.

    An unmapped channel is not fatal: the alarm still fires, labelled by channel
    number, because a new camera nobody registered is exactly when someone is on site.
    """
    if channel is None:
        return None, None
    entry = context.cameras_by_channel.get(channel)
    return entry if entry else (None, None)


def _event_instant(event, fallback: datetime) -> datetime:
    """The UTC instant to score against.

    The recorder's local timestamp needs the site's timezone to become an instant, and
    that conversion belongs to the persistence layer where the site row is available.
    Until then, delivery time is the honest approximation - it is at most seconds off
    and never silently wrong by a whole offset.
    """
    return event.received_at or fallback


def summarize(outcome: FlowOutcome) -> str:
    """One-line log record for the flow."""
    parts = [outcome.stage.upper()]
    if outcome.camera_name:
        parts.append(outcome.camera_name)
    if outcome.decision:
        parts.append(f"{outcome.decision.risk.score}/{outcome.decision.severity.value}")
    parts.append(outcome.reason)
    return " · ".join(parts)


__all__ = [
    "ALARMED",
    "BLOCKED",
    "DUPLICATE",
    "NO_RULE_MATCH",
    "SUPPRESSED",
    "AiVerdict",
    "FlowOutcome",
    "ParseStatus",
    "SiteContext",
    "process",
    "summarize",
]
