"""In-process fan-out for live alarm updates (spec §16.4).

A push notification wakes a phone that is asleep. This is the other half: the app is
already open on the guard's screen, and the new alarm has to appear without a poll.

Three decisions worth stating:

* **Every subscription is tenant-scoped at subscribe time**, from the verified token.
  A client cannot ask for another tenant's stream, because it never gets to name one.
* **Slow clients are dropped, not waited for.** Each connection has a bounded queue;
  a phone on a bad site link that stops reading must not stall the alarm path for the
  other guards. It reconnects and refetches from the REST API — that is what the
  `resume` hint in the hello frame is for.
* **The hub holds no secrets.** Frames carry the same fields as the push payload,
  which already refuses to serialise an RTSP URL or a password.

This is deliberately in-process. With one API node it is correct; when a second node
appears, `Hub.publish` becomes a Redis pub/sub fan-out and nothing else changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

log = logging.getLogger(__name__)

#: Frames a single client may fall behind by before we consider it unreachable.
QUEUE_LIMIT = 50
#: Server-side keepalive. Mobile networks drop idle sockets well inside a night shift.
HEARTBEAT_SECONDS = 25.0


@dataclass(slots=True)
class Subscriber:
    id: UUID
    tenant_id: UUID
    user_id: UUID
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_LIMIT)
    )
    dropped: int = 0

    def offer(self, frame: dict[str, Any]) -> bool:
        """Non-blocking by design: publishing must never await a slow phone."""
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        return True


class Hub:
    """Tenant-scoped publish/subscribe over live connections."""

    def __init__(self) -> None:
        self._subscribers: dict[UUID, dict[UUID, Subscriber]] = {}

    def subscriber_count(self, tenant_id: UUID | None = None) -> int:
        if tenant_id is not None:
            return len(self._subscribers.get(tenant_id, {}))
        return sum(len(group) for group in self._subscribers.values())

    @contextlib.contextmanager
    def subscribe(self, *, tenant_id: UUID, user_id: UUID):
        """Register a connection for the life of the `with` block."""
        subscriber = Subscriber(id=uuid4(), tenant_id=tenant_id, user_id=user_id)
        self._subscribers.setdefault(tenant_id, {})[subscriber.id] = subscriber
        log.info("realtime subscriber %s joined tenant %s", subscriber.id, tenant_id)
        try:
            yield subscriber
        finally:
            group = self._subscribers.get(tenant_id, {})
            group.pop(subscriber.id, None)
            if not group:
                self._subscribers.pop(tenant_id, None)
            log.info(
                "realtime subscriber %s left tenant %s (dropped %d frames)",
                subscriber.id,
                tenant_id,
                subscriber.dropped,
            )

    def publish(self, tenant_id: UUID, frame: dict[str, Any]) -> int:
        """Deliver a frame to every live connection of one tenant. Returns the count."""
        group = self._subscribers.get(tenant_id)
        if not group:
            return 0
        delivered = 0
        for subscriber in list(group.values()):
            if subscriber.offer(frame):
                delivered += 1
            else:
                log.warning(
                    "realtime subscriber %s is too slow; frame dropped", subscriber.id
                )
        return delivered


#: The application-wide hub. One process, one hub.
hub = Hub()


def alarm_frame(payload_dict: dict[str, Any], *, event_id: UUID | None = None) -> dict[str, Any]:
    """Wrap a push payload as a realtime frame, so both channels say the same thing."""
    return _frame(
        "alarm.raised",
        {**payload_dict, "event_id": str(event_id or payload_dict.get("event_id"))},
    )


def event_frame(
    *,
    event_id: UUID,
    event_type: str,
    severity: str,
    camera_name: str | None,
    occurred_at: datetime,
) -> dict[str, Any]:
    """A sighting that did not raise an alarm; the timeline still wants it."""
    return _frame(
        "event.recorded",
        {
            "event_id": str(event_id),
            "event_type": event_type,
            "severity": severity,
            "camera_name": camera_name,
            "occurred_at": occurred_at.isoformat(),
        },
    )


def alarm_status_frame(*, alarm_id: UUID, status: str, actor_id: UUID | None) -> dict[str, Any]:
    """Someone acknowledged or resolved: every other guard's screen should follow."""
    return _frame(
        "alarm.updated",
        {
            "alarm_id": str(alarm_id),
            "status": status,
            "actor_id": str(actor_id) if actor_id else None,
        },
    )


def _frame(frame_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": frame_type, "sent_at": datetime.now(UTC).isoformat(), "data": data}


async def frames(subscriber: Subscriber) -> AsyncIterator[dict[str, Any]]:
    """Yield frames, emitting a heartbeat when the site is quiet.

    The heartbeat is not decoration: it is how the client learns the connection is
    still alive on a link that silently dropped, which on a night shift is the
    difference between "no alarms" and "no connection".
    """
    while True:
        try:
            async with asyncio.timeout(HEARTBEAT_SECONDS):
                yield await subscriber.queue.get()
        except TimeoutError:
            yield _frame("heartbeat", {"queued": subscriber.queue.qsize()})


__all__ = [
    "HEARTBEAT_SECONDS",
    "QUEUE_LIMIT",
    "Hub",
    "Subscriber",
    "alarm_frame",
    "alarm_status_frame",
    "event_frame",
    "frames",
    "hub",
]
