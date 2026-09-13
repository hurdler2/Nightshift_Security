"""Alarm workflow: escalation and suppression (spec 11, 13)."""

from app.modules.alarms.escalation import (
    CRITICAL_POLICY,
    DEFAULT_POLICY,
    EscalationPolicy,
    EscalationState,
    EscalationStep,
    policy_for,
)
from app.modules.alarms.suppression import (
    NEVER_SUPPRESSED,
    AlarmLevel,
    OutageState,
    should_suppress,
)

__all__ = [
    "CRITICAL_POLICY",
    "DEFAULT_POLICY",
    "NEVER_SUPPRESSED",
    "AlarmLevel",
    "EscalationPolicy",
    "EscalationState",
    "EscalationStep",
    "OutageState",
    "policy_for",
    "should_suppress",
]
