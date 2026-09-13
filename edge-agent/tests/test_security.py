"""Credential handling: redaction and the encrypted local store (spec §33)."""

from __future__ import annotations

import logging

import pytest

from nightshift_edge.devices.dahua.cgi_client import DahuaCredentials
from nightshift_edge.devices.dahua.playback import build_clip_window, build_playback_url
from nightshift_edge.devices.dahua.rtsp import build_live_url
from nightshift_edge.secrets.secret_store import SecretStore, SecretStoreError, generate_key
from nightshift_edge.security.redaction import (
    redact_command,
    redact_mapping,
    redact_text,
    redact_url,
)
from nightshift_edge.telemetry.logging_setup import RedactingFilter

CREDS = DahuaCredentials("nightshift", "sup3r-s3cret")


class TestRedaction:
    def test_rtsp_credentials_are_stripped(self):
        url = build_live_url("10.0.0.5", 1, credentials=CREDS)
        safe = redact_url(url)
        assert "sup3r-s3cret" not in safe
        assert "nightshift:" not in safe
        assert safe.startswith("rtsp://***@10.0.0.5/")
        assert "channel=1" in safe  # diagnostics survive

    def test_playback_credentials_are_stripped(self):
        from datetime import datetime

        url = build_playback_url(
            "10.0.0.5", 1, build_clip_window(datetime(2026, 9, 12, 22, 10, 10)), credentials=CREDS
        )
        assert "sup3r-s3cret" not in redact_url(url)

    @pytest.mark.parametrize(
        "text",
        [
            "http://user:pw@10.0.0.5/cgi-bin/snapshot.cgi",
            "GET /x?password=hunter2&channel=1",
            "curl --digest -u 'nightshift:hunter2' http://10.0.0.5/",
            "-u nightshift:hunter2",
        ],
    )
    def test_secrets_do_not_survive_redaction(self, text):
        safe = redact_text(text)
        assert "hunter2" not in safe
        assert "pw@" not in safe

    def test_query_secret_is_masked(self):
        assert redact_url("http://h/x?token=abc123&channel=2") == ("http://h/x?token=***&channel=2")

    def test_mapping_is_scrubbed_recursively(self):
        payload = {
            "device": {"host": "10.0.0.5", "password": "hunter2"},
            "urls": ["rtsp://u:p@10.0.0.5/cam"],
            "token": "abc",
        }
        safe = redact_mapping(payload)
        assert safe["device"]["password"] == "***"
        assert safe["token"] == "***"
        assert "u:p@" not in safe["urls"][0]

    def test_command_argv_is_scrubbed(self):
        argv = ["ffprobe", "-rtsp_transport", "tcp", build_live_url("h", 1, credentials=CREDS)]
        assert "sup3r-s3cret" not in " ".join(redact_command(argv))

    def test_logging_filter_scrubs_records(self, caplog):
        logger = logging.getLogger("test.redaction")
        logger.addFilter(RedactingFilter())
        with caplog.at_level(logging.INFO, logger="test.redaction"):
            logger.info("opening %s", build_live_url("10.0.0.5", 1, credentials=CREDS))
        assert "sup3r-s3cret" not in caplog.text


class TestSecretStore:
    def test_round_trip(self, tmp_path):
        store = SecretStore(tmp_path / "secrets.json", generate_key())
        store.put("device-1", CREDS)
        loaded = store.get("device-1")
        assert loaded == CREDS

    def test_password_is_not_stored_in_plaintext(self, tmp_path):
        path = tmp_path / "secrets.json"
        SecretStore(path, generate_key()).put("device-1", CREDS)
        raw = path.read_bytes()
        assert b"sup3r-s3cret" not in raw
        assert b"nightshift" not in raw

    def test_wrong_key_cannot_decrypt(self, tmp_path):
        path = tmp_path / "secrets.json"
        SecretStore(path, generate_key()).put("device-1", CREDS)
        with pytest.raises(SecretStoreError):
            SecretStore(path, generate_key()).get("device-1")

    def test_missing_device_returns_none(self, tmp_path):
        assert SecretStore(tmp_path / "s.json", generate_key()).get("nope") is None

    def test_delete_and_list(self, tmp_path):
        store = SecretStore(tmp_path / "s.json", generate_key())
        store.put("a", CREDS)
        store.put("b", CREDS)
        assert store.device_ids() == ["a", "b"]
        assert store.delete("a") is True
        assert store.delete("a") is False
        assert store.device_ids() == ["b"]

    def test_requires_a_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("EDGE_SECRET_KEY", raising=False)
        with pytest.raises(SecretStoreError, match="EDGE_SECRET_KEY"):
            SecretStore(tmp_path / "s.json")

    def test_credentials_repr_hides_the_password(self):
        assert "sup3r-s3cret" not in repr(CREDS)
