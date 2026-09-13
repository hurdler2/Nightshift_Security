"""Offline outbox backed by SQLite (spec §59).

The edge must survive an internet outage without losing events. Metadata is tiny and
always kept; media is the first thing dropped when the disk quota is reached — event
metadata is never the first data deleted.

PHASE 1 ships the schema and the enqueue/claim primitives; the sync worker that
drains it into the cloud belongs to PHASE 4.
"""

from __future__ import annotations

import enum
import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_events (
    id              TEXT PRIMARY KEY,
    edge_event_id   TEXT NOT NULL UNIQUE,
    device_id       TEXT NOT NULL,
    logical_channel INTEGER,
    event_type      TEXT NOT NULL,
    payload         TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'PENDING',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_events_state ON outbox_events(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS outbox_media (
    id            TEXT PRIMARY KEY,
    edge_event_id TEXT NOT NULL,
    kind          TEXT NOT NULL,           -- snapshot | clip | thumbnail
    file_path     TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    priority      INTEGER NOT NULL DEFAULT 100,
    created_at    TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'PENDING',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    FOREIGN KEY (edge_event_id) REFERENCES outbox_events(edge_event_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_outbox_media_state ON outbox_media(state, priority);

CREATE TABLE IF NOT EXISTS pending_commands (
    id           TEXT PRIMARY KEY,
    command      TEXT NOT NULL,
    payload      TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'PENDING',
    result       TEXT
);

CREATE TABLE IF NOT EXISTS local_health_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_health_device ON local_health_history(device_id, recorded_at);
"""


class OutboxState(enum.StrEnum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteOutbox:
    """Durable local queue. Single-writer; the sync worker owns the connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self._conn.close()

    # -- events -------------------------------------------------------------
    def enqueue_event(
        self,
        *,
        device_id: str,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: datetime,
        logical_channel: int | None = None,
        edge_event_id: str | None = None,
    ) -> str:
        """Insert an event. Idempotent on ``edge_event_id`` (spec §58)."""
        event_id = edge_event_id or str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO outbox_events
                (id, edge_event_id, device_id, logical_channel, event_type,
                 payload, occurred_at, created_at, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edge_event_id) DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                event_id,
                device_id,
                logical_channel,
                event_type,
                json.dumps(payload, ensure_ascii=False),
                occurred_at.isoformat(),
                _now(),
                OutboxState.PENDING.value,
            ),
        )
        return event_id

    def claim_events(self, limit: int = 50) -> list[sqlite3.Row]:
        rows = self._conn.execute(
            """
            SELECT * FROM outbox_events
            WHERE state IN (?, ?)
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY occurred_at
            LIMIT ?
            """,
            (OutboxState.PENDING.value, OutboxState.FAILED_RETRYABLE.value, _now(), limit),
        ).fetchall()
        if rows:
            self._conn.executemany(
                "UPDATE outbox_events SET state = ? WHERE id = ?",
                [(OutboxState.UPLOADING.value, row["id"]) for row in rows],
            )
        return rows

    def mark_event(
        self,
        event_id: str,
        state: OutboxState,
        *,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE outbox_events
               SET state = ?, last_error = ?, attempts = attempts + 1, next_attempt_at = ?
             WHERE edge_event_id = ?
            """,
            (
                state.value,
                error,
                next_attempt_at.isoformat() if next_attempt_at else None,
                event_id,
            ),
        )

    # -- media --------------------------------------------------------------
    def enqueue_media(
        self,
        *,
        edge_event_id: str,
        kind: str,
        file_path: Path | str,
        size_bytes: int,
        priority: int = 100,
    ) -> str:
        media_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO outbox_media
                (id, edge_event_id, kind, file_path, size_bytes, priority, created_at, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                media_id,
                edge_event_id,
                kind,
                str(file_path),
                size_bytes,
                priority,
                _now(),
                OutboxState.PENDING.value,
            ),
        )
        return media_id

    def spool_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM outbox_media WHERE state != ?",
            (OutboxState.DONE.value,),
        ).fetchone()
        return int(row["total"])

    def evict_media(self, target_bytes: int) -> list[str]:
        """Drop lowest-priority, oldest media until the spool fits the quota.

        Event metadata is never evicted (spec §59).
        """
        freed: list[str] = []
        total = self.spool_bytes()
        if total <= target_bytes:
            return freed
        rows: Iterable[sqlite3.Row] = self._conn.execute(
            """
            SELECT id, file_path, size_bytes FROM outbox_media
            WHERE state != ?
            ORDER BY priority DESC, created_at ASC
            """,
            (OutboxState.DONE.value,),
        ).fetchall()
        for row in rows:
            if total <= target_bytes:
                break
            self._conn.execute("DELETE FROM outbox_media WHERE id = ?", (row["id"],))
            total -= int(row["size_bytes"])
            freed.append(row["file_path"])
        return freed

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) AS n FROM outbox_events GROUP BY state"
        ).fetchall()
        return {row["state"]: int(row["n"]) for row in rows}


# TODO(V1-BLOCKER): PHASE 4 — sync_worker draining this outbox into
# POST /internal/edge/v1/events with exponential retry and idempotency on
# edge_event_id.
