"""CGI client behaviour: digest auth, error mapping, snapshot validation."""

from __future__ import annotations

import httpx
import pytest
import respx

from nightshift_edge.devices.dahua.capabilities import (
    fetch_channel_titles,
    fetch_device_clock,
    fetch_device_identity,
    fetch_exposure_events,
)
from nightshift_edge.devices.dahua.cgi_client import (
    DahuaAuthError,
    DahuaCgiClient,
    DahuaCredentials,
    DahuaUnsupportedError,
    parse_kv_response,
)
from nightshift_edge.devices.dahua.snapshot import (
    SnapshotError,
    detect_snapshot_channel_base,
    fetch_snapshot,
)

HOST = "192.168.1.108"
CREDS = DahuaCredentials("nightshift", "secret-pass")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 4096 + b"\xff\xd9"


@pytest.fixture
def client():
    return DahuaCgiClient(HOST, CREDS)


class TestUrls:
    def test_cgi_bin_prefix_is_added(self, client):
        assert client.url("snapshot.cgi") == f"http://{HOST}/cgi-bin/snapshot.cgi"

    def test_explicit_path_is_kept(self, client):
        assert client.url("/cgi-bin/magicBox.cgi") == f"http://{HOST}/cgi-bin/magicBox.cgi"

    def test_non_default_port(self):
        c = DahuaCgiClient(HOST, CREDS, port=8000)
        assert c.url("snapshot.cgi").startswith(f"http://{HOST}:8000/")


class TestDigestAuth:
    @respx.mock
    async def test_completes_the_digest_challenge(self, client):
        route = respx.get(f"http://{HOST}/cgi-bin/magicBox.cgi")
        route.side_effect = [
            httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Digest realm="Login to XVR", qop="auth", '
                    'nonce="dcd98b7102dd2f0e", opaque="5ccc069c"'
                },
            ),
            httpx.Response(200, text="type=XVR5108HS-I3\r\n"),
        ]
        text = await client.get_text("magicBox.cgi", {"action": "getDeviceType"})
        assert parse_kv_response(text) == {"type": "XVR5108HS-I3"}
        assert route.call_count == 2
        authorization = route.calls[1].request.headers["authorization"]
        assert authorization.startswith("Digest ")
        assert "secret-pass" not in authorization  # hashed, never sent in the clear
        await client.aclose()

    @respx.mock
    async def test_rejected_credentials_raise_auth_error(self, client):
        respx.get(f"http://{HOST}/cgi-bin/magicBox.cgi").mock(return_value=httpx.Response(401))
        with pytest.raises(DahuaAuthError):
            await client.get_text("magicBox.cgi", {"action": "getDeviceType"})
        await client.aclose()

    @respx.mock
    async def test_missing_endpoint_is_unsupported_not_fatal(self, client):
        respx.get(f"http://{HOST}/cgi-bin/eventManager.cgi").mock(return_value=httpx.Response(400))
        with pytest.raises(DahuaUnsupportedError):
            await client.get_text("eventManager.cgi", {"action": "getCaps"})
        await client.aclose()

    @respx.mock
    async def test_error_body_is_treated_as_unsupported(self, client):
        respx.get(f"http://{HOST}/cgi-bin/eventManager.cgi").mock(
            return_value=httpx.Response(200, text="Error\r\n")
        )
        with pytest.raises(DahuaUnsupportedError):
            await client.get_text("eventManager.cgi", {"action": "getExposureEvents"})
        await client.aclose()

    @respx.mock
    async def test_password_never_appears_in_error_text(self, client):
        respx.get(f"http://{HOST}/cgi-bin/magicBox.cgi").mock(return_value=httpx.Response(401))
        with pytest.raises(DahuaAuthError) as excinfo:
            await client.get_text("magicBox.cgi", {"action": "getDeviceType"})
        assert "secret-pass" not in str(excinfo.value)
        await client.aclose()


class TestSnapshot:
    @respx.mock
    async def test_returns_jpeg(self, client):
        respx.get(f"http://{HOST}/cgi-bin/snapshot.cgi", params={"channel": "1"}).mock(
            return_value=httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})
        )
        snap = await fetch_snapshot(client, 1)
        assert snap.size == len(JPEG)
        assert snap.vendor_channel == 1
        await client.aclose()

    @respx.mock
    async def test_html_error_page_is_not_accepted_as_a_snapshot(self, client):
        respx.get(f"http://{HOST}/cgi-bin/snapshot.cgi").mock(
            return_value=httpx.Response(200, content=b"<html>error</html>")
        )
        with pytest.raises(SnapshotError):
            await fetch_snapshot(client, 1)
        await client.aclose()

    @respx.mock
    async def test_channel_base_1_detected(self, client):
        route = respx.get(f"http://{HOST}/cgi-bin/snapshot.cgi")
        route.mock(return_value=httpx.Response(200, content=JPEG))
        base, attempts = await detect_snapshot_channel_base(client)
        assert base == 1
        assert attempts[1][0] is True
        await client.aclose()

    @respx.mock
    async def test_channel_base_0_detected_when_channel_1_fails(self, client):
        """Some firmwares are 0-based despite the v2.63 docs (spec §3.1)."""

        def responder(request: httpx.Request) -> httpx.Response:
            channel = request.url.params.get("channel")
            if channel == "0":
                return httpx.Response(200, content=JPEG)
            return httpx.Response(400, text="Error")

        respx.get(f"http://{HOST}/cgi-bin/snapshot.cgi").mock(side_effect=responder)
        base, _ = await detect_snapshot_channel_base(client)
        assert base == 0
        await client.aclose()

    @respx.mock
    async def test_base_stays_unknown_when_nothing_works(self, client):
        """Never guess a base — an unknown mapping must surface as None (spec §0.3)."""
        respx.get(f"http://{HOST}/cgi-bin/snapshot.cgi").mock(return_value=httpx.Response(500))
        base, _ = await detect_snapshot_channel_base(client)
        assert base is None
        await client.aclose()


class TestCapabilityDiscovery:
    @respx.mock
    async def test_identity_survives_missing_endpoints(self, client):
        """Old firmwares 404 on some magicBox actions; the rest must still be read."""

        def responder(request: httpx.Request) -> httpx.Response:
            action = request.url.params.get("action")
            bodies = {
                "getVendor": "vendor=Dahua\r\n",
                "getDeviceType": "type=XVR5108HS-I3\r\n",
                "getSerialNo": "sn=7L03CE2PAZ1B4F5\r\n",
                "getSoftwareVersion": "version=4.001.0000000.4\r\nBuildDate=2022-06-20\r\n",
            }
            if action in bodies:
                return httpx.Response(200, text=bodies[action])
            return httpx.Response(404)

        respx.get(f"http://{HOST}/cgi-bin/magicBox.cgi").mock(side_effect=responder)
        respx.get(f"http://{HOST}/cgi-bin/configManager.cgi").mock(
            return_value=httpx.Response(
                200,
                text="table.ChannelTitle[0].Name=Depo\r\ntable.ChannelTitle[4].Name=Kapi\r\n",
            )
        )
        identity = await fetch_device_identity(client)
        assert identity.model == "XVR5108HS-I3"
        assert identity.serial_number == "7L03CE2PAZ1B4F5"
        assert identity.firmware_version == "4.001.0000000.4"
        assert identity.channel_count == 5
        await client.aclose()

    @respx.mock
    async def test_channel_titles(self, client):
        respx.get(f"http://{HOST}/cgi-bin/configManager.cgi").mock(
            return_value=httpx.Response(
                200,
                text="table.ChannelTitle[0].Name=Depo Arka\r\ntable.ChannelTitle[1].Name=Giris\r\n",
            )
        )
        assert await fetch_channel_titles(client) == {0: "Depo Arka", 1: "Giris"}
        await client.aclose()

    @respx.mock
    async def test_exposure_events_missing_endpoint_returns_empty(self, client):
        respx.get(f"http://{HOST}/cgi-bin/eventManager.cgi").mock(return_value=httpx.Response(404))
        assert await fetch_exposure_events(client) == []
        await client.aclose()

    @respx.mock
    async def test_exposure_events_parsed(self, client):
        respx.get(f"http://{HOST}/cgi-bin/eventManager.cgi").mock(
            return_value=httpx.Response(
                200,
                text="events[0]=VideoMotion\r\nevents[1]=SmartMotionHuman\r\n"
                "events[2]=SmartMotionVehicle\r\n",
            )
        )
        codes = await fetch_exposure_events(client)
        assert "SmartMotionHuman" in codes
        assert "SmartMotionVehicle" in codes
        await client.aclose()

    @respx.mock
    async def test_clock_skew_is_measured(self, client):
        from datetime import datetime

        respx.get(f"http://{HOST}/cgi-bin/global.cgi").mock(
            return_value=httpx.Response(200, text="result=2026-09-12 22:00:00\r\n")
        )
        respx.get(f"http://{HOST}/cgi-bin/configManager.cgi").mock(
            return_value=httpx.Response(
                200, text="table.NTP.Enable=false\r\ntable.NTP.Address=pool.ntp.org\r\n"
            )
        )
        clock = await fetch_device_clock(client, now=datetime(2026, 9, 12, 22, 5, 0))
        assert clock.device_time == datetime(2026, 9, 12, 22, 0, 0)
        assert clock.skew_seconds == 300
        assert clock.ntp_enabled is False
        assert clock.ntp_server == "pool.ntp.org"
        await client.aclose()
