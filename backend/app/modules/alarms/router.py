"""Event and alarm endpoints (spec §16).

Every query is tenant-scoped from the token. There is no code path where a tenant id
arrives from the client, which is what makes the cross-tenant tests in
`test_api.py` meaningful rather than decorative.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_session, require
from app.core.rbac import Permission
from app.db import models
from app.modules.events.lifecycle import ResolutionCode
from app.modules.realtime.hub import alarm_status_frame, hub

events_router = APIRouter(prefix="/v1/events", tags=["events"])
alarms_router = APIRouter(prefix="/v1/alarms", tags=["alarms"])

MAX_PAGE = 200


class EventOut(BaseModel):
    id: UUID
    event_type: str
    severity: str
    risk_score: int
    status: str
    camera_id: UUID | None
    channel_number: int | None
    occurred_at: datetime
    occurrence_count: int
    risk_reasons: list[str]
    parse_status: str


class AlarmOut(BaseModel):
    id: UUID
    event_id: UUID
    severity: str
    status: str
    opened_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    resolution_code: str | None


class ResolveRequest(BaseModel):
    resolution_code: ResolutionCode
    note: str | None = Field(default=None, max_length=2000)


@events_router.get("", response_model=list[EventOut])
async def list_events(
    user: CurrentUser = Depends(require(Permission.EVENTS_VIEW)),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    event_type: str | None = None,
    severity: str | None = None,
    site_id: UUID | None = None,
) -> list[EventOut]:
    stmt = (
        select(models.Event)
        .where(models.Event.tenant_id == user.tenant_id)
        .order_by(models.Event.occurred_at.desc())
        .limit(limit)
    )
    if event_type:
        stmt = stmt.where(models.Event.event_type == event_type)
    if severity:
        stmt = stmt.where(models.Event.severity == severity.upper())
    if site_id:
        stmt = stmt.where(models.Event.site_id == site_id)

    rows = (await session.execute(stmt)).scalars().all()
    return [_event_out(row) for row in rows]


@events_router.get("/{event_id}", response_model=EventOut)
async def get_event(
    event_id: UUID,
    user: CurrentUser = Depends(require(Permission.EVENTS_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    row = await session.get(models.Event, event_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    user.assert_owns(row.tenant_id)
    return _event_out(row)


@alarms_router.get("", response_model=list[AlarmOut])
async def list_alarms(
    user: CurrentUser = Depends(require(Permission.ALARMS_VIEW)),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    alarm_status: str | None = Query(default=None, alias="status"),
    severity: str | None = None,
) -> list[AlarmOut]:
    stmt = (
        select(models.Alarm)
        .where(models.Alarm.tenant_id == user.tenant_id)
        .order_by(models.Alarm.opened_at.desc())
        .limit(limit)
    )
    if alarm_status:
        stmt = stmt.where(models.Alarm.status == alarm_status.upper())
    if severity:
        stmt = stmt.where(models.Alarm.severity == severity.upper())

    rows = (await session.execute(stmt)).scalars().all()
    return [_alarm_out(row) for row in rows]


@alarms_router.get("/{alarm_id}", response_model=AlarmOut)
async def get_alarm(
    alarm_id: UUID,
    user: CurrentUser = Depends(require(Permission.ALARMS_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> AlarmOut:
    return _alarm_out(await _load_alarm(session, alarm_id, user))


@alarms_router.post("/{alarm_id}/acknowledge", response_model=AlarmOut)
async def acknowledge(
    alarm_id: UUID,
    user: CurrentUser = Depends(require(Permission.ALARMS_ACKNOWLEDGE)),
    session: AsyncSession = Depends(get_session),
) -> AlarmOut:
    alarm = await _load_alarm(session, alarm_id, user)

    if alarm.acknowledged_at is not None:
        # Idempotent: two guards tapping at once must not race or double-log.
        return _alarm_out(alarm)
    if alarm.resolved_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="alarm is already resolved"
        )

    alarm.acknowledged_at = datetime.now(UTC)
    alarm.acknowledged_by = user.user_id
    alarm.status = "ACKNOWLEDGED"
    _audit(session, user, "alarm_acknowledged", alarm.id)
    # Every other guard looking at the same alarm sees it claimed, without polling.
    hub.publish(
        user.tenant_id,
        alarm_status_frame(alarm_id=alarm.id, status=alarm.status, actor_id=user.user_id),
    )
    return _alarm_out(alarm)


@alarms_router.post("/{alarm_id}/resolve", response_model=AlarmOut)
async def resolve(
    alarm_id: UUID,
    payload: ResolveRequest,
    user: CurrentUser = Depends(require(Permission.ALARMS_RESOLVE)),
    session: AsyncSession = Depends(get_session),
) -> AlarmOut:
    alarm = await _load_alarm(session, alarm_id, user)
    if alarm.resolved_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="alarm is already resolved"
        )

    now = datetime.now(UTC)
    alarm.resolved_at = now
    alarm.resolved_by = user.user_id
    alarm.resolution_code = payload.resolution_code.value
    alarm.resolution_note = payload.note
    alarm.status = "RESOLVED"
    # Resolving without acknowledging is legitimate (a manager closing a daylight
    # false positive); record the acknowledgement so the audit trail stays complete.
    if alarm.acknowledged_at is None:
        alarm.acknowledged_at = now
        alarm.acknowledged_by = user.user_id

    event = await session.get(models.Event, alarm.event_id)
    if event is not None:
        event.status = (
            "FALSE_POSITIVE"
            if not payload.resolution_code.is_true_positive
            and payload.resolution_code is ResolutionCode.FALSE_POSITIVE
            else "RESOLVED"
        )

    _audit(session, user, "alarm_resolved", alarm.id, {"code": payload.resolution_code.value})
    hub.publish(
        user.tenant_id,
        alarm_status_frame(alarm_id=alarm.id, status=alarm.status, actor_id=user.user_id),
    )
    return _alarm_out(alarm)


async def _load_alarm(
    session: AsyncSession, alarm_id: UUID, user: CurrentUser
) -> models.Alarm:
    alarm = await session.get(models.Alarm, alarm_id)
    if alarm is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    user.assert_owns(alarm.tenant_id)
    return alarm


def _audit(
    session: AsyncSession,
    user: CurrentUser,
    action: str,
    target_id: UUID,
    detail: dict | None = None,
) -> None:
    session.add(
        models.AuditLog(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            action=action,
            target_type="alarm",
            target_id=str(target_id),
            detail=detail or {},
        )
    )


def _event_out(row: models.Event) -> EventOut:
    return EventOut(
        id=row.id,
        event_type=row.event_type,
        severity=row.severity,
        risk_score=row.risk_score,
        status=row.status,
        camera_id=row.camera_id,
        channel_number=row.channel_number,
        occurred_at=row.occurred_at,
        occurrence_count=row.occurrence_count,
        risk_reasons=list(row.risk_reasons or []),
        parse_status=row.parse_status,
    )


def _alarm_out(row: models.Alarm) -> AlarmOut:
    return AlarmOut(
        id=row.id,
        event_id=row.event_id,
        severity=row.severity,
        status=row.status,
        opened_at=row.opened_at,
        acknowledged_at=row.acknowledged_at,
        resolved_at=row.resolved_at,
        resolution_code=row.resolution_code,
    )
