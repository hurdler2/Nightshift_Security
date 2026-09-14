"""Nightshift Security cloud API.

PHASE 0 skeleton: application wiring, health endpoints, request correlation and the
module boundaries from spec §35. The business routers land phase by phase; each
module directory documents which phase owns it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, request_id_var
from app.modules.alarms.router import alarms_router, events_router
from app.modules.auth.router import router as auth_router
from app.modules.realtime.router import router as realtime_router
from app.modules.sites.router import (
    cameras_router,
    devices_router,
    push_router,
    rules_router,
    sites_router,
)

settings: Settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
    yield


app = FastAPI(
    title="Nightshift Security API",
    version=__version__,
    description=(
        "Multi-tenant construction-site security platform. Device access happens only "
        "through Edge Agents; this API never talks to an XVR directly."
    ),
    lifespan=lifespan,
    openapi_url="/openapi.json",
    docs_url="/docs",
)


@app.middleware("http")
async def correlate_requests(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["x-request-id"] = request_id
    return response


health = APIRouter(tags=["health"])


@health.get("/health", summary="Liveness probe")
async def liveness() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "env": settings.app_env}


@health.get("/ready", summary="Readiness probe")
async def readiness() -> JSONResponse:
    """Dependency checks land with the modules that own them (PHASE 2+)."""
    return JSONResponse(
        {
            "status": "degraded",
            "checks": {"database": "unknown", "redis": "unknown", "rabbitmq": "unknown"},
            "detail": "dependency probes are wired up in PHASE 2",
        },
        status_code=200,
    )


app.include_router(health)
app.include_router(auth_router)
app.include_router(events_router)
app.include_router(alarms_router)
app.include_router(realtime_router)
app.include_router(sites_router)
app.include_router(devices_router)
app.include_router(cameras_router)
app.include_router(rules_router)
app.include_router(push_router)

# Still to mount, by phase (spec §16, §20):
#   analytics, billing
# TODO(V1-BLOCKER): mount each module router in its phase; keep OpenAPI in sync.
