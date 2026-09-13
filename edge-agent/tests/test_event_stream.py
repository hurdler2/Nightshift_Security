"""Attach stream lifecycle: connect, parse, watchdog, reconnect."""

from __future__ import annotations

import httpx
import pytest
import respx

from nightshift_edge.devices.dahua.cgi_client import DahuaCgiClient, DahuaCredentials
from nightshift_edge.devices.dahua.event_stream import DahuaEventStream, collect_events

HOST = "192.168.1.108"
CREDS = DahuaCredentials("nightshift", "secret-pass")
ATTACH_URL = f"http://{HOST}/cgi-bin/eventManager.cgi"
MULTIPART = "multipart/x-mixed-replace;boundary=myboundary"


@pytest.fixture
def client():
    return DahuaCgiClient(HOST, CREDS)


class TestCollectEvents:
    @respx.mock
    async def test_reads_events_from_a_live_stream(self, client, fixture_bytes):
        respx.get(ATTACH_URL).mock(
            return_value=httpx.Response(
                200,
                headers={"Content-Type": MULTIPART},
                content=fixture_bytes("smd_human_stream.bin"),
            )
        )
        events = await collect_events(client, duration_seconds=0.4)
        assert [e.code for e in events[:4]] == [
            "SmartMotionHuman",
            "Heartbeat",
            "SmartMotionHuman",
            "SmartMotionVehicle",
        ]
        await client.aclose()

    @respx.mock
    async def test_attach_query_is_sent_unencoded(self, client, fixture_bytes):
        route = respx.get(ATTACH_URL).mock(
            return_value=httpx.Response(
                200,
                headers={"Content-Type": MULTIPART},
                content=fixture_bytes("smd_human_stream.bin"),
            )
        )
        await collect_events(client, duration_seconds=0.2)
        query = str(route.calls[0].request.url.query, "ascii")
        assert "action=attach" in query
        assert "codes=[All]" in query or "codes=%5BAll%5D" in query
        assert "heartbeat=5" in query
        await client.aclose()

    @respx.mock
    async def test_narrowed_code_filter(self, client, fixture_bytes):
        route = respx.get(ATTACH_URL).mock(
            return_value=httpx.Response(
                200,
                headers={"Content-Type": MULTIPART},
                content=fixture_bytes("smd_human_stream.bin"),
            )
        )
        await collect_events(
            client,
            duration_seconds=0.2,
            codes=["SmartMotionHuman", "SmartMotionVehicle"],
        )
        query = str(route.calls[0].request.url.query, "ascii")
        assert "SmartMotionHuman" in query
        await client.aclose()

    @respx.mock
    async def test_auth_failure_backs_off_instead_of_hammering_the_device(self, client):
        """A tight retry loop would lock the XVR service account out."""
        route = respx.get(ATTACH_URL).mock(return_value=httpx.Response(401))
        events = await collect_events(client, duration_seconds=0.4)
        assert events == []
        assert route.call_count <= 2
        await client.aclose()

    @respx.mock
    async def test_empty_stream_yields_nothing(self, client):
        respx.get(ATTACH_URL).mock(
            return_value=httpx.Response(200, headers={"Content-Type": MULTIPART}, content=b"")
        )
        assert await collect_events(client, duration_seconds=0.3) == []
        await client.aclose()


class TestStreamStats:
    @respx.mock
    async def test_counts_events_and_heartbeats(self, client, fixture_bytes):
        respx.get(ATTACH_URL).mock(
            return_value=httpx.Response(
                200,
                headers={"Content-Type": MULTIPART},
                content=fixture_bytes("smd_human_stream.bin"),
            )
        )
        stream = DahuaEventStream(client)
        seen = []
        async for event in stream.listen():
            seen.append(event)
            if len(seen) == 4:
                break
        assert stream.stats.events == 3
        assert stream.stats.heartbeats == 1
        assert stream.stats.connects == 1
        await client.aclose()

    def test_state_change_callback(self, client):
        states: list[bool] = []
        stream = DahuaEventStream(client, on_state_change=states.append)
        stream._set_connected(True)
        stream._set_connected(True)  # no duplicate notification
        stream._set_connected(False)
        assert states == [True, False]
