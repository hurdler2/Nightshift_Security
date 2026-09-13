"""AI inference service — PHASE 0 skeleton (spec §14, §15)."""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.providers import StubDetectorProvider

app = FastAPI(title="Nightshift Security Inference", version=__version__)
provider = StubDetectorProvider()


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "provider": provider.name}


@app.get("/ready", tags=["health"])
async def ready() -> dict[str, str]:
    """No model is loaded yet; say so instead of claiming readiness (spec §0.3)."""
    return {"status": "degraded", "detail": "no detector model loaded (PHASE 5)"}


# TODO(V1-BLOCKER): PHASE 5 — POST /v1/detect (image + task -> DetectionResult),
# RabbitMQ job consumption, and annotated snapshot rendering.
