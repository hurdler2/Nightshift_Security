"""RTSP live / playback URL construction and clip windows (spec §4, §5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from nightshift_edge.devices.dahua.cgi_client import DahuaCredentials
from nightshift_edge.devices.dahua.event_stream import ALL_CODES, backoff_delay, build_attach_query
from nightshift_edge.devices.dahua.playback import (
    build_clip_window,
    build_playback_url,
    format_dahua_time,
)
from nightshift_edge.devices.dahua.rtsp import (
    StreamQuality,
    build_live_url,
    vendor_rtsp_channel,
)
from nightshift_edge.devices.dahua.snapshot import vendor_snapshot_channel

CREDS = DahuaCredentials("nightshift", "p@ss w/ord")


class TestLiveUrl:
    def test_documented_format(self):
        assert build_live_url("10.0.0.5", 1, subtype=1) == (
            "rtsp://10.0.0.5/cam/realmonitor?channel=1&subtype=1"
        )

    def test_credentials_are_percent_encoded(self):
        url = build_live_url("10.0.0.5", 2, subtype=0, credentials=CREDS)
        assert url.startswith("rtsp://nightshift:p%40ss%20w%2Ford@10.0.0.5/")
        assert "channel=2&subtype=0" in url

    def test_non_default_port(self):
        assert "10.0.0.5:5554" in build_live_url("10.0.0.5", 1, port=5554)

    @pytest.mark.parametrize(
        ("quality", "subtype"), [(StreamQuality.LOW, 1), (StreamQuality.HIGH, 0)]
    )
    def test_quality_maps_to_subtype(self, quality, subtype):
        assert quality.subtype == subtype

    def test_rejects_bad_subtype(self):
        with pytest.raises(ValueError, match="subtype"):
            build_live_url("10.0.0.5", 1, subtype=9)

    def test_safe_form_has_no_credentials(self):
        assert "@" not in build_live_url("10.0.0.5", 1)


class TestChannelBases:
    @pytest.mark.parametrize(
        ("logical", "base", "expected"), [(1, 1, 1), (1, 0, 0), (5, 1, 5), (5, 0, 4)]
    )
    def test_snapshot_channel(self, logical, base, expected):
        assert vendor_snapshot_channel(logical, base) == expected

    @pytest.mark.parametrize(("logical", "base", "expected"), [(1, 1, 1), (8, 1, 8), (1, 0, 0)])
    def test_rtsp_channel(self, logical, base, expected):
        assert vendor_rtsp_channel(logical, base) == expected


class TestClipWindow:
    def test_default_window_is_t_minus_10_to_t_plus_20(self):
        moment = datetime(2026, 9, 12, 22, 10, 10)
        window = build_clip_window(moment)
        assert window.start == datetime(2026, 9, 12, 22, 10, 0)
        assert window.end == datetime(2026, 9, 12, 22, 10, 30)
        assert window.duration_seconds == 30

    def test_configurable_padding(self):
        window = build_clip_window(
            datetime(2026, 9, 12, 22, 10, 10), pre_event_seconds=5, post_event_seconds=5
        )
        assert window.duration_seconds == 10

    def test_aware_timestamp_is_converted_to_device_local(self):
        utc = datetime(2026, 9, 12, 21, 10, 10, tzinfo=UTC)
        tz = timezone(timedelta(hours=1))  # Africa/Algiers offset
        window = build_clip_window(utc, device_timezone=tz)
        assert window.start == datetime(2026, 9, 12, 22, 10, 0)
        assert window.start.tzinfo is None

    def test_end_is_clamped_to_now(self):
        """The XVR has no footage from the future; an unclamped end returns nothing."""
        moment = datetime(2026, 9, 12, 22, 10, 10)
        window = build_clip_window(moment, now=datetime(2026, 9, 12, 22, 10, 15))
        assert window.end == datetime(2026, 9, 12, 22, 10, 15)

    def test_clamping_never_inverts_the_window(self):
        moment = datetime(2026, 9, 12, 22, 10, 10)
        window = build_clip_window(moment, now=datetime(2026, 9, 12, 21, 0, 0))
        assert window.end > window.start

    def test_rejects_negative_padding(self):
        with pytest.raises(ValueError, match="non-negative"):
            build_clip_window(datetime(2026, 9, 12, 22, 10, 10), pre_event_seconds=-1)

    def test_time_format(self):
        assert format_dahua_time(datetime(2026, 9, 12, 22, 10, 0)) == "2026_09_12_22_10_00"


class TestPlaybackUrl:
    def test_documented_format(self):
        window = build_clip_window(datetime(2026, 9, 12, 22, 10, 10))
        url = build_playback_url("10.0.0.5", 1, window)
        assert url == (
            "rtsp://10.0.0.5/cam/playback?channel=1"
            "&starttime=2026_09_12_22_10_00&endtime=2026_09_12_22_10_30"
        )

    def test_with_credentials(self):
        window = build_clip_window(datetime(2026, 9, 12, 22, 10, 10))
        url = build_playback_url("10.0.0.5", 1, window, credentials=CREDS)
        assert url.startswith("rtsp://nightshift:p%40ss%20w%2Ford@")


class TestAttachQuery:
    def test_default_is_all_codes(self):
        assert build_attach_query() == "action=attach&codes=[All]&heartbeat=5"

    def test_brackets_and_commas_stay_literal(self):
        """Percent-encoding these makes some firmwares return an empty stream."""
        query = build_attach_query(["SmartMotionHuman", "SmartMotionVehicle"], heartbeat=10)
        assert query == ("action=attach&codes=[SmartMotionHuman,SmartMotionVehicle]&heartbeat=10")

    def test_bare_code_is_wrapped(self):
        assert build_attach_query("SmartMotionHuman") == (
            "action=attach&codes=[SmartMotionHuman]&heartbeat=5"
        )

    def test_all_codes_constant(self):
        assert ALL_CODES == "[All]"


class TestBackoff:
    def test_ladder_is_1_2_5_10_30(self):
        assert [backoff_delay(i, jitter=0) for i in range(5)] == [1.0, 2.0, 5.0, 10.0, 30.0]

    def test_saturates_at_30_seconds(self):
        assert backoff_delay(50, jitter=0) == 30.0

    def test_jitter_stays_within_bounds(self):
        import random

        rng = random.Random(1234)
        values = [backoff_delay(2, jitter=0.25, rng=rng) for _ in range(200)]
        assert all(3.75 <= v <= 6.25 for v in values)
        assert len(set(values)) > 1  # actually jittered, not constant
