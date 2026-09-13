"""Prometheus-compatible counters for the edge (spec §45).

PHASE 1 keeps them in-process so the probe and the event stream can report; the
exporter endpoint is wired up with the run loop in PHASE 2.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

METRIC_NAMES = (
    "edge_connected_total",
    "dahua_event_stream_connected",
    "smd_events_total",
    "snapshots_success_total",
    "snapshots_failed_total",
    "rtsp_session_errors_total",
    "queue_depth",
)


@dataclass(slots=True)
class Metrics:
    counters: Counter[str] = field(default_factory=Counter)
    gauges: dict[str, float] = field(default_factory=dict)

    def inc(self, name: str, value: int = 1) -> None:
        self.counters[name] += value

    def set(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def render(self) -> str:
        """Minimal text exposition format."""
        lines = [f"{name} {count}" for name, count in sorted(self.counters.items())]
        lines += [f"{name} {value}" for name, value in sorted(self.gauges.items())]
        return "\n".join(lines) + "\n"


METRICS = Metrics()
