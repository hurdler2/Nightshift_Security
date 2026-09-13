"""Alarm-mail ingest: MIME handling, profile parsing, event construction.

**The message bodies here are synthetic.** The real firmware format has not been
captured yet (spec §24.3), so these tests pin *behaviour* — never drop an event,
never trust the body for identity, never keep face data — rather than asserting that
any particular wording is correct. When a real message arrives, add it as a fixture
and mark the profile verified; these tests should keep passing unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from uuid import uuid4

import pytest

from app.modules.ingest.mime import parse_message, synthesize_message_id
from app.modules.ingest.pipeline import DeviceIdentity, ParseStatus, ingest
from app.modules.ingest.profiles import (
    PROVISIONAL_DAHUA,
    classify_event_label,
    parse_body,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 4096 + b"\xff\xd9"

DAHUA_BODY = """\
Alarm Event: Smart Motion Human
Alarm Input Channel No.: 3
Channel Name: Depo Arka
Alarm Device Name: SANTIYE-A-XVR
Alarm Start Time(D/M/Y H:M:S): 12/09/2026 22:14:32
IP Address: 192.168.1.108
"""


def build_mail(
    body: str = DAHUA_BODY,
    *,
    attach: bytes | None = JPEG,
    subject: str = "Alarm Message",
    message_id: str | None = "<abc123@xvr.local>",
    html: bool = False,
) -> bytes:
    message = EmailMessage()
    message["From"] = "xvr@site-a.local"
    message["To"] = "alarm@in.nightshift.app"
    message["Subject"] = subject
    message["Date"] = "Sat, 12 Sep 2026 22:14:35 +0300"
    if message_id:
        message["Message-ID"] = message_id
    if html:
        rows = "".join(f"<tr><td>{line}</td></tr>" for line in body.splitlines())
        message.set_content(f"<html><body><table>{rows}</table></body></html>", subtype="html")
    else:
        message.set_content(body)
    if attach:
        message.add_attachment(attach, maintype="image", subtype="jpeg", filename="snapshot.jpg")
    # policy.SMTP gives CRLF line endings, which is what actually goes over the wire.
    return message.as_bytes(policy=policy.SMTP)


def identity(**overrides) -> DeviceIdentity:
    defaults = {
        "device_id": uuid4(),
        "tenant_id": uuid4(),
        "site_id": uuid4(),
        "smtp_username": "dev-abc123",
    }
    defaults.update(overrides)
    return DeviceIdentity(**defaults)


class TestMime:
    def test_extracts_headers_body_and_image(self):
        parsed = parse_message(build_mail())
        assert parsed.message_id == "<abc123@xvr.local>"
        assert "Smart Motion Human" in parsed.body_text
        assert len(parsed.images) == 1
        assert parsed.images[0].size == len(JPEG)
        assert parsed.images[0].sha256

    def test_html_body_is_flattened(self):
        parsed = parse_message(build_mail(html=True))
        assert "Alarm Event: Smart Motion Human" in parsed.body_text
        assert "<td>" not in parsed.body_text

    def test_missing_message_id_is_synthesized_stably(self):
        raw = build_mail(message_id=None)
        first = parse_message(raw).message_id
        second = parse_message(raw).message_id
        assert first == second == synthesize_message_id(raw)

    def test_non_jpeg_attachment_is_not_treated_as_a_snapshot(self):
        parsed = parse_message(build_mail(attach=b"not an image at all" * 100))
        assert parsed.attachments  # it is still recorded
        assert parsed.images == []  # but not as usable media

    def test_tiny_image_is_rejected(self):
        """Firmware error stubs are a few bytes of JPEG header and nothing else."""
        parsed = parse_message(build_mail(attach=b"\xff\xd8\xff" + b"\x00" * 10))
        assert parsed.images == []

    def test_garbage_input_does_not_raise(self):
        """Recovering is fine; crashing is not. Whatever survives becomes the body."""
        parsed = parse_message(b"\x00\x01\x02 this is not an email")
        assert parsed.message_id
        assert parsed.images == []

    def test_empty_message_is_flagged(self):
        parsed = parse_message(b"")
        assert parsed.body_text == ""
        assert any("no readable text body" in w for w in parsed.warnings)

    def test_delivery_date_is_captured_for_latency_measurement(self):
        parsed = parse_message(build_mail())
        assert parsed.date is not None
        assert parsed.date.tzinfo is not None


class TestProfileParsing:
    def test_full_body(self):
        body = parse_body(DAHUA_BODY)
        assert body.event_type == "person_detected"
        assert body.channel_number == 3
        assert body.channel_name == "Depo Arka"
        assert body.device_name == "SANTIYE-A-XVR"
        assert body.device_ip == "192.168.1.108"
        assert body.occurred_at == datetime(2026, 9, 12, 22, 14, 32)
        assert body.is_complete

    def test_event_time_is_device_local_not_utc(self):
        """The recorder reports wall clock with no offset; conversion needs the site."""
        assert parse_body(DAHUA_BODY).occurred_at.tzinfo is None

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Smart Motion Human", "person_detected"),
            ("SMD Human", "person_detected"),
            ("Smart Motion Vehicle", "vehicle_detected"),
            ("Smart Motion", "motion_detected"),
            ("Tripwire", "line_crossing"),
            ("Intrusion", "zone_intrusion"),
            ("Video Loss", "video_loss"),
            ("Camera Masking", "video_tampering"),
            ("Disk Error", "storage_failure"),
            ("Something Nobody Mapped", "unknown_alarm"),
        ],
    )
    def test_label_classification(self, label, expected):
        assert classify_event_label(label, PROVISIONAL_DAHUA.event_type_map) == expected

    def test_longest_label_wins(self):
        """ "smart motion human" must not be swallowed by "smart motion"."""
        assert (
            classify_event_label("Smart Motion Human", PROVISIONAL_DAHUA.event_type_map)
            == "person_detected"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "12/09/2026 22:14:32",
            "2026-09-12 22:14:32",
            "2026/09/12 22:14:32",
            "2026_09_12_22_14_32",
        ],
    )
    def test_time_formats(self, raw):
        body = parse_body(f"Alarm Event: Video Loss\nAlarm Start Time: {raw}\n")
        assert body.occurred_at == datetime(2026, 9, 12, 22, 14, 32)

    def test_unknown_wording_reports_what_is_missing(self):
        body = parse_body("Something entirely different arrived here.")
        assert body.event_type == "unknown_alarm"
        assert body.channel_number is None
        assert set(body.missing) == {"event", "channel_number", "occurred_at"}

    def test_empty_body(self):
        assert parse_body("").event_type == "unknown_alarm"

    def test_provisional_profile_is_marked_unverified(self):
        """It must stay false until confirmed on the pilot recorder (spec §0.3)."""
        assert PROVISIONAL_DAHUA.verified is False


class TestPipeline:
    def test_happy_path(self):
        who = identity()
        result = ingest(build_mail(), who)
        assert result.status is ParseStatus.PARSED
        assert result.event.event_type == "person_detected"
        assert result.event.channel_number == 3
        assert result.event.tenant_id == who.tenant_id
        assert len(result.media) == 1
        assert result.media[0].content == JPEG

    def test_identity_comes_from_auth_not_from_the_body(self):
        """A forged device name in the body must not change ownership (spec §7.1)."""
        who = identity()
        body = DAHUA_BODY.replace("SANTIYE-A-XVR", "SOMEONE-ELSES-DVR")
        result = ingest(build_mail(body), who)
        assert result.event.device_id == who.device_id
        assert result.event.tenant_id == who.tenant_id
        assert result.body.device_name == "SOMEONE-ELSES-DVR"  # kept as a label only

    def test_unparseable_body_still_raises_an_event(self):
        """Spec §7.3: a parse failure may not cost an alarm."""
        result = ingest(build_mail("total gibberish from a firmware we do not know"), identity())
        assert result.event is not None
        assert result.event.event_type == "unknown_alarm"
        assert result.status is ParseStatus.UNPARSED
        assert result.media  # the snapshot is still usable

    def test_partial_parse_is_distinguished(self):
        result = ingest(build_mail("Alarm Event: Video Loss\n"), identity())
        assert result.status is ParseStatus.PARTIAL
        assert result.event.event_type == "video_loss"
        assert result.event.channel_number is None

    def test_unverified_profile_is_flagged_on_every_event(self):
        result = ingest(build_mail(), identity())
        assert any("unverified" in w for w in result.event.warnings)

    def test_missing_snapshot_is_flagged_but_not_fatal(self):
        result = ingest(build_mail(attach=None), identity())
        assert result.event is not None
        assert result.media == []
        assert any("no usable JPEG" in w for w in result.event.warnings)

    def test_raw_sample_is_captured_while_the_profile_is_unproven(self):
        result = ingest(build_mail(), identity())
        assert result.raw_sample is not None

    def test_verified_profile_only_captures_problem_messages(self):
        from dataclasses import replace

        verified = replace(PROVISIONAL_DAHUA, verified=True)
        who = identity(email_profile=verified, capture_samples=False)

        clean = ingest(build_mail(), who)
        assert clean.raw_sample is None

        broken = ingest(build_mail("unrecognised"), who)
        assert broken.raw_sample is not None  # firmware-change signal

    def test_dedup_key_shape(self):
        who = identity()
        result = ingest(build_mail(), who)
        assert result.event.dedup_key == (
            f"{who.tenant_id}:{who.site_id}:{who.device_id}:3:person_detected"
        )

    def test_received_at_is_utc(self):
        now = datetime(2026, 9, 12, 19, 14, 40, tzinfo=UTC)
        result = ingest(build_mail(), identity(), received_at=now)
        assert result.event.received_at == now


class TestFaceRefusal:
    """Spec §19.3: no face data is stored, at any layer."""

    FACE_BODY = (
        "Alarm Event: Face Detection\n"
        "Alarm Input Channel No.: 1\n"
        "Alarm Start Time(D/M/Y H:M:S): 12/09/2026 22:14:32\n"
    )

    def test_face_event_produces_no_event_and_no_media(self):
        result = ingest(build_mail(self.FACE_BODY), identity())
        assert result.blocked is True
        assert result.status is ParseStatus.BLOCKED
        assert result.event is None
        assert result.media == []
        assert result.raw_sample is None

    def test_face_body_and_attachments_are_not_retained(self):
        result = ingest(build_mail(self.FACE_BODY), identity())
        assert result.message.body_text == ""
        assert result.message.attachments == []
        assert "Face" not in str(result.message)

    def test_face_mentioned_anywhere_blocks_the_message(self):
        body = DAHUA_BODY + "\nExtra: face recognition candidate list attached\n"
        assert ingest(build_mail(body), identity()).blocked is True
