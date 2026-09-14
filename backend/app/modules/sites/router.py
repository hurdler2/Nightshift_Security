"""Commissioning and configuration API: sites, recorders, cameras, zones (spec §16, §5).

This is the surface an installer uses on the day the fourteen recorders go in, so it
follows the order of that day: create the site, register the recorder, mint its SMTP
credentials, map the channels to cameras, draw the zones.

Three things are enforced here rather than left to the caller:

* **The SMTP password is shown exactly once.** Only its hash is stored, so a lost
  password means rotation, not recovery — which is the correct answer for a
  credential that lives in a recorder's config screen.
* **Zones are replaced as a set, never patched.** A polygon half-updated by a dropped
  request would silently disarm part of a site.
* **Every write is scoped by the token's tenant**, and every lookup of somebody
  else's row answers 404, so the API never confirms that a foreign id exists.
"""

from __future__ import annotations

import base64
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, get_session, require
from app.core.rbac import Permission
from app.core.security import hash_password
from app.db import models
from app.modules.devices.watchdog import (
    DEFAULT_INTERVAL_SECONDS,
    WatchdogConfig,
    evaluate_silence,
)
from app.modules.rules.zones import ZoneType

sites_router = APIRouter(prefix="/v1/sites", tags=["sites"])
devices_router = APIRouter(prefix="/v1/devices", tags=["devices"])
cameras_router = APIRouter(prefix="/v1/cameras", tags=["cameras"])
rules_router = APIRouter(prefix="/v1/rules", tags=["rules"])
push_router = APIRouter(prefix="/v1/push-devices", tags=["notifications"])

#: Long enough that the recorder's config field is the weak link, not the secret.
SMTP_PASSWORD_BYTES = 24


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="Europe/Istanbul", max_length=64)
    address: str | None = Field(default=None, max_length=2000)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            # A bad timezone would shift every night schedule; refuse it at the door.
            raise ValueError(f"unknown timezone: {value}") from exc
        return value


class SitePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=2000)
    is_armed: bool | None = None


class SiteOut(BaseModel):
    id: UUID
    name: str
    timezone: str
    address: str | None
    is_armed: bool
    is_offline: bool


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    model: str | None = Field(default=None, max_length=100)
    firmware_version: str | None = Field(default=None, max_length=100)
    serial_number: str | None = Field(default=None, max_length=100)
    channel_count: int | None = Field(default=None, ge=1, le=64)
    vendor: str = Field(default="dahua", max_length=50)


class DeviceOut(BaseModel):
    id: UUID
    site_id: UUID
    name: str
    vendor: str
    model: str | None
    firmware_version: str | None
    serial_number: str | None
    channel_count: int | None
    commissioned_at: datetime | None


class SmtpAccountOut(BaseModel):
    """Returned once, at creation. The password is never retrievable afterwards."""

    device_id: UUID
    username: str
    password: str
    recipient: str
    smtp_host_hint: str = "Set this on the recorder: port 587 with STARTTLS (spec §7.5)."


class CameraIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    channel_number: int = Field(ge=1, le=64)
    enabled: bool = True
    has_perimeter_rule: bool = False


class CameraPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    has_perimeter_rule: bool | None = None


class CameraOut(BaseModel):
    id: UUID
    device_id: UUID
    name: str
    channel_number: int
    enabled: bool
    has_perimeter_rule: bool


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    zone_type: ZoneType = ZoneType.NORMAL
    points: list[tuple[float, float]] = Field(min_length=3)

    @field_validator("points")
    @classmethod
    def normalized(cls, value: list[tuple[float, float]]) -> list[tuple[float, float]]:
        for x, y in value:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                # Pixel coordinates here would silently move the zone on any other
                # resolution; normalized coordinates are the whole point (spec §10.1).
                raise ValueError("zone points must be normalized to 0..1")
        return value


class ZoneOut(BaseModel):
    id: UUID
    camera_id: UUID
    name: str
    zone_type: str
    points: list[tuple[float, float]]


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    site_id: UUID | None = None
    enabled: bool = True
    event_types: list[str] = Field(default_factory=list)
    camera_ids: list[UUID] = Field(default_factory=list)
    zone_names: list[str] = Field(default_factory=list)
    schedule: dict = Field(default_factory=dict)
    min_ai_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_severity: str = Field(default="LOW")
    actions: list[str] = Field(default_factory=lambda: ["push", "snapshot"])
    if_ai_unavailable: str = "ALERT_ON_VENDOR_EVENT"
    weights: dict = Field(default_factory=dict)

    @field_validator("min_severity")
    @classmethod
    def known_severity(cls, value: str) -> str:
        from app.modules.rules.risk import Severity

        return Severity(value.upper()).value


class RuleOut(BaseModel):
    id: UUID
    site_id: UUID | None
    name: str
    enabled: bool
    event_types: list[str]
    zone_names: list[str]
    schedule: dict
    min_severity: str
    actions: list[str]


class PushDeviceIn(BaseModel):
    platform: str = Field(pattern="^(ios|android)$")
    token: str = Field(min_length=10, max_length=512)


class PushDeviceOut(BaseModel):
    id: UUID
    platform: str
    last_seen_at: datetime | None


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


@sites_router.get("", response_model=list[SiteOut])
async def list_sites(
    user: CurrentUser = Depends(require(Permission.SITES_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> list[SiteOut]:
    rows = (
        (
            await session.execute(
                select(models.Site)
                .where(models.Site.tenant_id == user.tenant_id)
                .order_by(models.Site.name)
            )
        )
        .scalars()
        .all()
    )
    return [_site_out(row) for row in rows]


@sites_router.post("", response_model=SiteOut, status_code=status.HTTP_201_CREATED)
async def create_site(
    payload: SiteIn,
    user: CurrentUser = Depends(require(Permission.SITES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> SiteOut:
    row = models.Site(
        id=uuid4(),
        tenant_id=user.tenant_id,
        name=payload.name,
        timezone=payload.timezone,
        address=payload.address,
    )
    session.add(row)
    await session.flush()
    return _site_out(row)


@sites_router.get("/{site_id}", response_model=SiteOut)
async def get_site(
    site_id: UUID,
    user: CurrentUser = Depends(require(Permission.SITES_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> SiteOut:
    return _site_out(await _load_site(session, site_id, user))


@sites_router.patch("/{site_id}", response_model=SiteOut)
async def update_site(
    site_id: UUID,
    payload: SitePatch,
    user: CurrentUser = Depends(require(Permission.SITES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> SiteOut:
    site = await _load_site(session, site_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(site, field, value)
    if payload.is_armed is not None:
        # Disarming a site stops alarms; it belongs in the audit trail (spec §15).
        session.add(
            models.AuditLog(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                action="site_armed" if payload.is_armed else "site_disarmed",
                target_type="site",
                target_id=str(site.id),
                detail={},
            )
        )
    return _site_out(site)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@sites_router.get("/{site_id}/devices", response_model=list[DeviceOut])
async def list_devices(
    site_id: UUID,
    user: CurrentUser = Depends(require(Permission.DEVICES_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> list[DeviceOut]:
    await _load_site(session, site_id, user)
    rows = (
        (
            await session.execute(
                select(models.Device)
                .where(
                    models.Device.tenant_id == user.tenant_id,
                    models.Device.site_id == site_id,
                )
                .order_by(models.Device.name)
            )
        )
        .scalars()
        .all()
    )
    return [_device_out(row) for row in rows]


@sites_router.post(
    "/{site_id}/devices", response_model=DeviceOut, status_code=status.HTTP_201_CREATED
)
async def create_device(
    site_id: UUID,
    payload: DeviceIn,
    user: CurrentUser = Depends(require(Permission.DEVICES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> DeviceOut:
    await _load_site(session, site_id, user)
    row = models.Device(
        id=uuid4(),
        tenant_id=user.tenant_id,
        site_id=site_id,
        name=payload.name,
        vendor=payload.vendor,
        model=payload.model,
        firmware_version=payload.firmware_version,
        serial_number=payload.serial_number,
        channel_count=payload.channel_count,
    )
    session.add(row)
    await session.flush()
    return _device_out(row)


@devices_router.get("/{device_id}", response_model=DeviceOut)
async def get_device(
    device_id: UUID,
    user: CurrentUser = Depends(require(Permission.DEVICES_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> DeviceOut:
    return _device_out(await _load_device(session, device_id, user))


@devices_router.post(
    "/{device_id}/smtp-account",
    response_model=SmtpAccountOut,
    status_code=status.HTTP_201_CREATED,
)
async def issue_smtp_account(
    device_id: UUID,
    user: CurrentUser = Depends(require(Permission.DEVICES_MANAGE)),
    session: AsyncSession = Depends(get_session),
    recipient: str = Query(default="alarm@nightshift.local", max_length=320),
) -> SmtpAccountOut:
    """Mint (or rotate) the credentials this recorder uses to send alarm mail.

    Rotating deactivates the previous account rather than editing it, so a recorder
    still configured with the old password fails to authenticate loudly instead of
    quietly sharing an identity with the new one.
    """
    device = await _load_device(session, device_id, user)

    existing = (
        (
            await session.execute(
                select(models.DeviceSmtpAccount).where(
                    models.DeviceSmtpAccount.device_id == device_id,
                    models.DeviceSmtpAccount.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for account in existing:
        account.is_active = False
        account.rotated_at = now

    username = f"dev-{device.id.hex[:12]}"
    password = secrets.token_urlsafe(SMTP_PASSWORD_BYTES)
    session.add(
        models.DeviceSmtpAccount(
            id=uuid4(),
            tenant_id=user.tenant_id,
            device_id=device.id,
            # Unique per account, so a rotated username never collides with the old row.
            username=f"{username}-{secrets.token_hex(3)}" if existing else username,
            password_hash=hash_password(password),
            recipient=recipient,
        )
    )
    session.add(
        models.AuditLog(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            action="smtp_account_rotated" if existing else "smtp_account_issued",
            target_type="device",
            target_id=str(device.id),
            detail={},
        )
    )
    await session.flush()

    issued = await session.scalar(
        select(models.DeviceSmtpAccount).where(
            models.DeviceSmtpAccount.device_id == device_id,
            models.DeviceSmtpAccount.is_active.is_(True),
        )
    )
    return SmtpAccountOut(
        device_id=device.id,
        username=issued.username,
        password=password,
        recipient=recipient,
    )


class EmailSampleOut(BaseModel):
    """A captured raw message. The body is base64 so a JPEG survives the round trip."""

    id: UUID
    profile_name: str | None
    firmware_version: str | None
    captured_at: datetime
    size_bytes: int
    raw_base64: str | None = None


class TestAlarmOut(BaseModel):
    verified: bool
    detail: str
    message_id: str | None = None
    received_at: datetime | None = None
    event_type: str | None = None
    channel_number: int | None = None
    has_snapshot: bool = False
    parse_status: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SilenceStateOut(BaseModel):
    device_id: UUID
    state: str
    last_message_at: datetime | None
    silent_for_seconds: int | None
    expected_interval_seconds: int
    is_security_relevant: bool
    reason: str


@devices_router.get("/{device_id}/email-samples", response_model=list[EmailSampleOut])
async def list_email_samples(
    device_id: UUID,
    user: CurrentUser = Depends(require(Permission.DEVICES_MANAGE)),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=10, ge=1, le=50),
    include_raw: bool = Query(
        default=False,
        description="Include the full message. Off by default: these are large.",
    ),
) -> list[EmailSampleOut]:
    """Raw messages captured while the parsing profile is unproven (spec 7.3).

    This is the endpoint that turns a real recorder's first alarm into a profile: you
    read what the device actually sent, rather than guessing at its format.
    """
    await _load_device(session, device_id, user)
    rows = (
        (
            await session.execute(
                select(models.EmailSample)
                .where(models.EmailSample.device_id == device_id)
                .order_by(models.EmailSample.captured_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        EmailSampleOut(
            id=row.id,
            profile_name=row.profile_name,
            firmware_version=row.firmware_version,
            captured_at=row.captured_at,
            size_bytes=len(row.raw_message),
            raw_base64=(
                base64.b64encode(row.raw_message).decode("ascii") if include_raw else None
            ),
        )
        for row in rows
    ]


@devices_router.post("/{device_id}/test-alarm", response_model=TestAlarmOut)
async def verify_test_alarm(
    device_id: UUID,
    user: CurrentUser = Depends(require(Permission.DEVICES_MANAGE)),
    session: AsyncSession = Depends(get_session),
    within_minutes: int = Query(default=10, ge=1, le=120),
) -> TestAlarmOut:
    """Did the recorder's test alarm arrive, and did we understand it?

    Commissioning step (spec 5): the installer triggers a test alarm on the recorder,
    then calls this. It reports what we received rather than sending anything itself —
    we are the recorder's mail server, not its client, so there is nothing to push.
    """
    await _load_device(session, device_id, user)
    cutoff = datetime.now(UTC) - timedelta(minutes=within_minutes)

    message = await session.scalar(
        select(models.EmailMessage)
        .where(
            models.EmailMessage.device_id == device_id,
            models.EmailMessage.received_at >= cutoff,
        )
        .order_by(models.EmailMessage.received_at.desc())
        .limit(1)
    )
    if message is None:
        return TestAlarmOut(
            verified=False,
            detail=(
                f"no message from this recorder in the last {within_minutes} minutes; "
                "check the recorder's SMTP settings (port 587, STARTTLS, the issued "
                "username and password) and that the alarm linkage includes email"
            ),
        )

    event = await session.scalar(
        select(models.Event)
        .where(models.Event.source_message_id == message.source_message_id)
        .limit(1)
    )
    if event is None:
        return TestAlarmOut(
            verified=False,
            detail=(
                "the message arrived but produced no event: the parsing profile does "
                "not match this firmware. Fetch the raw sample from /email-samples."
            ),
            message_id=message.source_message_id,
            received_at=message.received_at,
            parse_status=message.parse_status,
            warnings=list(message.warnings or []),
        )

    has_snapshot = bool(
        await session.scalar(
            select(models.MediaAsset.id).where(models.MediaAsset.event_id == event.id).limit(1)
        )
    )
    # The channel number is what maps an alarm to a camera; without it the event
    # cannot be shown on the right feed, so it is not a passing commissioning test.
    verified = event.channel_number is not None and event.parse_status == "PARSED"
    return TestAlarmOut(
        verified=verified,
        detail=(
            "test alarm received and parsed"
            if verified
            else "the message parsed only partially; see warnings and the raw sample"
        ),
        message_id=message.source_message_id,
        received_at=message.received_at,
        event_type=event.event_type,
        channel_number=event.channel_number,
        has_snapshot=has_snapshot,
        parse_status=event.parse_status,
        warnings=list(message.warnings or []),
    )


@devices_router.get("/{device_id}/silence-state", response_model=SilenceStateOut)
async def get_silence_state(
    device_id: UUID,
    user: CurrentUser = Depends(require(Permission.DEVICES_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> SilenceStateOut:
    """How worried to be about a quiet recorder (spec 13.3).

    Evaluated on read from the stored `last_message_at`, so the answer is current even
    if the watchdog sweep has not run since the recorder went quiet.
    """
    device = await _load_device(session, device_id, user)
    site = await session.get(models.Site, device.site_id)
    row = await session.get(models.DeviceSilenceState, device_id)

    config = WatchdogConfig(
        expected_interval_seconds=(
            row.expected_interval_seconds if row else DEFAULT_INTERVAL_SECONDS
        )
    )
    verdict = evaluate_silence(
        last_message_at=row.last_message_at if row else None,
        now=datetime.now(UTC),
        config=config,
        site_timezone=site.timezone if site else "Europe/Istanbul",
    )
    return SilenceStateOut(
        device_id=device_id,
        state=verdict.state.value,
        last_message_at=row.last_message_at if row else None,
        silent_for_seconds=(
            int(verdict.silent_for.total_seconds()) if verdict.silent_for else None
        ),
        expected_interval_seconds=config.expected_interval_seconds,
        is_security_relevant=verdict.is_security_relevant,
        reason=verdict.reason,
    )


# ---------------------------------------------------------------------------
# Cameras and zones
# ---------------------------------------------------------------------------


@devices_router.get("/{device_id}/cameras", response_model=list[CameraOut])
async def list_cameras(
    device_id: UUID,
    user: CurrentUser = Depends(require(Permission.DEVICES_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> list[CameraOut]:
    await _load_device(session, device_id, user)
    rows = (
        (
            await session.execute(
                select(models.Camera)
                .where(models.Camera.device_id == device_id)
                .order_by(models.Camera.channel_number)
            )
        )
        .scalars()
        .all()
    )
    return [_camera_out(row) for row in rows]


@devices_router.post(
    "/{device_id}/cameras", response_model=CameraOut, status_code=status.HTTP_201_CREATED
)
async def create_camera(
    device_id: UUID,
    payload: CameraIn,
    user: CurrentUser = Depends(require(Permission.CAMERAS_CONFIGURE)),
    session: AsyncSession = Depends(get_session),
) -> CameraOut:
    device = await _load_device(session, device_id, user)
    row = models.Camera(
        id=uuid4(),
        tenant_id=user.tenant_id,
        site_id=device.site_id,
        device_id=device.id,
        name=payload.name,
        channel_number=payload.channel_number,
        enabled=payload.enabled,
        has_perimeter_rule=payload.has_perimeter_rule,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        # One channel, one camera: a second row would split one channel's events.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"channel {payload.channel_number} already has a camera",
        ) from exc
    return _camera_out(row)


@cameras_router.patch("/{camera_id}", response_model=CameraOut)
async def update_camera(
    camera_id: UUID,
    payload: CameraPatch,
    user: CurrentUser = Depends(require(Permission.CAMERAS_CONFIGURE)),
    session: AsyncSession = Depends(get_session),
) -> CameraOut:
    camera = await _load_camera(session, camera_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(camera, field, value)
    return _camera_out(camera)


@cameras_router.get("/{camera_id}/zones", response_model=list[ZoneOut])
async def list_zones(
    camera_id: UUID,
    user: CurrentUser = Depends(require(Permission.SITES_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> list[ZoneOut]:
    await _load_camera(session, camera_id, user)
    rows = (
        (
            await session.execute(
                select(models.CameraZone).where(models.CameraZone.camera_id == camera_id)
            )
        )
        .scalars()
        .all()
    )
    return [_zone_out(row) for row in rows]


@cameras_router.put("/{camera_id}/zones", response_model=list[ZoneOut])
async def replace_zones(
    camera_id: UUID,
    payload: list[ZoneIn],
    user: CurrentUser = Depends(require(Permission.ZONES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> list[ZoneOut]:
    """Replace the camera's zones as one set — see the module docstring."""
    camera = await _load_camera(session, camera_id, user)

    existing = (
        (
            await session.execute(
                select(models.CameraZone).where(models.CameraZone.camera_id == camera_id)
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await session.delete(row)
    await session.flush()

    created = [
        models.CameraZone(
            id=uuid4(),
            tenant_id=user.tenant_id,
            camera_id=camera.id,
            name=zone.name,
            zone_type=zone.zone_type.value,
            points=[[x, y] for x, y in zone.points],
        )
        for zone in payload
    ]
    session.add_all(created)
    session.add(
        models.AuditLog(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            action="zones_replaced",
            target_type="camera",
            target_id=str(camera.id),
            detail={"count": len(created)},
        )
    )
    await session.flush()
    return [_zone_out(row) for row in created]


@cameras_router.post("/{camera_id}/live-sessions", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def start_live_session(
    camera_id: UUID,
    user: CurrentUser = Depends(require(Permission.SITES_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """In-app live view is not part of V1 (spec §17).

    The site is behind CGNAT and the recorder is the only hardware, so there is no
    inbound path to its RTSP port. Rather than pretend, this returns 501 with the
    handoff the app actually uses: Dahua's own P2P client. Honest 501 beats a spinner.
    """
    camera = await _load_camera(session, camera_id, user)
    device = await session.get(models.Device, camera.device_id)
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={
            "detail": "live view is handed off to DMSS in V1 (spec §17)",
            "handoff": {
                "scheme": "dmss",
                "serial": (device.serial_number if device else None) or "",
                "channel": camera.channel_number,
                "note": "open the recorder in DMSS/DoLynk; the cloud has no inbound path",
            },
        },
    )


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@rules_router.get("", response_model=list[RuleOut])
async def list_rules(
    user: CurrentUser = Depends(require(Permission.SITES_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> list[RuleOut]:
    rows = (
        (
            await session.execute(
                select(models.Rule)
                .where(models.Rule.tenant_id == user.tenant_id)
                .order_by(models.Rule.name)
            )
        )
        .scalars()
        .all()
    )
    return [_rule_out(row) for row in rows]


@rules_router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: RuleIn,
    user: CurrentUser = Depends(require(Permission.RULES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> RuleOut:
    if payload.site_id is not None:
        await _load_site(session, payload.site_id, user)
    row = models.Rule(
        id=uuid4(),
        tenant_id=user.tenant_id,
        site_id=payload.site_id,
        name=payload.name,
        enabled=payload.enabled,
        event_types=payload.event_types,
        camera_ids=[str(c) for c in payload.camera_ids],
        zone_names=payload.zone_names,
        schedule=payload.schedule,
        min_ai_confidence=payload.min_ai_confidence,
        min_severity=payload.min_severity,
        actions=payload.actions,
        if_ai_unavailable=payload.if_ai_unavailable,
        weights=payload.weights,
    )
    session.add(row)
    await session.flush()
    return _rule_out(row)


@rules_router.patch("/{rule_id}", response_model=RuleOut)
async def update_rule(
    rule_id: UUID,
    payload: RuleIn,
    user: CurrentUser = Depends(require(Permission.RULES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> RuleOut:
    rule = await session.get(models.Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    user.assert_owns(rule.tenant_id)

    data = payload.model_dump(exclude_unset=True)
    if "camera_ids" in data:
        data["camera_ids"] = [str(c) for c in data["camera_ids"]]
    for field, value in data.items():
        setattr(rule, field, value)
    return _rule_out(rule)


@rules_router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: UUID,
    user: CurrentUser = Depends(require(Permission.RULES_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> None:
    rule = await session.get(models.Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    user.assert_owns(rule.tenant_id)
    await session.delete(rule)
    session.add(
        models.AuditLog(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            action="rule_deleted",
            target_type="rule",
            target_id=str(rule_id),
            detail={"name": rule.name},
        )
    )


# ---------------------------------------------------------------------------
# Push registration
# ---------------------------------------------------------------------------


@push_router.post("", response_model=PushDeviceOut, status_code=status.HTTP_201_CREATED)
async def register_push_device(
    payload: PushDeviceIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PushDeviceOut:
    """Register this phone for alarms. Any authenticated user may register their own.

    A token can move between accounts when a phone is handed to another guard, so an
    existing row is reassigned rather than rejected — otherwise the new owner would
    silently receive nothing.
    """
    existing = await session.scalar(
        select(models.PushDevice).where(models.PushDevice.token == payload.token)
    )
    now = datetime.now(UTC)
    if existing is not None:
        existing.user_id = user.user_id
        existing.tenant_id = user.tenant_id
        existing.platform = payload.platform
        existing.last_seen_at = now
        return PushDeviceOut(
            id=existing.id, platform=existing.platform, last_seen_at=existing.last_seen_at
        )

    row = models.PushDevice(
        id=uuid4(),
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        platform=payload.platform,
        token=payload.token,
        last_seen_at=now,
    )
    session.add(row)
    await session.flush()
    return PushDeviceOut(id=row.id, platform=row.platform, last_seen_at=row.last_seen_at)


@push_router.delete("/{push_device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_push_device(
    push_device_id: UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(models.PushDevice, push_device_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    user.assert_owns(row.tenant_id)
    if row.user_id != user.user_id:
        # Unregistering someone else's phone is a way to silence them.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    await session.delete(row)


# ---------------------------------------------------------------------------
# Loaders and serialisers
# ---------------------------------------------------------------------------


async def _load_site(session: AsyncSession, site_id: UUID, user: CurrentUser) -> models.Site:
    row = await session.get(models.Site, site_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    user.assert_owns(row.tenant_id)
    return row


async def _load_device(session: AsyncSession, device_id: UUID, user: CurrentUser) -> models.Device:
    row = await session.get(models.Device, device_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    user.assert_owns(row.tenant_id)
    return row


async def _load_camera(session: AsyncSession, camera_id: UUID, user: CurrentUser) -> models.Camera:
    row = await session.get(models.Camera, camera_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    user.assert_owns(row.tenant_id)
    return row


def _site_out(row: models.Site) -> SiteOut:
    return SiteOut(
        id=row.id,
        name=row.name,
        timezone=row.timezone,
        address=row.address,
        is_armed=row.is_armed,
        is_offline=row.is_offline,
    )


def _device_out(row: models.Device) -> DeviceOut:
    return DeviceOut(
        id=row.id,
        site_id=row.site_id,
        name=row.name,
        vendor=row.vendor,
        model=row.model,
        firmware_version=row.firmware_version,
        serial_number=row.serial_number,
        channel_count=row.channel_count,
        commissioned_at=row.commissioned_at,
    )


def _camera_out(row: models.Camera) -> CameraOut:
    return CameraOut(
        id=row.id,
        device_id=row.device_id,
        name=row.name,
        channel_number=row.channel_number,
        enabled=row.enabled,
        has_perimeter_rule=row.has_perimeter_rule,
    )


def _zone_out(row: models.CameraZone) -> ZoneOut:
    return ZoneOut(
        id=row.id,
        camera_id=row.camera_id,
        name=row.name,
        zone_type=row.zone_type,
        points=[(float(x), float(y)) for x, y in row.points],
    )


def _rule_out(row: models.Rule) -> RuleOut:
    return RuleOut(
        id=row.id,
        site_id=row.site_id,
        name=row.name,
        enabled=row.enabled,
        event_types=list(row.event_types or []),
        zone_names=list(row.zone_names or []),
        schedule=dict(row.schedule or {}),
        min_severity=row.min_severity,
        actions=list(row.actions or []),
    )


__all__ = ["cameras_router", "devices_router", "push_router", "rules_router", "sites_router"]
