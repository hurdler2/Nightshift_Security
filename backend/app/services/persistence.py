"""Database side of the alarm flow: load the context, store the outcome.

The flow itself (``services/alarm_flow``) is pure. This module is the only place that
knows about sessions and rows, which keeps every decision testable without a database
and every query in one reviewable file.

Ordering inside :func:`persist_flow` is deliberate: the event row is written before
media and alarms, so a failure part-way through leaves the sighting recorded rather
than losing it entirely. Losing the snapshot is survivable; losing the fact that
someone was on site is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.modules.ingest.pipeline import DeviceIdentity, IngestResult
from app.modules.rules.engine import Action
from app.modules.rules.engine import Rule as RuleSpec
from app.modules.rules.risk import RiskFactor, Severity
from app.modules.rules.schedule import ALWAYS, Schedule
from app.modules.rules.zones import Zone, ZoneType
from app.services.alarm_flow import ALARMED, FlowOutcome, SiteContext

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PersistedFlow:
    event_id: UUID | None = None
    alarm_id: UUID | None = None
    media_ids: list[UUID] = None  # type: ignore[assignment]
    message_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.media_ids is None:
            self.media_ids = []


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


async def resolve_device(session: AsyncSession, smtp_username: str) -> DeviceIdentity | None:
    """Map an SMTP login to the device that owns it (spec §7.1).

    Returns the identity only; password verification stays in the SMTP layer so this
    can also be used by tooling that already knows the device.
    """
    stmt = (
        select(models.DeviceSmtpAccount, models.Device, models.FirmwareEmailProfile)
        .join(models.Device, models.Device.id == models.DeviceSmtpAccount.device_id)
        .outerjoin(
            models.FirmwareEmailProfile,
            models.FirmwareEmailProfile.id == models.Device.email_profile_id,
        )
        .where(
            models.DeviceSmtpAccount.username == smtp_username,
            models.DeviceSmtpAccount.is_active.is_(True),
        )
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None

    account, device, profile = row
    return DeviceIdentity(
        device_id=device.id,
        tenant_id=device.tenant_id,
        site_id=device.site_id,
        smtp_username=account.username,
        # Keep capturing raw messages until a profile row says it is proven.
        capture_samples=profile is None or not profile.verified,
    )


async def load_site_context(session: AsyncSession, device_id: UUID) -> SiteContext | None:
    """Everything the flow needs about one recorder's site, in four queries."""
    device = await session.get(models.Device, device_id)
    if device is None:
        return None
    site = await session.get(models.Site, device.site_id)
    if site is None:
        return None

    cameras = (
        (
            await session.execute(
                select(models.Camera).where(
                    models.Camera.device_id == device_id,
                    models.Camera.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    cameras_by_channel = {c.channel_number: (str(c.id), c.name) for c in cameras}

    zone_rows = (
        (
            await session.execute(
                select(models.CameraZone).where(
                    models.CameraZone.camera_id.in_([c.id for c in cameras] or [uuid4()])
                )
            )
        )
        .scalars()
        .all()
    )
    zones_by_camera: dict[str, list[Zone]] = {}
    for row in zone_rows:
        try:
            zone = Zone(
                name=row.name,
                type=ZoneType(row.zone_type),
                points=[(float(x), float(y)) for x, y in row.points],
            )
        except (ValueError, TypeError) as exc:
            # A malformed polygon must not take the whole site's rules down with it.
            log.warning("skipping invalid zone %s on camera %s: %s", row.name, row.camera_id, exc)
            continue
        zones_by_camera.setdefault(str(row.camera_id), []).append(zone)

    rule_rows = (
        (
            await session.execute(
                select(models.Rule).where(
                    models.Rule.tenant_id == device.tenant_id,
                    models.Rule.enabled.is_(True),
                    (models.Rule.site_id == site.id) | (models.Rule.site_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )

    silence = await session.get(models.DeviceSilenceState, device_id)

    return SiteContext(
        site_name=site.name,
        site_timezone=site.timezone,
        rules=[rule_from_row(row, site.timezone) for row in rule_rows],
        cameras_by_channel=cameras_by_channel,
        zones_by_camera=zones_by_camera,
        device_silent=bool(silence and silence.state == "SILENT"),
    )


def rule_from_row(row: models.Rule, site_timezone: str) -> RuleSpec:
    """Turn a stored rule into the engine's frozen form."""
    schedule = ALWAYS
    raw = row.schedule or {}
    if raw.get("start") and raw.get("end"):
        try:
            schedule = Schedule(
                timezone=raw.get("timezone") or site_timezone,
                start=_parse_clock(raw["start"]),
                end=_parse_clock(raw["end"]),
                days=frozenset(raw.get("days") or range(7)),
            )
        except (ValueError, KeyError) as exc:
            # An unusable schedule arms the rule around the clock rather than
            # silently disarming it - failing loud is safer for a security product.
            log.warning("rule %s has an invalid schedule (%s); treating as 7/24", row.name, exc)

    return RuleSpec(
        name=row.name,
        event_types=frozenset(row.event_types or []),
        schedule=schedule,
        camera_ids=frozenset(str(c) for c in (row.camera_ids or [])),
        zone_names=frozenset(row.zone_names or []),
        min_ai_confidence=float(row.min_ai_confidence or 0.0),
        actions=frozenset(Action(a) for a in (row.actions or ["push", "snapshot"])),
        min_severity=Severity(row.min_severity or "LOW"),
        enabled=row.enabled,
        weights={RiskFactor(k): int(v) for k, v in (row.weights or {}).items()},
    )


def _parse_clock(value: str):
    from datetime import time

    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute or 0))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def touch_device_heartbeat(
    session: AsyncSession,
    tenant_id: UUID,
    device_id: UUID,
    *,
    at: datetime | None = None,
) -> None:
    """Record that we heard from this recorder (spec §13.3).

    The row is created on first contact rather than at commissioning, so a device
    that has never sent anything stays in NEVER_SEEN instead of looking healthy.
    """
    moment = at or datetime.now(UTC)
    state = await session.get(models.DeviceSilenceState, device_id)
    if state is None:
        session.add(
            models.DeviceSilenceState(
                device_id=device_id,
                tenant_id=tenant_id,
                last_message_at=moment,
                state="OK",
                state_changed_at=moment,
            )
        )
        return

    if state.state != "OK":
        # Coming back from silence is a state change worth timestamping: it is how
        # "the recorder was dark for two hours" stays visible after it returns.
        state.state = "OK"
        state.state_changed_at = moment
    state.last_message_at = moment


async def existing_event_id(session: AsyncSession, source_message_id: str | None) -> UUID | None:
    """The event a message already produced, if the recorder is resending it (§8.4)."""
    if not source_message_id:
        return None
    return await session.scalar(
        select(models.Event.id).where(models.Event.source_message_id == source_message_id)
    )


async def persist_flow(
    session: AsyncSession,
    result: IngestResult,
    outcome: FlowOutcome,
    *,
    media_keys: dict[str, str] | None = None,
    event_id: UUID | None = None,
) -> PersistedFlow:
    """Write everything one processed message produced.

    ``media_keys`` maps a media sha256 to the object-storage key it was written to;
    the upload happens before this call so a storage failure never leaves a row
    pointing at an object that does not exist. Because the key contains the event id,
    the caller allocates that id up front and passes it back in as ``event_id``.
    """
    persisted = PersistedFlow()
    identity = result.identity

    persisted.message_id = await _record_message(session, result, outcome)
    # Any delivered message proves the recorder is alive, parsed or not — even a
    # message we failed to understand answers the only question the watchdog asks.
    await touch_device_heartbeat(session, identity.tenant_id, identity.device_id)

    if result.blocked or not outcome.persist_event or outcome.stage is None:
        return persisted

    event = result.event
    if event is None:  # pragma: no cover - defensive
        return persisted

    existing = await existing_event_id(session, event.source_message_id)
    if existing is not None:
        # Redelivery of a message we already processed (spec §8.4).
        persisted.event_id = existing
        return persisted

    decision = outcome.decision
    row = models.Event(
        id=event_id or uuid4(),
        tenant_id=identity.tenant_id,
        site_id=identity.site_id,
        device_id=identity.device_id,
        camera_id=UUID(outcome.camera_id) if outcome.camera_id else None,
        event_type=event.event_type,
        vendor_label=event.vendor_label,
        channel_number=event.channel_number,
        occurred_at=event.received_at,
        occurred_at_local=event.occurred_at_local,
        source=event.source,
        source_message_id=event.source_message_id,
        dedup_key=event.dedup_key,
        occurrence_count=outcome.occurrence_count,
        status=(outcome.event_status.value if outcome.event_status else "NEW"),
        risk_score=decision.risk.score if decision else 0,
        severity=(decision.severity.value if decision else Severity.INFO.value),
        risk_reasons=[r.label for r in decision.risk.reasons] if decision else [],
        parse_status=event.parse_status.value,
        raw_payload={"warnings": event.warnings, "profile": event.profile_name},
    )
    session.add(row)
    await session.flush()
    persisted.event_id = row.id

    for media in outcome_media(result):
        key = (media_keys or {}).get(media.sha256)
        if key is None:
            continue
        asset = models.MediaAsset(
            id=uuid4(),
            tenant_id=identity.tenant_id,
            event_id=row.id,
            kind="snapshot",
            storage_key=key,
            content_type=media.content_type,
            size_bytes=len(media.content),
            sha256=media.sha256,
        )
        session.add(asset)
        persisted.media_ids.append(asset.id)

    if outcome.stage == ALARMED and decision is not None:
        alarm = models.Alarm(
            id=outcome.alarm_id or uuid4(),
            tenant_id=identity.tenant_id,
            site_id=identity.site_id,
            event_id=row.id,
            severity=decision.severity.value,
            status="ALERTED",
            opened_at=datetime.now(UTC),
        )
        session.add(alarm)
        persisted.alarm_id = alarm.id

    await session.flush()
    return persisted


def outcome_media(result: IngestResult):
    return result.media


async def _record_message(
    session: AsyncSession, result: IngestResult, outcome: FlowOutcome
) -> UUID | None:
    """Store the delivery record and, when warranted, the raw message."""
    identity = result.identity
    message = result.message

    duplicate = await session.scalar(
        select(models.EmailMessage.id).where(
            models.EmailMessage.source_message_id == message.message_id
        )
    )
    if duplicate is not None:
        return duplicate

    row = models.EmailMessage(
        id=uuid4(),
        tenant_id=identity.tenant_id,
        device_id=identity.device_id,
        source_message_id=message.message_id,
        smtp_auth_user=identity.smtp_username,
        subject=message.subject[:500] if message.subject else None,
        size_bytes=message.raw_size,
        parse_status=outcome.status_label(),
        profile_name=result.body.profile_name,
        warnings=list(message.warnings),
    )
    session.add(row)

    if result.raw_sample is not None:
        session.add(
            models.EmailSample(
                id=uuid4(),
                device_id=identity.device_id,
                profile_name=result.body.profile_name,
                raw_message=result.raw_sample,
            )
        )

    await session.flush()
    return row.id
