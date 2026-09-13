"""Event de-duplication (spec §8.2).

A person walking across a yard for thirty seconds is one incident, not thirty alarms.
The recorder already throttles with its own send interval; this is the second gate,
and the one we control.

Suppressed events are **counted, not discarded**: the operator still sees "12 sightings
in 4 minutes", which is exactly the signal that someone is still on site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

#: Spec §8.2. Longer than the old 15s because the recorder's own send interval is
#: already in the path, so a shorter window only produces duplicate alarms.
DEFAULT_WINDOW_SECONDS = 60


@dataclass(slots=True)
class DedupEntry:
    first_seen: datetime
    last_seen: datetime
    count: int = 1

    @property
    def duration(self) -> timedelta:
        return self.last_seen - self.first_seen


@dataclass(slots=True)
class DedupDecision:
    """Outcome for one event."""

    is_duplicate: bool
    key: str
    occurrence: int
    window_started_at: datetime
    #: How many were folded into the open window so far, including this one.
    total_in_window: int

    @property
    def should_alert(self) -> bool:
        return not self.is_duplicate


@dataclass(slots=True)
class DedupWindow:
    """In-process sliding window keyed by dedup key.

    Single-instance only. When the API runs on more than one node this moves behind
    Redis with the same interface; the semantics here are the contract.
    """

    window_seconds: int = DEFAULT_WINDOW_SECONDS
    _entries: dict[str, DedupEntry] = field(default_factory=dict)

    def check(self, key: str, at: datetime, *, window_seconds: int | None = None) -> DedupDecision:
        """Record an occurrence and say whether it opens a new alarm."""
        window = timedelta(seconds=window_seconds or self.window_seconds)
        entry = self._entries.get(key)

        if entry is None or at - entry.first_seen >= window:
            self._entries[key] = DedupEntry(first_seen=at, last_seen=at)
            return DedupDecision(
                is_duplicate=False,
                key=key,
                occurrence=1,
                window_started_at=at,
                total_in_window=1,
            )

        entry.count += 1
        entry.last_seen = max(entry.last_seen, at)
        return DedupDecision(
            is_duplicate=True,
            key=key,
            occurrence=entry.count,
            window_started_at=entry.first_seen,
            total_in_window=entry.count,
        )

    def peek(self, key: str) -> DedupEntry | None:
        return self._entries.get(key)

    def purge(self, before: datetime) -> int:
        """Drop windows that closed before `before`. Returns how many were removed."""
        stale = [k for k, e in self._entries.items() if e.first_seen < before]
        for key in stale:
            del self._entries[key]
        return len(stale)

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        """Always truthy.

        Without this, `__len__` makes an empty window falsy, so the common
        `dedup or DedupWindow()` default silently throws away the caller's window and
        every duplicate becomes a fresh alarm. Found exactly that way.
        """
        return True
