"""The seam between the SMTP server and everything that persists (spec §7, §8).

`AlarmMailHandler` hands every accepted message to one callable. This module is that
callable: it opens a session, loads the site, runs the pure flow, uploads the snapshot
and writes the rows — in that order, for reasons that matter:

* **Snapshot before rows.** A media row must never point at an object that does not
  exist, so the upload happens first and its key is only recorded once it succeeded.
* **Rows before push.** A guard who taps the notification must find the alarm already
  there; a push for an alarm that is not yet queryable is worse than a push a second
  later.
* **A failed upload does not fail the alarm.** Losing the picture is survivable.
  Losing the fact that someone was on site at 03:00 is not — so the event is still
  written, with a warning, and the push still goes out.

The dedup window and outage state live on the intake instance rather than in the
database: they are 60-second working memory, and a round trip per event to maintain
them would cost more than recomputing them after a restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.object_storage.media_store import (
    MediaKind,
    MediaRejected,
    MediaStore,
)
from app.modules.alarms.suppression import OutageState
from app.modules.events.dedup import DedupWindow
from app.modules.ingest.pipeline import IngestResult
from app.modules.realtime.hub import alarm_frame, event_frame, hub
from app.services import persistence
from app.services.alarm_flow import NO_EVENT, FlowOutcome, process

log = logging.getLogger(__name__)

#: Called after the rows are committed. Returning quickly matters: the recorder is
#: still holding the SMTP connection open while this runs.
Notifier = Callable[["IntakeOutcome"], Awaitable[None]]


@dataclass(slots=True)
class IntakeOutcome:
    """What one message produced, for the caller and for the tests."""

    outcome: FlowOutcome
    result: IngestResult
    event_id: UUID | None = None
    alarm_id: UUID | None = None
    media_keys: dict[str, str] = field(default_factory=dict)
    #: Set when the message was a redelivery of one already processed (spec §8.4).
    duplicate_message: bool = False

    @property
    def alarmed(self) -> bool:
        return self.outcome.alarmed


class MailIntake:
    """Turns an ingested message into committed rows and, if warranted, a push."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        media_store: MediaStore | None = None,
        notifier: Notifier | None = None,
        dedup: DedupWindow | None = None,
        outages: OutageState | None = None,
    ) -> None:
        self._sessions = session_factory
        self._media = media_store
        self._notify = notifier
        self._dedup = dedup or DedupWindow()
        self._outages = outages or OutageState()

    @property
    def outages(self) -> OutageState:
        """Exposed so the health watchdog can mark a site or device as degraded."""
        return self._outages

    async def __call__(self, result: IngestResult) -> IntakeOutcome:
        return await self.handle(result)

    async def handle(self, result: IngestResult) -> IntakeOutcome:
        identity = result.identity

        async with self._sessions() as session:
            context = await persistence.load_site_context(session, identity.device_id)
            if context is None:
                # Only reachable when the device row is gone: `devices.site_id` is a
                # foreign key, so a device without a site cannot exist, and deleting a
                # site cascades its devices away. Every row we could write — the
                # delivery record included — references the device, so there is
                # nowhere to put this message. It is logged rather than temp-failed:
                # asking the recorder to retry would loop forever.
                log.error(
                    "message from device %s (%s) has nowhere to be stored: the device is gone",
                    identity.device_id,
                    identity.smtp_username,
                )
                return IntakeOutcome(
                    outcome=FlowOutcome(stage=NO_EVENT, reason="device row missing"),
                    result=result,
                )

            outcome = process(
                result,
                context,
                outages=self._outages,
                dedup=self._dedup,
            )

            event_id: UUID | None = None
            media_keys: dict[str, str] = {}
            duplicate = False

            if outcome.persist_event and result.event is not None:
                known = await persistence.existing_event_id(session, result.event.source_message_id)
                duplicate = known is not None
                event_id = known or uuid4()
                if not duplicate:
                    media_keys = await self._upload(result, event_id)

            persisted = await persistence.persist_flow(
                session,
                result,
                outcome,
                media_keys=media_keys,
                event_id=event_id,
            )
            await session.commit()

        intake = IntakeOutcome(
            outcome=outcome,
            result=result,
            event_id=persisted.event_id,
            alarm_id=persisted.alarm_id,
            media_keys=media_keys,
            duplicate_message=duplicate,
        )

        self._broadcast(intake)

        if self._notify is not None and outcome.push is not None:
            try:
                await self._notify(intake)
            except Exception:
                # The alarm is already durable; a transport failure is the notifier's
                # problem to retry, not a reason to make the recorder resend the mail.
                log.exception("notifier failed for event %s", intake.event_id)

        return intake

    def _broadcast(self, intake: IntakeOutcome) -> None:
        """Push the news to any app already open. Never raises: the rows are what count."""
        tenant_id = intake.result.identity.tenant_id
        try:
            if intake.outcome.push is not None:
                hub.publish(
                    tenant_id, alarm_frame(intake.outcome.push.to_dict(), event_id=intake.event_id)
                )
            elif intake.event_id is not None and intake.result.event is not None:
                event = intake.result.event
                hub.publish(
                    tenant_id,
                    event_frame(
                        event_id=intake.event_id,
                        event_type=event.event_type,
                        severity=(
                            intake.outcome.severity.value if intake.outcome.severity else "INFO"
                        ),
                        camera_name=intake.outcome.camera_name,
                        occurred_at=event.received_at,
                    ),
                )
        except Exception:
            log.exception("realtime broadcast failed for event %s", intake.event_id)

    async def _upload(self, result: IngestResult, event_id: UUID) -> dict[str, str]:
        """Store each attached image. Returns sha256 -> object key for what landed."""
        if self._media is None or not result.media:
            return {}

        identity = result.identity
        occurred_at = result.event.received_at  # only called once an event exists
        keys: dict[str, str] = {}

        for media in result.media:
            try:
                # boto3 is blocking; the event loop is also serving other recorders.
                stored = await asyncio.to_thread(
                    self._media.put,
                    media.content,
                    tenant_id=identity.tenant_id,
                    site_id=identity.site_id,
                    event_id=event_id,
                    kind=MediaKind.SNAPSHOT,
                    occurred_at=occurred_at,
                )
            except MediaRejected as exc:
                # Not a JPEG: worth knowing about, never worth dropping the event.
                log.warning("snapshot rejected for device %s: %s", identity.device_id, exc)
                continue
            except Exception:
                log.exception("snapshot upload failed for device %s", identity.device_id)
                continue
            keys[media.sha256] = stored.key

        return keys


def build_intake(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    media_store: MediaStore | None = None,
    notifier: Notifier | None = None,
) -> MailIntake:
    return MailIntake(session_factory, media_store=media_store, notifier=notifier)


__all__ = ["IntakeOutcome", "MailIntake", "Notifier", "build_intake"]
