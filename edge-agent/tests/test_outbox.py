"""Offline queue behaviour (spec §58, §59)."""

from __future__ import annotations

from datetime import UTC, datetime

from nightshift_edge.queue.sqlite_store import OutboxState, SqliteOutbox


def make_outbox(tmp_path) -> SqliteOutbox:
    return SqliteOutbox(tmp_path / "queue.sqlite3")


OCCURRED = datetime(2026, 9, 12, 22, 14, 32, tzinfo=UTC)


class TestEventOutbox:
    def test_enqueue_and_claim(self, tmp_path):
        outbox = make_outbox(tmp_path)
        event_id = outbox.enqueue_event(
            device_id="dev-1",
            event_type="person_detected",
            payload={"vendor_event_code": "SmartMotionHuman"},
            occurred_at=OCCURRED,
            logical_channel=1,
        )
        rows = outbox.claim_events()
        assert [row["edge_event_id"] for row in rows] == [event_id]
        assert rows[0]["state"] == OutboxState.PENDING.value
        assert outbox.counts() == {OutboxState.UPLOADING.value: 1}
        outbox.close()

    def test_duplicate_edge_event_id_is_ignored(self, tmp_path):
        """Retrying an upload must not create a second row (spec §58)."""
        outbox = make_outbox(tmp_path)
        for _ in range(3):
            outbox.enqueue_event(
                device_id="dev-1",
                event_type="person_detected",
                payload={},
                occurred_at=OCCURRED,
                edge_event_id="fixed-uuid",
            )
        assert len(outbox.claim_events()) == 1
        outbox.close()

    def test_mark_done_removes_it_from_the_claim_set(self, tmp_path):
        outbox = make_outbox(tmp_path)
        event_id = outbox.enqueue_event(
            device_id="dev-1", event_type="video_loss", payload={}, occurred_at=OCCURRED
        )
        outbox.claim_events()
        outbox.mark_event(event_id, OutboxState.DONE)
        assert outbox.claim_events() == []
        outbox.close()

    def test_retryable_failures_come_back(self, tmp_path):
        outbox = make_outbox(tmp_path)
        event_id = outbox.enqueue_event(
            device_id="dev-1", event_type="video_loss", payload={}, occurred_at=OCCURRED
        )
        outbox.claim_events()
        outbox.mark_event(event_id, OutboxState.FAILED_RETRYABLE, error="cloud down")
        rows = outbox.claim_events()
        assert len(rows) == 1
        assert rows[0]["attempts"] == 1
        assert rows[0]["last_error"] == "cloud down"
        outbox.close()


class TestMediaQuota:
    def test_eviction_drops_media_but_never_events(self, tmp_path):
        """Spec §59: media is sacrificed first; event metadata is never the first loss."""
        outbox = make_outbox(tmp_path)
        event_id = outbox.enqueue_event(
            device_id="dev-1", event_type="person_detected", payload={}, occurred_at=OCCURRED
        )
        outbox.enqueue_media(
            edge_event_id=event_id,
            kind="clip",
            file_path="/spool/a.mp4",
            size_bytes=8_000_000,
            priority=200,
        )
        outbox.enqueue_media(
            edge_event_id=event_id,
            kind="snapshot",
            file_path="/spool/a.jpg",
            size_bytes=200_000,
            priority=10,
        )
        assert outbox.spool_bytes() == 8_200_000

        freed = outbox.evict_media(target_bytes=1_000_000)
        assert freed == ["/spool/a.mp4"]  # low priority (higher number) goes first
        assert outbox.spool_bytes() == 200_000
        assert len(outbox.claim_events()) == 1  # the event itself survived
        outbox.close()

    def test_no_eviction_below_quota(self, tmp_path):
        outbox = make_outbox(tmp_path)
        event_id = outbox.enqueue_event(
            device_id="dev-1", event_type="person_detected", payload={}, occurred_at=OCCURRED
        )
        outbox.enqueue_media(
            edge_event_id=event_id, kind="snapshot", file_path="/spool/a.jpg", size_bytes=1000
        )
        assert outbox.evict_media(target_bytes=10_000) == []
        outbox.close()


def test_schema_is_reopenable(tmp_path):
    path = tmp_path / "queue.sqlite3"
    SqliteOutbox(path).close()
    outbox = SqliteOutbox(path)
    assert outbox.counts() == {}
    outbox.close()
