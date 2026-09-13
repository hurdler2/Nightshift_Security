"""Event and alarm lifecycles (spec §8.3, §11).

Written as an explicit transition table rather than scattered `if` statements: the
states are how a court-usable audit trail is built, and an unnoticed illegal jump
(RESOLVED back to NEW, say) would corrupt the incident history silently.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


class EventStatus(enum.StrEnum):
    NEW = "NEW"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    ALERTED = "ALERTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    #: Deduplicated into an open window; kept for the count, never alarmed on its own.
    SUPPRESSED = "SUPPRESSED"


#: Allowed transitions. Anything absent is a bug, not a business case.
EVENT_TRANSITIONS: dict[EventStatus, frozenset[EventStatus]] = {
    EventStatus.NEW: frozenset(
        {
            EventStatus.VERIFYING,
            # AI down, or a rule that alerts on the vendor event alone (spec §11.3).
            EventStatus.ALERTED,
            EventStatus.SUPPRESSED,
            EventStatus.FALSE_POSITIVE,
        }
    ),
    EventStatus.VERIFYING: frozenset(
        {EventStatus.VERIFIED, EventStatus.FALSE_POSITIVE, EventStatus.ALERTED}
    ),
    EventStatus.VERIFIED: frozenset({EventStatus.ALERTED, EventStatus.FALSE_POSITIVE}),
    EventStatus.ALERTED: frozenset(
        {EventStatus.ACKNOWLEDGED, EventStatus.RESOLVED, EventStatus.FALSE_POSITIVE}
    ),
    EventStatus.ACKNOWLEDGED: frozenset({EventStatus.RESOLVED, EventStatus.FALSE_POSITIVE}),
    # Terminal.
    EventStatus.RESOLVED: frozenset(),
    EventStatus.FALSE_POSITIVE: frozenset(),
    EventStatus.SUPPRESSED: frozenset(),
}

TERMINAL_STATES = frozenset(
    {EventStatus.RESOLVED, EventStatus.FALSE_POSITIVE, EventStatus.SUPPRESSED}
)


class ResolutionCode(enum.StrEnum):
    """Spec §11.4. This feedback is the AI tuning dataset, so it is not free text."""

    TRUE_SECURITY_INCIDENT = "TRUE_SECURITY_INCIDENT"
    SUSPICIOUS_ACTIVITY = "SUSPICIOUS_ACTIVITY"
    AUTHORIZED_PERSON = "AUTHORIZED_PERSON"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    ANIMAL = "ANIMAL"
    WEATHER = "WEATHER"
    LIGHT_CHANGE = "LIGHT_CHANGE"
    MAINTENANCE = "MAINTENANCE"
    CAMERA_ERROR = "CAMERA_ERROR"
    OTHER = "OTHER"

    @property
    def is_true_positive(self) -> bool:
        return self in {
            ResolutionCode.TRUE_SECURITY_INCIDENT,
            ResolutionCode.SUSPICIOUS_ACTIVITY,
        }


class IllegalTransition(ValueError):
    """Raised when code attempts a transition the lifecycle does not allow."""

    def __init__(self, current: EventStatus, target: EventStatus) -> None:
        super().__init__(f"cannot move from {current} to {target}")
        self.current = current
        self.target = target


def can_transition(current: EventStatus, target: EventStatus) -> bool:
    return target in EVENT_TRANSITIONS.get(current, frozenset())


@dataclass(slots=True)
class TransitionRecord:
    at: datetime
    from_status: EventStatus
    to_status: EventStatus
    actor_id: UUID | None = None
    note: str | None = None


@dataclass(slots=True)
class EventLifecycle:
    """Tracks one event's status and the audit trail behind it."""

    status: EventStatus = EventStatus.NEW
    history: list[TransitionRecord] = field(default_factory=list)

    def transition(
        self,
        target: EventStatus,
        at: datetime,
        *,
        actor_id: UUID | None = None,
        note: str | None = None,
    ) -> TransitionRecord:
        if not can_transition(self.status, target):
            raise IllegalTransition(self.status, target)
        record = TransitionRecord(
            at=at, from_status=self.status, to_status=target, actor_id=actor_id, note=note
        )
        self.history.append(record)
        self.status = target
        return record

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def opened_at(self) -> datetime | None:
        for record in self.history:
            if record.to_status is EventStatus.ALERTED:
                return record.at
        return None

    @property
    def acknowledged_at(self) -> datetime | None:
        for record in self.history:
            if record.to_status is EventStatus.ACKNOWLEDGED:
                return record.at
        return None

    @property
    def acknowledgement_latency_seconds(self) -> float | None:
        """Spec §22.2 measures this; it is the operational quality number."""
        opened, acked = self.opened_at, self.acknowledged_at
        if opened is None or acked is None:
            return None
        return (acked - opened).total_seconds()
