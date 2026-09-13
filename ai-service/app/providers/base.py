"""Detector provider contract (spec §15).

The model name must never leak into business logic: everything goes through
``DetectorProvider`` so ONNX / TensorRT / a remote GPU stay swappable.

Licence gate: no AGPL or otherwise restricted model ships in the commercial SaaS
image without a licence review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class Detection:
    """One detected object, in normalized 0..1 coordinates (spec §17)."""

    label: str
    confidence: float
    #: (x1, y1, x2, y2), normalized so zones stay resolution independent.
    box: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within 0..1")
        if any(not 0.0 <= value <= 1.0 for value in self.box):
            raise ValueError("box coordinates must be normalized to 0..1")


@dataclass(slots=True)
class DetectionResult:
    task: str
    detections: list[Detection] = field(default_factory=list)
    model: str = "unknown"
    latency_ms: float = 0.0

    def best(self, label: str) -> Detection | None:
        matches = [d for d in self.detections if d.label == label]
        return max(matches, key=lambda d: d.confidence) if matches else None

    def max_confidence(self, label: str) -> float:
        detection = self.best(label)
        return detection.confidence if detection else 0.0


@runtime_checkable
class DetectorProvider(Protocol):
    name: str

    async def detect(self, image: bytes, task: str) -> DetectionResult: ...
