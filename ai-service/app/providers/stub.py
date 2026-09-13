"""Placeholder provider used until a real model is wired up in PHASE 5.

It deliberately returns *no* detections rather than plausible fake ones: a fabricated
confidence would silently poison rule evaluation and the tuning dataset (spec §0.3).
"""

from __future__ import annotations

from app.providers.base import DetectionResult


class StubDetectorProvider:
    name = "stub"

    async def detect(self, image: bytes, task: str) -> DetectionResult:
        if not image:
            raise ValueError("empty image payload")
        return DetectionResult(task=task, detections=[], model="stub-no-model")


# TODO(V1-BLOCKER): PHASE 5 — ONNX person/vehicle detector plus annotated-snapshot
# rendering; PHASE 11 adds PPE, fire/smoke, loitering, crowd and scene tamper.
