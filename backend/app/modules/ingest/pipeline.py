"""Alarm-mail ingest pipeline (spec §7.2).

    SMTP AUTH → device identity → MIME parse → body profile → event + media

Non-negotiables encoded here:

* The device comes from **SMTP AUTH**, never from the body (spec §7.1).
* A message that cannot be parsed still produces an event (spec §7.3) — silence is
  the one outcome a security product may not have.
* Face events are dropped with their payload before anything is persisted (§19.3).
* Every raw message is captured until its profile is verified, so the real firmware
  format can be pinned from field data rather than guessed (§24.3).
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.modules.ingest.mime import Attachment, ParsedMessage, parse_message
from app.modules.ingest.profiles import PROVISIONAL_DAHUA, EmailProfile, ParsedBody, parse_body

log = logging.getLogger(__name__)


class ParseStatus(enum.StrEnum):
    #: Event type and channel both resolved.
    PARSED = "PARSED"
    #: Something was extracted, but not enough to place the event precisely.
    PARTIAL = "PARTIAL"
    #: Nothing usable in the body; the event is still raised as unknown_alarm.
    UNPARSED = "UNPARSED"
    #: Face event refused (spec §19.3).
    BLOCKED = "BLOCKED"


@dataclass(slots=True)
class DeviceIdentity:
    """Who sent this, established by SMTP AUTH before the body is even read."""

    device_id: UUID
    tenant_id: UUID
    site_id: UUID
    smtp_username: str
    email_profile: EmailProfile = PROVISIONAL_DAHUA
    #: Capture raw messages until the profile is confirmed against this fleet.
    capture_samples: bool = True


@dataclass(slots=True)
class IngestedMedia:
    filename: str | None
    content_type: str
    content: bytes
    sha256: str

    @classmethod
    def from_attachment(cls, attachment: Attachment) -> IngestedMedia:
        return cls(
            filename=attachment.filename,
            content_type=attachment.content_type,
            content=attachment.content,
            sha256=attachment.sha256,
        )


@dataclass(slots=True)
class IngestedEvent:
    """Vendor-neutral event ready for dedup, AI verification and rule evaluation."""

    tenant_id: UUID
    site_id: UUID
    device_id: UUID
    event_type: str
    channel_number: int | None
    channel_name: str | None
    occurred_at_local: datetime | None
    received_at: datetime
    source: str = "email"
    source_message_id: str = ""
    parse_status: ParseStatus = ParseStatus.UNPARSED
    profile_name: str = ""
    vendor_label: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def dedup_key(self) -> str:
        """Spec §8.2: tenant + site + device + channel + type."""
        channel = self.channel_number if self.channel_number is not None else "?"
        return f"{self.tenant_id}:{self.site_id}:{self.device_id}:{channel}:{self.event_type}"


@dataclass(slots=True)
class IngestResult:
    identity: DeviceIdentity
    message: ParsedMessage
    body: ParsedBody
    event: IngestedEvent | None
    media: list[IngestedMedia] = field(default_factory=list)
    raw_sample: bytes | None = None
    blocked: bool = False

    @property
    def status(self) -> ParseStatus:
        if self.blocked:
            return ParseStatus.BLOCKED
        return self.event.parse_status if self.event else ParseStatus.UNPARSED


class SampleStore(Protocol):
    """Persists raw messages while a firmware profile is still unverified."""

    async def save_sample(self, device_id: UUID, raw: bytes, profile_name: str) -> None: ...


def ingest(
    raw: bytes,
    identity: DeviceIdentity,
    *,
    received_at: datetime | None = None,
) -> IngestResult:
    """Turn one raw message into an event plus media. Pure: no I/O, fully testable."""
    now = received_at or datetime.now(UTC)
    message = parse_message(raw)
    body = parse_body(message.body_text, identity.email_profile)

    if body.is_blocked:
        # Nothing about this message is retained: no event, no attachments, no sample.
        log.warning(
            "refused face-related alarm mail from device %s (message %s)",
            identity.device_id,
            message.message_id,
        )
        return IngestResult(
            identity=identity,
            message=_stripped(message),
            body=body,
            event=None,
            media=[],
            raw_sample=None,
            blocked=True,
        )

    status = _classify(body)
    event = IngestedEvent(
        tenant_id=identity.tenant_id,
        site_id=identity.site_id,
        device_id=identity.device_id,
        event_type=body.event_type,
        channel_number=body.channel_number,
        channel_name=body.channel_name,
        occurred_at_local=body.occurred_at,
        received_at=now,
        source_message_id=message.message_id,
        parse_status=status,
        profile_name=body.profile_name,
        vendor_label=body.event_label,
        warnings=list(message.warnings),
    )

    if status is not ParseStatus.PARSED:
        event.warnings.append(
            f"body parse {status.value.lower()} with profile {body.profile_name}; "
            f"missing: {', '.join(body.missing) or 'n/a'}"
        )
    if not identity.email_profile.verified:
        event.warnings.append(
            f"profile {body.profile_name} is unverified - confirm against a real device"
        )
    if not message.images:
        event.warnings.append("no usable JPEG attachment on this alarm")

    return IngestResult(
        identity=identity,
        message=message,
        body=body,
        event=event,
        media=[IngestedMedia.from_attachment(a) for a in message.images],
        raw_sample=raw if _should_capture(identity, status) else None,
    )


def _classify(body: ParsedBody) -> ParseStatus:
    if body.has_event_identity:
        return ParseStatus.PARSED
    if body.event_type != "unknown_alarm" or body.channel_number is not None:
        return ParseStatus.PARTIAL
    return ParseStatus.UNPARSED


def _should_capture(identity: DeviceIdentity, status: ParseStatus) -> bool:
    """Keep the raw message while the profile is unproven, or whenever it misbehaves.

    A spike in captured samples is the signal that a firmware update changed the
    format under us (spec §2.4b, monoculture risk).
    """
    if identity.capture_samples or not identity.email_profile.verified:
        return True
    return status is not ParseStatus.PARSED


def _stripped(message: ParsedMessage) -> ParsedMessage:
    """Copy of the message with body and attachments removed (face refusal path)."""
    return ParsedMessage(
        message_id=message.message_id,
        subject="",
        sender=message.sender,
        recipients=message.recipients,
        date=message.date,
        body_text="",
        attachments=[],
        raw_size=message.raw_size,
        warnings=["content refused: face-related alarm (spec 19.3)"],
    )
