"""End-to-end probe behaviour against a simulated XVR.

The point of these tests is honesty: the report must never claim a capability that was
not observed (spec §0.3), and it must call out the blockers that stop PHASE 2.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nightshift_edge.devices.dahua import probe as probe_module
from nightshift_edge.devices.dahua.cgi_client import DahuaCredentials
from nightshift_edge.devices.dahua.models import Capability
from nightshift_edge.devices.dahua.probe import DahuaProbe, ProbeOptions, summarize

HOST = "192.168.1.108"
CREDS = DahuaCredentials("nightshift", "secret-pass")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 4096 + b"\xff\xd9"
MULTIPART = "multipart/x-mixed-replace;boundary=myboundary"

MAGICBOX_BODIES = {
    "getVendor": "vendor=Dahua\r\n",
    "getDeviceType": "type=XVR5108HS-I3\r\n",
    "getSerialNo": "sn=7L03CE2PAZ1B4F5\r\n",
    "getSoftwareVersion": "version=4.001.0000000.4\r\nBuildDate=2022-06-20\r\n",
    "getSystemInfo": "deviceType=XVR5108HS-I3\r\nserialNumber=7L03CE2PAZ1B4F5\r\n",
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The probe's TCP reachability check and ffprobe are not available in tests."""

    async def fake_tcp(host: str, port: int, timeout: float = 5.0):
        return True, "open"

    monkeypatch.setattr(DahuaProbe, "_check_tcp", staticmethod(fake_tcp))
    monkeypatch.setattr(probe_module, "ffprobe_available", lambda: False)


def mock_device(*, event_stream: bytes, channels: int = 4, snapshot_ok: bool = True) -> None:
    def magicbox(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if action in MAGICBOX_BODIES:
            return httpx.Response(200, text=MAGICBOX_BODIES[action])
        return httpx.Response(404)

    def config_manager(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("name")
        if name == "ChannelTitle":
            return httpx.Response(
                200,
                text="".join(
                    f"table.ChannelTitle[{i}].Name=Kamera{i + 1}\r\n" for i in range(channels)
                ),
            )
        if name == "NTP":
            return httpx.Response(200, text="table.NTP.Enable=true\r\n")
        return httpx.Response(404)

    def event_manager(request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        if action == "getCaps":
            return httpx.Response(200, text="caps.SmartMotion=true\r\n")
        if action == "getExposureEvents":
            return httpx.Response(
                200,
                text="events[0]=VideoMotion\r\nevents[1]=SmartMotionHuman\r\n",
            )
        return httpx.Response(200, headers={"Content-Type": MULTIPART}, content=event_stream)

    respx.get(f"http://{HOST}/cgi-bin/magicBox.cgi").mock(side_effect=magicbox)
    respx.get(f"http://{HOST}/cgi-bin/configManager.cgi").mock(side_effect=config_manager)
    respx.get(f"http://{HOST}/cgi-bin/eventManager.cgi").mock(side_effect=event_manager)
    respx.get(f"http://{HOST}/cgi-bin/global.cgi").mock(return_value=httpx.Response(404))
    respx.get(f"http://{HOST}/cgi-bin/storageDevice.cgi").mock(return_value=httpx.Response(404))
    respx.get(f"http://{HOST}/cgi-bin/snapshot.cgi").mock(
        return_value=httpx.Response(200, content=JPEG) if snapshot_ok else httpx.Response(500)
    )


def options(**overrides) -> ProbeOptions:
    defaults = {
        "host": HOST,
        "credentials": CREDS,
        "channels": 4,
        "listen_seconds": 0.3,
        "test_playback": False,
    }
    defaults.update(overrides)
    return ProbeOptions(**defaults)


class TestHappyPath:
    @respx.mock
    async def test_full_report(self, fixture_bytes):
        mock_device(event_stream=fixture_bytes("smd_human_stream.bin"))
        report = await DahuaProbe(options()).run()

        assert report.reachable is Capability.SUPPORTED
        assert report.digest_auth is Capability.SUPPORTED
        assert report.identity.model == "XVR5108HS-I3"
        assert report.identity.firmware_version == "4.001.0000000.4"
        assert report.identity.channel_count == 4
        assert report.snapshot is Capability.SUPPORTED
        assert report.snapshot_channel_base == 1
        assert report.event_stream is Capability.SUPPORTED
        assert report.smart_motion_human is Capability.SUPPORTED
        assert report.smart_motion_vehicle is Capability.SUPPORTED
        assert report.errors == []

    @respx.mock
    async def test_every_channel_is_snapshotted(self, fixture_bytes):
        mock_device(event_stream=fixture_bytes("smd_human_stream.bin"))
        report = await DahuaProbe(options()).run()
        assert [c.logical_channel for c in report.channels] == [1, 2, 3, 4]
        assert all(c.snapshot_ok for c in report.channels)
        assert [c.snapshot_channel for c in report.channels] == [1, 2, 3, 4]

    @respx.mock
    async def test_index_base_pinned_by_trigger_channel(self, fixture_bytes):
        """Operator says "I triggered channel 1"; index 0 arrives -> base 0."""
        mock_device(event_stream=fixture_bytes("smd_human_stream.bin"))
        report = await DahuaProbe(options(trigger_channel=1)).run()
        assert report.event_index_base == 0

    @respx.mock
    async def test_report_serializes_to_json_safe_dict(self, fixture_bytes):
        import json

        mock_device(event_stream=fixture_bytes("smd_human_stream.bin"))
        report = await DahuaProbe(options()).run()
        payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
        assert payload["smart_motion_human"] == "SUPPORTED"
        assert payload["snapshot_channel_base"] == 1
        assert "secret-pass" not in json.dumps(payload)


class TestHonesty:
    @respx.mock
    async def test_rtsp_stays_unknown_without_ffprobe(self, fixture_bytes):
        """No verification tool -> UNKNOWN, never a cheerful False or True."""
        mock_device(event_stream=fixture_bytes("smd_human_stream.bin"))
        report = await DahuaProbe(options()).run()
        assert report.rtsp is Capability.UNKNOWN
        assert report.rtsp_playback is Capability.UNKNOWN
        assert any("ffprobe" in w for w in report.warnings)

    @respx.mock
    async def test_missing_human_event_is_a_blocker(self, fixture_bytes):
        mock_device(event_stream=fixture_bytes("no_content_length_stream.bin"))
        report = await DahuaProbe(options()).run()
        assert report.smart_motion_human is Capability.UNSUPPORTED
        assert any("SmartMotionHuman" in e for e in report.errors)
        assert "FAIL" in summarize(report)

    @respx.mock
    async def test_silent_stream_is_reported_as_failure(self):
        mock_device(event_stream=b"")
        report = await DahuaProbe(options()).run()
        assert report.event_stream is Capability.UNSUPPORTED
        assert any("attach stream" in e or "no data" in e for e in report.errors)

    @respx.mock
    async def test_snapshot_failure_does_not_abort_the_probe(self, fixture_bytes):
        mock_device(event_stream=fixture_bytes("smd_human_stream.bin"), snapshot_ok=False)
        report = await DahuaProbe(options()).run()
        assert report.snapshot is Capability.UNSUPPORTED
        assert report.snapshot_channel_base is None
        # the event stream was still probed
        assert report.smart_motion_human is Capability.SUPPORTED

    async def test_unreachable_host_short_circuits(self, monkeypatch):
        async def fake_tcp(host: str, port: int, timeout: float = 5.0):
            return False, "connection refused"

        monkeypatch.setattr(DahuaProbe, "_check_tcp", staticmethod(fake_tcp))
        report = await DahuaProbe(options()).run()
        assert report.reachable is Capability.UNSUPPORTED
        assert report.digest_auth is Capability.UNKNOWN
        assert any("unreachable" in e for e in report.errors)

    @respx.mock
    async def test_bad_credentials_stop_before_hammering_endpoints(self):
        respx.get(f"http://{HOST}/cgi-bin/magicBox.cgi").mock(return_value=httpx.Response(401))
        report = await DahuaProbe(options()).run()
        assert report.digest_auth is Capability.UNSUPPORTED
        assert report.snapshot is Capability.UNKNOWN
        assert report.event_stream is Capability.UNKNOWN


class TestSummary:
    @respx.mock
    async def test_summary_lists_observed_codes(self, fixture_bytes):
        mock_device(event_stream=fixture_bytes("smd_human_stream.bin"))
        report = await DahuaProbe(options()).run()
        text = summarize(report)
        assert "SmartMotionHuman" in text
        assert "XVR5108HS-I3" in text
        assert "PASS" in text

    def test_example_commands_contain_no_real_password(self):
        commands = probe_module.example_commands(HOST, "nightshift")
        assert all("secret-pass" not in c for c in commands)
        assert any("eventManager.cgi?action=attach" in c for c in commands)
