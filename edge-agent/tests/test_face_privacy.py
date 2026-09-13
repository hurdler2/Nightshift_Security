"""Face events must never leave the gateway (spec §47).

The pilot recorder (DH-XVR5108HS-I3/T, WizSense) can run recorder-side face
detection. V1 does no face processing at all, so `codes=[All]` must not turn into
silent ingestion of face payloads.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nightshift_edge.devices.dahua.adapter import DahuaCgiAdapter
from nightshift_edge.devices.dahua.cgi_client import DahuaCgiClient, DahuaCredentials
from nightshift_edge.devices.dahua.event_parser import (
    is_blocked_code,
    normalize_event,
    parse_event_payload,
)

HOST = "192.168.1.108"
CREDS = DahuaCredentials("nightshift", "secret-pass")
ATTACH_URL = f"http://{HOST}/cgi-bin/eventManager.cgi"
MULTIPART = "multipart/x-mixed-replace;boundary=myboundary"

FACE_PAYLOAD = (
    'Code=FaceDetection;action=Start;index=0;data={"Object":{"ObjectType":"HumanFace",'
    '"BoundingBox":[3184,2872,4088,5296],"FaceID":"7731"},"Candidates":[{"Person":'
    '{"Name":"unknown"}}]}'
)


def _part(payload: bytes) -> bytes:
    return (
        b"--myboundary\r\nContent-Type: text/plain\r\nContent-Length: "
        + str(len(payload)).encode()
        + b"\r\n\r\n"
        + payload
        + b"\r\n"
    )


class TestBlockList:
    @pytest.mark.parametrize(
        "code",
        [
            "FaceDetection",
            "FaceRecognition",
            "FaceComparison",
            "FaceAnalysis",
            "FaceCapture",
            "HumanFaceDetect",
            "FaceSomethingNewInFirmware",  # prefix rule catches future variants
        ],
    )
    def test_face_codes_are_blocked(self, code):
        assert is_blocked_code(code)

    @pytest.mark.parametrize(
        "code", ["SmartMotionHuman", "SmartMotionVehicle", "VideoLoss", "CrossLineDetection"]
    )
    def test_normal_codes_are_not_blocked(self, code):
        assert not is_blocked_code(code)


class TestNormalization:
    def test_face_payload_is_stripped(self):
        """Defence in depth: even if normalized, no face data survives."""
        event = normalize_event(parse_event_payload(FACE_PAYLOAD))
        assert event.event_type == "blocked:face_event"
        assert event.raw_data == {}
        serialized = str(event.to_dict())
        assert "BoundingBox" not in serialized
        assert "FaceID" not in serialized
        assert "7731" not in serialized


class TestAdapterStream:
    @respx.mock
    async def test_face_events_never_reach_the_consumer(self):
        stream = _part(FACE_PAYLOAD.encode()) + _part(b"Code=SmartMotionHuman;action=Start;index=0")
        respx.get(ATTACH_URL).mock(
            return_value=httpx.Response(200, headers={"Content-Type": MULTIPART}, content=stream)
        )
        client = DahuaCgiClient(HOST, CREDS)
        adapter = DahuaCgiAdapter(HOST, CREDS, client=client)

        received = []
        async for event in adapter.events():
            received.append(event)
            break  # the human event is the first one that may be delivered

        assert [e.vendor_event_code for e in received] == ["SmartMotionHuman"]
        await adapter.aclose()


class TestProbeReport:
    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        from nightshift_edge.devices.dahua import probe as probe_module

        async def fake_tcp(host: str, port: int, timeout: float = 5.0):
            return True, "open"

        monkeypatch.setattr(probe_module.DahuaProbe, "_check_tcp", staticmethod(fake_tcp))
        monkeypatch.setattr(probe_module, "ffprobe_available", lambda: False)

    @respx.mock
    async def test_report_records_the_finding_without_storing_face_data(self):
        from nightshift_edge.devices.dahua.probe import DahuaProbe
        from tests.test_probe import mock_device, options

        mock_device(event_stream=_part(FACE_PAYLOAD.encode()))
        report = await DahuaProbe(options()).run()

        assert "FaceDetection" in report.observed_event_codes
        assert any("face detection" in w for w in report.warnings)
        stored = [e for e in report.observed_events if e["code"] == "FaceDetection"]
        assert stored and stored[0]["data"] == {}
        assert "7731" not in str(report.to_dict())
