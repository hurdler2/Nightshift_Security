"""Pilot metrics (spec §22.2).

The week-long site trial has a list of numbers it has to produce, and this is where
they come from. Two principles decide the shape of every endpoint here:

* **Report what the database knows, and say plainly what it does not.** Some of the
  pilot's metrics — events we missed entirely, whether the horn made someone leave —
  can only come from watching the recordings. They are named in `not_measurable` with
  the reason, rather than quietly omitted or, worse, approximated into a number
  somebody would then trust.
* **The false-positive rate is a cost metric, not only a quality one** (spec §22.1).
  Email volume scales with it, so it is reported next to the storage figures rather
  than in a separate "quality" corner.

Every query is tenant-scoped from the token, like everywhere else.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_session, require
from app.core.rbac import Permission
from app.db import models
from app.modules.events.lifecycle import ResolutionCode

router = APIRouter(prefix="/v1/analytics", tags=["analytics"])

MAX_WINDOW_DAYS = 90

#: Metrics the pilot needs that no query can answer. Stated, not silently dropped.
NOT_MEASURABLE = {
    "missed_events": (
        "only findable by reviewing the recorder's own footage afterwards; the cloud "
        "never sees what the recorder did not send"
    ),
    "deterrent_departure_rate": (
        "whether the voice prompt made someone leave is an observation from the "
        "recording, not something the alarm pipeline can know"
    ),
    "smd_to_email_latency": (
        "the gap between the recorder detecting motion and sending mail happens "
        "inside the device; we can only measure from arrival onwards"
    ),
}


class Window(BaseModel):
    days: int
    since: datetime
    until: datetime


class VolumeOut(BaseModel):
    events: int
    alarms: int
    duplicates_suppressed: int
    messages_received: int
    media_stored: int
    media_bytes: int
    snapshot_attachment_rate: float | None = Field(
        default=None, description="Share of events that arrived with a usable image."
    )


class QualityOut(BaseModel):
    resolved: int
    true_positive: int
    false_positive: int
    false_positive_rate: float | None
    by_resolution_code: dict[str, int]
    unresolved: int


class ResponseOut(BaseModel):
    acknowledged: int
    never_acknowledged: int
    ack_seconds_p50: float | None
    ack_seconds_p95: float | None
    resolve_seconds_p50: float | None
    escalated_past_first_step: int


class DeliveryOut(BaseModel):
    attempts: int
    sent: int
    failed: int
    pruned_tokens: int
    delivery_rate: float | None
    email_to_push_seconds_p50: float | None
    email_to_push_seconds_p95: float | None


class HealthOut(BaseModel):
    device_silent_events: int
    site_offline_events: int
    devices_currently_silent: int
    devices_never_seen: int


class SummaryOut(BaseModel):
    window: Window
    volume: VolumeOut
    quality: QualityOut
    response: ResponseOut
    delivery: DeliveryOut
    health: HealthOut
    not_measurable: dict[str, str] = Field(default_factory=lambda: dict(NOT_MEASURABLE))


class DailyPoint(BaseModel):
    day: date
    events: int
    alarms: int
    true_positive: int
    false_positive: int


@router.get("/summary", response_model=SummaryOut)
async def summary(
    user: CurrentUser = Depends(require(Permission.ANALYTICS_VIEW)),
    session: AsyncSession = Depends(get_session),
    days: int = Query(default=7, ge=1, le=MAX_WINDOW_DAYS),
    site_id: UUID | None = None,
) -> SummaryOut:
    """Everything the pilot report needs, for one window."""
    until = datetime.now(UTC)
    since = until - timedelta(days=days)

    return SummaryOut(
        window=Window(days=days, since=since, until=until),
        volume=await _volume(session, user, since, site_id),
        quality=await _quality(session, user, since, site_id),
        response=await _response(session, user, since, site_id),
        delivery=await _delivery(session, user, since),
        health=await _health(session, user, since, site_id),
    )


@router.get("/daily", response_model=list[DailyPoint])
async def daily(
    user: CurrentUser = Depends(require(Permission.ANALYTICS_VIEW)),
    session: AsyncSession = Depends(get_session),
    days: int = Query(default=14, ge=1, le=MAX_WINDOW_DAYS),
    site_id: UUID | None = None,
) -> list[DailyPoint]:
    """Per-day counts, oldest first, for the trend the weekly report shows."""
    since = datetime.now(UTC) - timedelta(days=days)
    day = func.date_trunc("day", models.Event.occurred_at).label("day")

    stmt = (
        select(
            day,
            func.count(models.Event.id),
            func.count(models.Alarm.id),
            func.count(
                case(
                    (
                        models.Alarm.resolution_code.in_(
                            [c.value for c in ResolutionCode if c.is_true_positive]
                        ),
                        1,
                    )
                )
            ),
            func.count(
                case((models.Alarm.resolution_code == ResolutionCode.FALSE_POSITIVE.value, 1))
            ),
        )
        .select_from(models.Event)
        .outerjoin(models.Alarm, models.Alarm.event_id == models.Event.id)
        .where(models.Event.tenant_id == user.tenant_id, models.Event.occurred_at >= since)
        .group_by(day)
        .order_by(day)
    )
    if site_id:
        # No ownership check needed: the tenant clause above already means a foreign
        # site id simply matches nothing, rather than leaking a row or a 403.
        stmt = stmt.where(models.Event.site_id == site_id)

    rows = (await session.execute(stmt)).all()
    return [
        DailyPoint(
            day=moment.date(),
            events=events,
            alarms=alarms,
            true_positive=true_positive,
            false_positive=false_positive,
        )
        for moment, events, alarms, true_positive, false_positive in rows
    ]


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


async def _volume(
    session: AsyncSession, user: CurrentUser, since: datetime, site_id: UUID | None
) -> VolumeOut:
    event_filter = [models.Event.tenant_id == user.tenant_id, models.Event.occurred_at >= since]
    if site_id:
        event_filter.append(models.Event.site_id == site_id)

    events, duplicates = (
        await session.execute(
            select(
                func.count(models.Event.id),
                # occurrence_count above 1 means the dedup window absorbed repeats.
                func.coalesce(func.sum(models.Event.occurrence_count - 1), 0),
            ).where(and_(*event_filter))
        )
    ).one()

    alarms = await session.scalar(
        select(func.count(models.Alarm.id)).where(
            models.Alarm.tenant_id == user.tenant_id, models.Alarm.opened_at >= since
        )
    )
    messages = await session.scalar(
        select(func.count(models.EmailMessage.id)).where(
            models.EmailMessage.tenant_id == user.tenant_id,
            models.EmailMessage.received_at >= since,
        )
    )
    media_count, media_bytes = (
        await session.execute(
            select(
                func.count(models.MediaAsset.id),
                func.coalesce(func.sum(models.MediaAsset.size_bytes), 0),
            ).where(
                models.MediaAsset.tenant_id == user.tenant_id,
                models.MediaAsset.created_at >= since,
            )
        )
    ).one()

    return VolumeOut(
        events=events,
        alarms=alarms or 0,
        duplicates_suppressed=int(duplicates or 0),
        messages_received=messages or 0,
        media_stored=media_count,
        media_bytes=int(media_bytes or 0),
        snapshot_attachment_rate=_ratio(media_count, events),
    )


async def _quality(
    session: AsyncSession, user: CurrentUser, since: datetime, site_id: UUID | None
) -> QualityOut:
    stmt = select(models.Alarm.resolution_code, func.count(models.Alarm.id)).where(
        models.Alarm.tenant_id == user.tenant_id, models.Alarm.opened_at >= since
    )
    if site_id:
        stmt = stmt.where(models.Alarm.site_id == site_id)
    rows = (await session.execute(stmt.group_by(models.Alarm.resolution_code))).all()

    by_code = {code: count for code, count in rows if code}
    unresolved = sum(count for code, count in rows if not code)
    true_positive = sum(
        count for code, count in by_code.items() if ResolutionCode(code).is_true_positive
    )
    false_positive = by_code.get(ResolutionCode.FALSE_POSITIVE.value, 0)
    resolved = sum(by_code.values())

    return QualityOut(
        resolved=resolved,
        true_positive=true_positive,
        false_positive=false_positive,
        # Against resolved alarms only: counting unresolved ones as correct would
        # flatter the number exactly when people have stopped closing alarms.
        false_positive_rate=_ratio(false_positive, resolved),
        by_resolution_code=by_code,
        unresolved=unresolved,
    )


async def _response(
    session: AsyncSession, user: CurrentUser, since: datetime, site_id: UUID | None
) -> ResponseOut:
    ack_seconds = func.extract(
        "epoch", models.Alarm.acknowledged_at - models.Alarm.opened_at
    )
    resolve_seconds = func.extract("epoch", models.Alarm.resolved_at - models.Alarm.opened_at)

    stmt = select(
        func.count(models.Alarm.id).filter(models.Alarm.acknowledged_at.isnot(None)),
        func.count(models.Alarm.id).filter(models.Alarm.acknowledged_at.is_(None)),
        func.percentile_cont(0.5).within_group(ack_seconds),
        func.percentile_cont(0.95).within_group(ack_seconds),
        func.percentile_cont(0.5).within_group(resolve_seconds),
    ).where(models.Alarm.tenant_id == user.tenant_id, models.Alarm.opened_at >= since)
    if site_id:
        stmt = stmt.where(models.Alarm.site_id == site_id)

    acknowledged, never, ack_p50, ack_p95, resolve_p50 = (await session.execute(stmt)).one()

    escalated = await session.scalar(
        select(func.count(func.distinct(models.AlarmDelivery.alarm_id))).where(
            models.AlarmDelivery.tenant_id == user.tenant_id,
            models.AlarmDelivery.escalation_step > 0,
            models.AlarmDelivery.created_at >= since,
        )
    )

    return ResponseOut(
        acknowledged=acknowledged,
        never_acknowledged=never,
        ack_seconds_p50=_seconds(ack_p50),
        ack_seconds_p95=_seconds(ack_p95),
        resolve_seconds_p50=_seconds(resolve_p50),
        escalated_past_first_step=escalated or 0,
    )


async def _delivery(session: AsyncSession, user: CurrentUser, since: datetime) -> DeliveryOut:
    rows = (
        await session.execute(
            select(models.AlarmDelivery.status, func.count(models.AlarmDelivery.id))
            .where(
                models.AlarmDelivery.tenant_id == user.tenant_id,
                models.AlarmDelivery.created_at >= since,
            )
            .group_by(models.AlarmDelivery.status)
        )
    ).all()
    by_status = dict(rows)
    attempts = sum(by_status.values())
    sent = by_status.get("SENT", 0)

    # Arrival to notification: the half of the latency budget we actually control.
    latency = func.extract("epoch", models.AlarmDelivery.sent_at - models.EmailMessage.received_at)
    p50, p95 = (
        await session.execute(
            select(
                func.percentile_cont(0.5).within_group(latency),
                func.percentile_cont(0.95).within_group(latency),
            )
            .select_from(models.AlarmDelivery)
            .join(models.Alarm, models.Alarm.id == models.AlarmDelivery.alarm_id)
            .join(models.Event, models.Event.id == models.Alarm.event_id)
            .join(
                models.EmailMessage,
                models.EmailMessage.source_message_id == models.Event.source_message_id,
            )
            .where(
                models.AlarmDelivery.tenant_id == user.tenant_id,
                models.AlarmDelivery.sent_at.isnot(None),
                models.AlarmDelivery.created_at >= since,
            )
        )
    ).one()

    return DeliveryOut(
        attempts=attempts,
        sent=sent,
        failed=by_status.get("FAILED", 0),
        pruned_tokens=by_status.get("UNREGISTERED", 0),
        delivery_rate=_ratio(sent, attempts),
        email_to_push_seconds_p50=_seconds(p50),
        email_to_push_seconds_p95=_seconds(p95),
    )


async def _health(
    session: AsyncSession, user: CurrentUser, since: datetime, site_id: UUID | None
) -> HealthOut:
    stmt = select(models.Event.event_type, func.count(models.Event.id)).where(
        models.Event.tenant_id == user.tenant_id,
        models.Event.occurred_at >= since,
        models.Event.event_type.in_(["device_silent", "site_offline"]),
    )
    if site_id:
        stmt = stmt.where(models.Event.site_id == site_id)
    by_type = dict((await session.execute(stmt.group_by(models.Event.event_type))).all())

    states = dict(
        (
            await session.execute(
                select(
                    models.DeviceSilenceState.state,
                    func.count(models.DeviceSilenceState.device_id),
                )
                .where(models.DeviceSilenceState.tenant_id == user.tenant_id)
                .group_by(models.DeviceSilenceState.state)
            )
        ).all()
    )

    return HealthOut(
        device_silent_events=by_type.get("device_silent", 0),
        site_offline_events=by_type.get("site_offline", 0),
        devices_currently_silent=states.get("SILENT", 0),
        devices_never_seen=states.get("NEVER_SEEN", 0),
    )


def _ratio(part: int | None, whole: int | None) -> float | None:
    """None rather than 0.0 when there is nothing to divide: an empty week has no rate."""
    if not whole:
        return None
    return round((part or 0) / whole, 4)


def _seconds(value) -> float | None:
    return None if value is None else round(float(value), 2)


__all__ = ["NOT_MEASURABLE", "router"]
