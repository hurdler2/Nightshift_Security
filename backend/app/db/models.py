"""Database schema (spec §15).

Rules that apply to every table here:

* Every business row carries ``tenant_id``. It is the column PostgreSQL row-level
  security will key on in PHASE 10, and the service layer filters on it before that.
* Every timestamp is ``timestamptz`` in UTC (spec §10.2). The one deliberate exception
  is ``events.occurred_at_local``: the recorder reports its own wall clock with no
  offset, and pretending otherwise would silently shift every event by the site's
  offset.
* Nothing here stores a recorder password. `device_smtp_accounts` holds the credential
  the *recorder* uses to log in to us, which is ours to issue and rotate.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

#: JSONB on PostgreSQL, plain JSON on SQLite so tests can run without a server.
JsonB = JSON().with_variant(JSONB(), "postgresql")
Uuid = PgUUID(as_uuid=True)


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# Tenancy and identity
# ---------------------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    plan: Mapped[str] = mapped_column(String(50), default="starter", nullable=False)
    #: Media retention in days; plan-driven (spec §19.4).
    media_retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    created_at: Mapped[datetime] = _created()

    sites: Mapped[list[Site]] = relationship(back_populates="tenant")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(40), default="VIEWER", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class PushDevice(Base):
    """A phone that can receive alarms (spec §11.1)."""

    __tablename__ = "push_devices"
    __table_args__ = (UniqueConstraint("token", name="uq_push_devices_token"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    platform: Mapped[str] = mapped_column(String(10), nullable=False)  # ios | android
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------------------
# Sites, recorders, cameras
# ---------------------------------------------------------------------------


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: IANA name. Every schedule and every displayed time resolves through this.
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Istanbul", nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    is_armed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Set by the watchdog when the whole site stops reporting (spec §13.3).
    is_offline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = _created()

    tenant: Mapped[Tenant] = relationship(back_populates="sites")
    devices: Mapped[list[Device]] = relationship(back_populates="site")


class Device(Base):
    """One recorder. In V1 this is the only hardware on site."""

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    vendor: Mapped[str] = mapped_column(String(50), default="dahua", nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    firmware_version: Mapped[str | None] = mapped_column(String(100))
    serial_number: Mapped[str | None] = mapped_column(String(100))
    channel_count: Mapped[int | None] = mapped_column(Integer)
    #: Verified capability report from the commissioning probe (spec §5.5).
    capabilities: Mapped[dict] = mapped_column(JsonB, default=dict, nullable=False)
    #: Which parsing profile this recorder's mail uses.
    email_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("firmware_email_profiles.id", ondelete="SET NULL")
    )
    commissioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()

    site: Mapped[Site] = relationship(back_populates="devices")
    cameras: Mapped[list[Camera]] = relationship(back_populates="device")


class Camera(Base):
    __tablename__ = "cameras"
    __table_args__ = (
        UniqueConstraint("device_id", "channel_number", name="uq_camera_device_channel"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("devices.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The channel as the recorder reports it; the mapping to a camera is here.
    channel_number: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: True when the recorder runs a perimeter rule on this channel (4 per device).
    has_perimeter_rule: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_snapshot_key: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created()

    device: Mapped[Device] = relationship(back_populates="cameras")
    zones: Mapped[list[CameraZone]] = relationship(back_populates="camera")


class CameraZone(Base):
    """Polygon drawn on the camera's snapshot, in normalized coordinates (spec §10.1)."""

    __tablename__ = "camera_zones"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cameras.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    zone_type: Mapped[str] = mapped_column(String(30), default="NORMAL", nullable=False)
    #: [[x, y], ...] with every value in 0..1.
    points: Mapped[list] = mapped_column(JsonB, nullable=False)
    created_at: Mapped[datetime] = _created()

    camera: Mapped[Camera] = relationship(back_populates="zones")


# ---------------------------------------------------------------------------
# Mail ingest (spec §7)
# ---------------------------------------------------------------------------


class DeviceSmtpAccount(Base):
    """Credentials the recorder uses to log in to our MTA (spec §7.1).

    This is the device's identity. Rotating a row here and updating the recorder
    through DoLynk Care is the whole revocation story.
    """

    __tablename__ = "device_smtp_accounts"
    __table_args__ = (UniqueConstraint("username", name="uq_smtp_username"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("devices.id", ondelete="CASCADE"), index=True, nullable=False
    )
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The single address this account may send to.
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class FirmwareEmailProfile(Base):
    """Regex rules for one firmware family (spec §7.3).

    ``verified`` stays false until a message from a real recorder has been parsed
    with it. The V1 fleet is uniform, so one verified row covers all fourteen.
    """

    __tablename__ = "firmware_email_profiles"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    vendor: Mapped[str] = mapped_column(String(50), default="dahua", nullable=False)
    model_pattern: Mapped[str | None] = mapped_column(String(200))
    firmware_pattern: Mapped[str | None] = mapped_column(String(200))
    #: {field: regex}
    field_regexes: Mapped[dict] = mapped_column(JsonB, default=dict, nullable=False)
    #: {vendor label: normalized event type}
    event_label_map: Mapped[dict] = mapped_column(JsonB, default=dict, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class EmailMessage(Base):
    """One delivered alarm mail. Unique on message id so redelivery is idempotent."""

    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint("source_message_id", name="uq_email_message_id"),
        Index("ix_email_messages_device_time", "device_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    source_message_id: Mapped[str] = mapped_column(String(400), nullable=False)
    smtp_auth_user: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Shared and rotating under CGNAT, so a weak anomaly signal only (spec §7.4).
    sender_ip: Mapped[str | None] = mapped_column(String(45))
    subject: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime] = _created()
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parse_status: Mapped[str] = mapped_column(String(20), default="UNPARSED", nullable=False)
    profile_name: Mapped[str | None] = mapped_column(String(120))
    warnings: Mapped[list] = mapped_column(JsonB, default=list, nullable=False)


class EmailSample(Base):
    """Raw message kept while a profile is unproven, or when parsing degrades.

    A spike in rows here is the firmware-change alarm (spec §2.4b).
    """

    __tablename__ = "email_samples"

    id: Mapped[uuid.UUID] = _pk()
    device_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("devices.id", ondelete="CASCADE"), index=True, nullable=False
    )
    firmware_version: Mapped[str | None] = mapped_column(String(100))
    profile_name: Mapped[str | None] = mapped_column(String(120))
    raw_message: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    captured_at: Mapped[datetime] = _created()


class DeviceSilenceState(Base):
    """Watchdog bookkeeping per recorder (spec §13.3)."""

    __tablename__ = "device_silence_state"

    device_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_interval_seconds: Mapped[int] = mapped_column(Integer, default=1800, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="NEVER_SEEN", nullable=False)
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Events, media, alarms
# ---------------------------------------------------------------------------


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_events_dedup", "dedup_key", "occurred_at"),
        CheckConstraint("risk_score >= 0 AND risk_score <= 100", name="ck_events_risk_range"),
    )

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    camera_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cameras.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    vendor_label: Mapped[str | None] = mapped_column(String(200))
    channel_number: Mapped[int | None] = mapped_column(Integer)
    #: UTC instant we settled on, used for ordering and retention.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The recorder's own wall clock, naive on purpose (see module docstring).
    occurred_at_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    received_at: Mapped[datetime] = _created()
    source: Mapped[str] = mapped_column(String(20), default="email", nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(String(400))
    dedup_key: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Occurrences folded into this event's window (spec §8.2).
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="NEW", nullable=False)
    risk_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), default="INFO", nullable=False)
    risk_reasons: Mapped[list] = mapped_column(JsonB, default=list, nullable=False)
    parse_status: Mapped[str] = mapped_column(String(20), default="PARSED", nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JsonB, default=dict, nullable=False)


class EventDetection(Base):
    """One AI detection on the event snapshot (spec §9)."""

    __tablename__ = "event_detections"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    #: Normalized [x1, y1, x2, y2].
    box: Mapped[list] = mapped_column(JsonB, nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    zone_names: Mapped[list] = mapped_column(JsonB, default=list, nullable=False)
    created_at: Mapped[datetime] = _created()


class MediaAsset(Base):
    """Snapshot or clip in object storage. Buckets are private; URLs are presigned."""

    __tablename__ = "media_assets"
    __table_args__ = (UniqueConstraint("storage_key", name="uq_media_storage_key"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # snapshot|annotated|clip
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    #: Retention is plan-driven and enforced by a sweeper (spec §19.4).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = _created()


class Alarm(Base):
    __tablename__ = "alarms"
    __table_args__ = (Index("ix_alarms_tenant_status", "tenant_id", "status"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True, nullable=False
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rules.id", ondelete="SET NULL")
    )
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ALERTED", nullable=False)
    opened_at: Mapped[datetime] = _created()
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    resolution_code: Mapped[str | None] = mapped_column(String(40))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    escalation_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AlarmDelivery(Base):
    """One notification attempt, for the audit trail (spec §11.2)."""

    __tablename__ = "alarm_deliveries"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    alarm_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("alarms.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # fcm | apns
    escalation_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True, nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    event_types: Mapped[list] = mapped_column(JsonB, default=list, nullable=False)
    camera_ids: Mapped[list] = mapped_column(JsonB, default=list, nullable=False)
    zone_names: Mapped[list] = mapped_column(JsonB, default=list, nullable=False)
    #: {timezone, start, end, days}
    schedule: Mapped[dict] = mapped_column(JsonB, default=dict, nullable=False)
    min_ai_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    min_severity: Mapped[str] = mapped_column(String(10), default="LOW", nullable=False)
    actions: Mapped[list] = mapped_column(JsonB, default=list, nullable=False)
    if_ai_unavailable: Mapped[str] = mapped_column(
        String(30), default="ALERT_ON_VENDOR_EVENT", nullable=False
    )
    #: Per-rule overrides of the risk weights (spec §10.3).
    weights: Mapped[dict] = mapped_column(JsonB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    """Spec §19.5. Append-only; nothing in the app updates a row here."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_tenant_time", "tenant_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(60))
    target_id: Mapped[str | None] = mapped_column(String(100))
    detail: Mapped[dict] = mapped_column(JsonB, default=dict, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = _created()
