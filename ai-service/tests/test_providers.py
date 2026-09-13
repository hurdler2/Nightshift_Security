"""Detector provider contract (spec §15) and service health."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers import Detection, DetectionResult, DetectorProvider, StubDetectorProvider

client = TestClient(app)


class TestHealth:
    def test_health(self):
        assert client.get("/health").json()["status"] == "ok"

    def test_ready_admits_there_is_no_model(self):
        assert client.get("/ready").json()["status"] == "degraded"


class TestStubProvider:
    async def test_returns_no_fabricated_detections(self):
        """A stub must return nothing, not invented confidences (spec §0.3)."""
        result = await StubDetectorProvider().detect(b"\xff\xd8\xff", "person")
        assert result.detections == []
        assert result.model == "stub-no-model"

    async def test_rejects_empty_image(self):
        with pytest.raises(ValueError, match="empty image"):
            await StubDetectorProvider().detect(b"", "person")

    def test_satisfies_the_protocol(self):
        assert isinstance(StubDetectorProvider(), DetectorProvider)


class TestDetectionModel:
    def test_validates_confidence(self):
        with pytest.raises(ValueError, match="confidence"):
            Detection("person", 1.5, (0.1, 0.1, 0.2, 0.2))

    def test_validates_normalized_box(self):
        with pytest.raises(ValueError, match="normalized"):
            Detection("person", 0.9, (10, 10, 200, 200))

    def test_result_helpers(self):
        result = DetectionResult(
            task="person",
            detections=[
                Detection("person", 0.7, (0.1, 0.1, 0.2, 0.3)),
                Detection("person", 0.93, (0.4, 0.1, 0.5, 0.3)),
                Detection("vehicle", 0.5, (0.6, 0.1, 0.9, 0.4)),
            ],
        )
        assert result.max_confidence("person") == 0.93
        assert result.max_confidence("dog") == 0.0
        assert result.best("vehicle").confidence == 0.5
