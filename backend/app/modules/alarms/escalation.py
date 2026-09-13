"""Alarm escalation chains (spec §11.2).

If the first person does not answer, the alarm climbs. An acknowledgement anywhere in
the chain stops it — including an acknowledgement that arrives while a later step is
already due, which is the case a naive timer implementation gets wrong and then pages
the company owner at 03:00 for an alarm the guard already handled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from app.modules.rules.risk import Severity


@dataclass(frozen=True, slots=True)
class EscalationStep:
    """Notify `role` (or specific users) once `after_seconds` have passed unacked."""

    after_seconds: int
    role: str
    user_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if self.after_seconds < 0:
            raise ValueError("escalation offsets must be non-negative")


@dataclass(frozen=True, slots=True)
class EscalationPolicy:
    name: str
    steps: tuple[EscalationStep, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("an escalation policy needs at least one step")
        offsets = [s.after_seconds for s in self.steps]
        if offsets != sorted(offsets):
            raise ValueError("escalation steps must be ordered by time")

    @property
    def total_seconds(self) -> int:
        return self.steps[-1].after_seconds


#: Spec §11.2, loosened from the original 30/60/120 because email transport puts
#: 10-30s in front of the whole chain (spec §22.2).
DEFAULT_POLICY = EscalationPolicy(
    name="default",
    steps=(
        EscalationStep(after_seconds=0, role="GUARD"),
        EscalationStep(after_seconds=60, role="SITE_MANAGER"),
        EscalationStep(after_seconds=180, role="SECURITY_MANAGER"),
    ),
)

#: A critical alarm skips the slow climb.
CRITICAL_POLICY = EscalationPolicy(
    name="critical",
    steps=(
        EscalationStep(after_seconds=0, role="GUARD"),
        EscalationStep(after_seconds=30, role="SITE_MANAGER"),
        EscalationStep(after_seconds=90, role="SECURITY_MANAGER"),
        EscalationStep(after_seconds=240, role="OWNER"),
    ),
)


def policy_for(severity: Severity) -> EscalationPolicy:
    return CRITICAL_POLICY if severity is Severity.CRITICAL else DEFAULT_POLICY


@dataclass(slots=True)
class EscalationState:
    """Tracks one alarm's climb. Pure: the scheduler asks it what is due."""

    opened_at: datetime
    policy: EscalationPolicy = DEFAULT_POLICY
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    delivered_steps: set[int] = field(default_factory=set)

    @property
    def is_stopped(self) -> bool:
        return self.acknowledged_at is not None or self.resolved_at is not None

    def acknowledge(self, at: datetime) -> bool:
        """Stop the chain. Returns False if it was already stopped."""
        if self.is_stopped:
            return False
        self.acknowledged_at = at
        return True

    def resolve(self, at: datetime) -> None:
        self.resolved_at = at

    def due_steps(self, now: datetime) -> list[EscalationStep]:
        """Steps whose time has come and that have not been delivered yet.

        Returns nothing once acknowledged, whatever the clock says.
        """
        if self.is_stopped:
            return []
        elapsed = (now - self.opened_at).total_seconds()
        return [
            step
            for index, step in enumerate(self.policy.steps)
            if index not in self.delivered_steps and elapsed >= step.after_seconds
        ]

    def mark_delivered(self, step: EscalationStep) -> None:
        for index, candidate in enumerate(self.policy.steps):
            if candidate is step or candidate == step:
                self.delivered_steps.add(index)
                return

    def next_due_at(self, now: datetime) -> datetime | None:
        """When the scheduler should look at this alarm again."""
        if self.is_stopped:
            return None
        for index, step in enumerate(self.policy.steps):
            if index in self.delivered_steps:
                continue
            due = self.opened_at + timedelta(seconds=step.after_seconds)
            if due > now:
                return due
        return None

    @property
    def is_exhausted(self) -> bool:
        return len(self.delivered_steps) >= len(self.policy.steps)

    @property
    def acknowledgement_latency_seconds(self) -> float | None:
        if self.acknowledged_at is None:
            return None
        return (self.acknowledged_at - self.opened_at).total_seconds()
