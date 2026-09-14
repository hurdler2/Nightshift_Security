"""The loop that makes an unanswered alarm louder (spec §12).

`python -m app.workers.escalation` wakes every few seconds, finds alarms nobody has
acknowledged, and notifies the next person in the chain. The policy itself already
exists in `app.modules.alarms.escalation`; this is the thing that makes time pass.

The rules it is careful about:

* **An acknowledgement stops the chain immediately**, including one that lands while a
  later step is already due. The state is rebuilt from the alarm row every sweep, so
  there is no in-memory timer to get out of step with the database.
* **A step is delivered once.** `alarm_deliveries.escalation_step` is the record of
  what has already gone out, so a restarted worker does not re-page everyone.
* **A step that reaches nobody falls through** to the roles above it rather than
  silently ending the chain (see `recipients_for_role`).
* **The escalation never resolves anything.** Only a person closes an alarm; a chain
  that ran out of steps stays open and loud.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db import models
from app.db.session import SessionFactory
from app.modules.alarms.escalation import EscalationState, policy_for
from app.modules.notifications.payload import build_push
from app.modules.notifications.transport import PushDispatcher, recipients_for_role
from app.modules.rules.risk import Severity

log = logging.getLogger(__name__)

#: Short: the shortest gap in any policy is 30 seconds, and being a few seconds late
#: to wake the site manager matters more here than a few extra queries.
SWEEP_INTERVAL_SECONDS = 10

#: Alarms older than this are left alone. Something that has been ringing unanswered
#: for two hours is not going to be fixed by a fourth notification; it is an
#: operational failure that belongs in the morning report.
MAX_AGE_SECONDS = 2 * 60 * 60

#: Which roles a step falls through to when nobody holds its own, most senior last.
ROLE_FALLBACK: dict[str, tuple[str, ...]] = {
    "GUARD": ("SUPERVISOR", "SITE_MANAGER", "SECURITY_MANAGER", "ADMIN", "OWNER"),
    "SUPERVISOR": ("SITE_MANAGER", "SECURITY_MANAGER", "ADMIN", "OWNER"),
    "SITE_MANAGER": ("SECURITY_MANAGER", "ADMIN", "OWNER"),
    "SECURITY_MANAGER": ("ADMIN", "OWNER"),
    "ADMIN": ("OWNER",),
    "OWNER": (),
}


@dataclass(slots=True)
class EscalationOutcome:
    """What one sweep did, for logging and for the tests."""

    escalated: list[tuple[UUID, int, str]] = field(default_factory=list)
    reached: int = 0
    unreachable: list[UUID] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not self.escalated


async def open_alarms(session: AsyncSession, now: datetime) -> list[models.Alarm]:
    """Alarms still waiting for a human, newest first."""
    cutoff = now.timestamp() - MAX_AGE_SECONDS
    rows = (
        (
            await session.execute(
                select(models.Alarm)
                .where(
                    models.Alarm.acknowledged_at.is_(None),
                    models.Alarm.resolved_at.is_(None),
                )
                .order_by(models.Alarm.opened_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [row for row in rows if row.opened_at.timestamp() >= cutoff]


async def delivered_steps(session: AsyncSession, alarm_id: UUID) -> set[int]:
    """Which steps already went out — the database is the only memory we keep."""
    rows = (
        (
            await session.execute(
                select(models.AlarmDelivery.escalation_step).where(
                    models.AlarmDelivery.alarm_id == alarm_id
                )
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def escalate_alarm(
    session: AsyncSession,
    dispatcher: PushDispatcher,
    alarm: models.Alarm,
    *,
    now: datetime,
) -> list[tuple[int, str]]:
    """Deliver whatever this alarm owes. Returns (step index, role) for each sent."""
    severity = Severity(alarm.severity)
    policy = policy_for(severity)
    state = EscalationState(
        opened_at=alarm.opened_at,
        policy=policy,
        acknowledged_at=alarm.acknowledged_at,
        resolved_at=alarm.resolved_at,
        delivered_steps=await delivered_steps(session, alarm.id),
    )

    due = state.due_steps(now)
    if not due:
        return []

    event = await session.get(models.Event, alarm.event_id)
    site = await session.get(models.Site, alarm.site_id)
    payload = build_push(
        event_id=alarm.event_id,
        alarm_id=alarm.id,
        severity=severity,
        site_name=site.name if site else "Şantiye",
        camera_name=(event.vendor_label if event and event.vendor_label else "Kamera"),
        event_type=event.event_type if event else "unknown_alarm",
        occurred_at=event.occurred_at if event else alarm.opened_at,
        risk_score=event.risk_score if event else 0,
        reasons=list(event.risk_reasons or []) if event else [],
        has_snapshot=False,
    )

    sent: list[tuple[int, str]] = []
    for step in due:
        index = policy.steps.index(step)
        recipients = await recipients_for_role(
            session,
            tenant_id=alarm.tenant_id,
            role=step.role,
            user_ids=step.user_ids,
            fallback_roles=ROLE_FALLBACK.get(step.role, ()),
        )
        if not recipients:
            # Nothing to send, but the step is spent: retrying it every ten seconds
            # would not conjure a phone, and the log line above already said so.
            state.mark_delivered(step)
            continue

        report = await dispatcher.dispatch(
            session,
            alarm_id=alarm.id,
            tenant_id=alarm.tenant_id,
            payload=payload,
            recipients=recipients,
            escalation_step=index,
        )
        state.mark_delivered(step)
        sent.append((index, step.role))
        log.warning(
            "alarm %s escalated to step %d (%s): %d reached",
            alarm.id,
            index,
            step.role,
            report.sent,
        )

    return sent


async def sweep_once(
    dispatcher: PushDispatcher,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    *,
    now: datetime | None = None,
) -> EscalationOutcome:
    moment = now or datetime.now(UTC)
    outcome = EscalationOutcome()

    async with session_factory() as session:
        for alarm in await open_alarms(session, moment):
            try:
                for index, role in await escalate_alarm(
                    session, dispatcher, alarm, now=moment
                ):
                    outcome.escalated.append((alarm.id, index, role))
            except Exception:
                # One bad alarm must not stop the others from climbing.
                log.exception("escalation failed for alarm %s", alarm.id)
                outcome.unreachable.append(alarm.id)
        await session.commit()

    return outcome


async def serve(settings: Settings | None = None) -> None:  # pragma: no cover - loop
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    from app.workers.mail_ingest import _notifier  # reuses the transport wiring

    notifier = _notifier(settings)
    if notifier is None:
        log.error("no push transport configured; escalation would reach nobody")
        return
    dispatcher = notifier.dispatcher

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(sig, stop.set)

    log.info("escalation sweeping every %ds", SWEEP_INTERVAL_SECONDS)
    while not stop.is_set():
        try:
            await sweep_once(dispatcher)
        except Exception:
            log.exception("escalation sweep failed")
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(SWEEP_INTERVAL_SECONDS):
                await stop.wait()

    await dispatcher.aclose()
    log.info("escalation stopped")


def main() -> None:  # pragma: no cover - process entrypoint
    asyncio.run(serve())


if __name__ == "__main__":  # pragma: no cover
    main()
