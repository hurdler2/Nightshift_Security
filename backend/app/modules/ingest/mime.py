"""RFC 5322 message parsing for DVR alarm mail (spec §7.3).

Deliberately tolerant: a recorder is not a well-behaved mail client, and a malformed
part must never cost us an alarm. Anything we cannot read becomes a warning, not an
exception.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

#: JPEG SOI marker. Attachments are validated by content, not by filename.
JPEG_MAGIC = b"\xff\xd8\xff"
#: Smaller than this is a firmware error stub, not a usable snapshot.
MIN_IMAGE_BYTES = 1024
#: Refuse to buffer more than this from one message.
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024


@dataclass(slots=True)
class Attachment:
    filename: str | None
    content_type: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def is_jpeg(self) -> bool:
        return self.size >= MIN_IMAGE_BYTES and self.content.startswith(JPEG_MAGIC)


@dataclass(slots=True)
class ParsedMessage:
    """Structural view of one alarm mail, before any vendor-specific parsing."""

    message_id: str
    subject: str
    sender: str
    recipients: tuple[str, ...]
    date: datetime | None
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    raw_size: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def images(self) -> list[Attachment]:
        """Attachments that really are JPEGs, whatever they claim to be."""
        return [a for a in self.attachments if a.is_jpeg]

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def synthesize_message_id(raw: bytes) -> str:
    """Stable id for a message whose sender omitted `Message-ID`.

    Hashing the whole message keeps redelivery idempotent (spec §8.4) without
    inventing a random id that would let the same alarm land twice.
    """
    return f"<sha256-{hashlib.sha256(raw).hexdigest()[:32]}@ingest.local>"


def parse_message(raw: bytes) -> ParsedMessage:
    """Parse raw RFC 5322 bytes into a `ParsedMessage`. Never raises on bad input."""
    try:
        message = message_from_bytes(raw, policy=policy.default)
    except Exception:  # malformed mail must not kill the pipeline
        return ParsedMessage(
            message_id=synthesize_message_id(raw),
            subject="",
            sender="",
            recipients=(),
            date=None,
            body_text="",
            raw_size=len(raw),
            warnings=["message could not be parsed as RFC 5322"],
        )

    parsed = ParsedMessage(
        message_id=_header(message, "Message-ID") or synthesize_message_id(raw),
        subject=_header(message, "Subject"),
        sender=_header(message, "From"),
        recipients=tuple(value for key in ("To", "Cc") if (value := _header(message, key))),
        date=_parse_date(message),
        body_text="",
        raw_size=len(raw),
    )

    body_parts: list[str] = []
    total_attachment_bytes = 0

    for part in _walk(message, parsed):
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()

        if content_type.startswith("text/") and disposition != "attachment":
            body_parts.append(_decode_text(part, parsed))
            continue

        payload = _decode_bytes(part, parsed)
        if payload is None:
            continue
        total_attachment_bytes += len(payload)
        if total_attachment_bytes > MAX_ATTACHMENT_BYTES:
            parsed.warn("attachments exceeded the size cap; remainder dropped")
            break
        parsed.attachments.append(
            Attachment(
                filename=part.get_filename(),
                content_type=content_type,
                content=payload,
            )
        )

    parsed.body_text = "\n".join(p for p in body_parts if p).strip()
    if not parsed.body_text:
        parsed.warn("message carried no readable text body")
    return parsed


def _walk(message: EmailMessage, parsed: ParsedMessage):
    try:
        yield from (message.walk() if message.is_multipart() else [message])
    except Exception:  # broken MIME tree: keep whatever we already collected
        parsed.warn("MIME tree could not be walked completely")


def _header(message: EmailMessage, name: str) -> str:
    try:
        value = message.get(name)
    except Exception:  # undecodable header
        return ""
    return str(value).strip() if value else ""


def _parse_date(message: EmailMessage) -> datetime | None:
    """The `Date` header is the *delivery* clock, not the event clock.

    It is kept for latency measurement only; event time comes from the body, which
    reflects the recorder's own clock (spec §5.1 requires NTP for exactly this reason).
    """
    raw = _header(message, "Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decode_text(part: EmailMessage, parsed: ParsedMessage) -> str:
    try:
        content = part.get_content()
    except Exception:  # unknown charset - fall back to raw bytes
        payload = part.get_payload(decode=True)
        if not payload:
            parsed.warn("a text part could not be decoded")
            return ""
        content = payload.decode("utf-8", "replace")
    if not isinstance(content, str):
        return ""
    if part.get_content_subtype() == "html":
        return _strip_html(content)
    return content


def _decode_bytes(part: EmailMessage, parsed: ParsedMessage) -> bytes | None:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # broken transfer encoding
        parsed.warn("an attachment could not be decoded")
        return None
    if not payload:
        return None
    return payload


def _strip_html(html: str) -> str:
    """Crude tag strip. Some firmwares send the alarm body as HTML."""
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"(?i)</td>", "  ", text)
    text = re.sub(r"<[^>]+>", "", text)
    from html import unescape

    return "\n".join(line.strip() for line in unescape(text).splitlines() if line.strip())
