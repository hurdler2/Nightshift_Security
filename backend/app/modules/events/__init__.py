"""Event ingestion, dedup and lifecycle (spec 8)."""

from app.modules.events.dedup import DedupDecision, DedupWindow
from app.modules.events.lifecycle import (
    EventLifecycle,
    EventStatus,
    IllegalTransition,
    ResolutionCode,
)

__all__ = [
    "DedupDecision",
    "DedupWindow",
    "EventLifecycle",
    "EventStatus",
    "IllegalTransition",
    "ResolutionCode",
]
