"""Vendor event -> Nightshift event normalization and channel mapping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nightshift_edge.devices.dahua.event_parser import (
    DahuaEventStreamParser,
    normalize_event,
    parse_event_payload,
)
from nightshift_edge.devices.dahua.models import EventAction


def norm(payload: str, **kwargs):
    return normalize_event(parse_event_payload(payload), **kwargs)


class TestEventTypeMapping:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("SmartMotionHuman", "person_detected"),
            ("SmartMotionVehicle", "vehicle_detected"),
            ("VideoMotion", "motion_detected"),
            ("VideoLoss", "video_loss"),
            ("VideoBlind", "video_blind"),
            ("StorageNotExist", "storage_not_exist"),
            ("StorageFailure", "storage_failure"),
            ("StorageLowSpace", "storage_low_space"),
            ("CrossLineDetection", "line_crossing"),
            ("CrossRegionDetection", "zone_intrusion"),
            ("VideoAbnormalDetection", "scene_abnormal"),
        ],
    )
    def test_known_codes(self, code, expected):
        event = norm(f"Code={code};action=Start;index=0")
        assert event.event_type == expected
        assert event.is_known_code is True
        assert event.vendor == "dahua"
        assert event.vendor_event_code == code

    def test_unknown_code_is_kept_not_dropped(self):
        """Spec §12: unmapped codes must survive normalization, flagged for review."""
        event = norm("Code=SomeFutureFirmwareEvent;action=Start;index=0")
        assert event.event_type == "vendor_unknown:SomeFutureFirmwareEvent"
        assert event.is_known_code is False
        assert event.vendor_event_code == "SomeFutureFirmwareEvent"


class TestActionMapping:
    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("Start", EventAction.OPENED),
            ("Stop", EventAction.CLOSED),
            ("Pulse", EventAction.PULSE),
            ("start", EventAction.OPENED),
        ],
    )
    def test_known_actions(self, action, expected):
        event = norm(f"Code=SmartMotionHuman;action={action};index=0")
        assert event.action is expected
        assert event.is_known_action is True

    def test_unknown_action_is_kept(self):
        event = norm("Code=SmartMotionHuman;action=Flicker;index=0")
        assert event.action is EventAction.UNKNOWN
        assert event.is_known_action is False
        assert event.vendor_action == "Flicker"


class TestChannelMapping:
    def test_zero_based_device_index_becomes_one_based_channel(self):
        event = norm("Code=SmartMotionHuman;action=Start;index=0", event_index_base=0)
        assert event.vendor_channel_index == 0
        assert event.logical_channel == 1

    def test_one_based_device_index(self):
        event = norm("Code=SmartMotionHuman;action=Start;index=1", event_index_base=1)
        assert event.logical_channel == 1

    def test_explicit_channel_map_wins(self):
        """Onboarding may discover an irregular mapping; it overrides arithmetic."""
        event = norm(
            "Code=SmartMotionHuman;action=Start;index=6",
            event_index_base=0,
            channel_map={6: 2},
        )
        assert event.logical_channel == 2

    def test_missing_index_yields_no_channel(self):
        event = norm("Code=StorageFailure;action=Start")
        assert event.vendor_channel_index is None
        assert event.logical_channel is None


class TestPayloadEnrichment:
    def test_object_type_from_stream_fixture(self, fixture_bytes):
        parser = DahuaEventStreamParser()
        events = parser.feed(fixture_bytes("smd_human_stream.bin"))
        human = normalize_event(events[0])
        vehicle = normalize_event(events[3])
        assert human.detected_object_type == "Human"
        assert vehicle.detected_object_type == "Vehicle"

    def test_serializes_to_cloud_payload(self):
        occurred = datetime(2026, 9, 12, 22, 14, 32, tzinfo=UTC)
        payload = norm("Code=SmartMotionHuman;action=Start;index=0", occurred_at=occurred).to_dict()
        assert payload["event_type"] == "person_detected"
        assert payload["vendor_event_code"] == "SmartMotionHuman"
        assert payload["action"] == "opened"
        assert payload["occurred_at"] == "2026-09-12T22:14:32+00:00"
        assert payload["logical_channel"] == 1

    def test_cloud_payload_carries_no_credentials(self):
        payload = norm("Code=SmartMotionHuman;action=Start;index=0").to_dict()
        flat = str(payload).lower()
        assert "password" not in flat
        assert "rtsp://" not in flat
