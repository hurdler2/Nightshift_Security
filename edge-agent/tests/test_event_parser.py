"""Dahua attach-stream parsing, against recorded/synthetic multipart fixtures."""

from __future__ import annotations

import pytest

from nightshift_edge.devices.dahua.event_parser import (
    DahuaEventStreamParser,
    extract_object_type,
    parse_event_payload,
)
from tests.conftest import chunked


def parse_all(payload: bytes, *, chunk_size: int | None = None, boundary: str | None = None):
    parser = DahuaEventStreamParser(boundary=boundary)
    events = []
    for chunk in chunked(payload, chunk_size) if chunk_size else [payload]:
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())
    return events


class TestCanonicalStream:
    def test_parses_every_part(self, fixture_bytes):
        events = parse_all(fixture_bytes("smd_human_stream.bin"))
        codes = [(e.code, e.action, e.index) for e in events]
        assert codes == [
            ("SmartMotionHuman", "Start", 0),
            ("Heartbeat", "Pulse", 0),
            ("SmartMotionHuman", "Stop", 0),
            ("SmartMotionVehicle", "Start", 3),
        ]

    def test_json_payload_is_decoded(self, fixture_bytes):
        human = parse_all(fixture_bytes("smd_human_stream.bin"))[0]
        assert human.data["SmartMotionEnable"] is True
        assert human.data["RegionName"] == ["Region1"]
        assert human.data["object"][0]["Rect"] == [3184, 2872, 4088, 5296]

    def test_heartbeat_is_flagged(self, fixture_bytes):
        events = parse_all(fixture_bytes("smd_human_stream.bin"))
        assert [e.is_heartbeat for e in events] == [False, True, False, False]

    @pytest.mark.parametrize("chunk_size", [1, 3, 7, 16, 64, 257])
    def test_chunk_boundaries_do_not_change_the_result(self, fixture_bytes, chunk_size):
        """A payload split mid-JSON across TCP reads must parse identically."""
        payload = fixture_bytes("smd_human_stream.bin")
        whole = [(e.code, e.action, e.index, e.data) for e in parse_all(payload)]
        split = [
            (e.code, e.action, e.index, e.data) for e in parse_all(payload, chunk_size=chunk_size)
        ]
        assert split == whole

    def test_events_are_emitted_before_the_next_boundary_arrives(self, fixture_bytes):
        """Content-Length must be honoured so an alarm is not delayed by one event."""
        payload = fixture_bytes("smd_human_stream.bin")
        first_end = payload.index(b"\r\n--myboundary", 10) + 2
        parser = DahuaEventStreamParser()
        events = parser.feed(payload[:first_end])
        assert [e.code for e in events] == ["SmartMotionHuman"]


class TestFirmwareVariants:
    def test_stream_without_content_length(self, fixture_bytes):
        events = parse_all(fixture_bytes("no_content_length_stream.bin"), boundary="myboundary")
        assert [e.code for e in events] == ["VideoMotion", "VideoLoss", "StorageLowSpace"]
        assert events[2].data["SpaceLimit"] == 10

    def test_quirky_firmware(self, fixture_bytes):
        """No space after Content-Length, a missing blank separator, bare keepalives."""
        events = parse_all(fixture_bytes("quirky_firmware_stream.bin"), boundary="myboundary")
        codes = [e.code for e in events]
        assert codes == ["SmartMotionHuman", "FaceRecognitionQuirk", "VideoLoss"]

    @pytest.mark.parametrize("chunk_size", [1, 5, 32])
    def test_quirky_firmware_is_chunk_stable(self, fixture_bytes, chunk_size):
        payload = fixture_bytes("quirky_firmware_stream.bin")
        assert [
            e.code for e in parse_all(payload, chunk_size=chunk_size, boundary="myboundary")
        ] == [e.code for e in parse_all(payload, boundary="myboundary")]


class TestBoundaryHandling:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("multipart/x-mixed-replace; boundary=myboundary", "myboundary"),
            ('multipart/x-mixed-replace;boundary="my-boundary"', "my-boundary"),
            ("multipart/x-mixed-replace", None),
            (None, None),
        ],
    )
    def test_boundary_from_content_type(self, header, expected):
        assert DahuaEventStreamParser.boundary_from_content_type(header) == expected

    def test_buffer_overflow_resyncs_instead_of_growing(self):
        parser = DahuaEventStreamParser(boundary="myboundary")
        parser.max_buffer_bytes = 512
        parser.feed(b"--myboundary\r\nContent-Type: text/plain\r\nContent-Length: 9999\r\n\r\n")
        parser.feed(b"x" * 600)
        payload = b"Code=SmartMotionHuman;action=Start;index=0"
        events = parser.feed(
            b"--myboundary\r\nContent-Type: text/plain\r\n"
            b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
        )
        assert [e.code for e in events] == ["SmartMotionHuman"]


class TestPayloadParsing:
    def test_payload_without_data(self):
        event = parse_event_payload("Code=VideoLoss;action=Start;index=4")
        assert (event.code, event.action, event.index, event.data) == (
            "VideoLoss",
            "Start",
            4,
            {},
        )

    def test_semicolons_inside_json_are_not_field_separators(self):
        event = parse_event_payload(
            'Code=VideoMotion;action=Start;index=1;data={"Note":"a;b;c","Id":7}'
        )
        assert event.index == 1
        assert event.data == {"Note": "a;b;c", "Id": 7}

    def test_invalid_json_is_kept_not_dropped(self):
        event = parse_event_payload("Code=NewFile;action=Pulse;index=0;data={not json}")
        assert event.code == "NewFile"
        assert event.data["_unparsed"] == "{not json}"

    def test_missing_index_is_none(self):
        event = parse_event_payload("Code=StorageFailure;action=Start")
        assert event.index is None

    @pytest.mark.parametrize("text", ["", "   ", "Heartbeat", "random noise"])
    def test_non_event_payloads_are_ignored(self, text):
        assert parse_event_payload(text) is None


class TestObjectType:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"Object": {"ObjectType": "Human"}}, "Human"),
            ({"object": [{"ObjectType": "Vehicle"}]}, "Vehicle"),
            ({"Objects": [{"Type": "NonMotor"}]}, "NonMotor"),
            ({"ObjectTypes": ["Human", "Vehicle"]}, "Human"),
            ({"SmartMotionEnable": True}, None),
            ({}, None),
        ],
    )
    def test_extract_object_type(self, data, expected):
        assert extract_object_type(data) == expected
