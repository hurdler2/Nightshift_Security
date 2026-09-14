"""The sweep that notices silence (spec §13.3).

`python -m app.workers.watchdog` wakes every minute, asks
:func:`app.services.silence_sweep.plan_sweep` what the fleet's quiet looks like, and
writes whatever it decides. Everything interesting is in that pure function; this file
is the loop, the queries and the rows.

Two things it deliberately does not do:

* **It does not evaluate rules.** A dark recorder is not a sighting to be scored
  against a schedule — it is a fault or a theft, and either way somebody is told.
  Routing it through the rule engine would let a misconfigured rule silence it.
* **It does not retry a still-dark recorder.** The alarm is raised on the transition
  into silence; keeping it open is the alarm's job, and escalation is what makes it
  louder. Re-alarming every minute would train people to ignore it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db import models
from app.db.session import SessionFactory
from app.modules.notifications.payload import build_push
from app.modules.realtime.hub import alarm_frame, hub
from app.modules.rules.risk import Severity
from app.services.silence_sweep import DeviceSnapshot, SilenceAlarm, SweepResult, plan_sweep

log = logging.getLogger(__name__)

#: How often the fleet is checked. Well under the shortest expected report interval,
#: so a recorder that dies at 02:00 is not first noticed at 02:30.
SWEEP_INTERVAL_SECONDS = 60

#: A site outage is serious; a single silent recorder on a live site is worse, because
#: that is what a stolen box looks like (spec §13.1).
SEVERITY_BY_EVENT = {
    "site_offline": Severity.HIGH,
    "device_silent": Severity.CRITICAL,
}


async def load_snapshots(session: AsyncSession) -> list[DeviceSnapshot]:
    """One query for the whole fleet: recorders, their sites and their last contact."""
    rows = (
        await session.execute(
            select(models.Device, models.Site, models.DeviceSilenceState)
            .join(models.Site, models.Site.id == models.Device.site_id)
            .outerjoin(
                models.DeviceSilenceState,
                models.DeviceSilenceState.device_id == models.Device.id,
            )
        )
    ).all()

    snapshots: list[DeviceSnapshot] = []
    for device, site, state in rows:
        snapshots.append(
            DeviceSnapshot(
                device_id=device.id,
                site_id=site.id,
                tenant_id=device.tenant_id,
                name=device.name,
                last_message_at=state.last_message_at if state else None,
                stored_state=state.state if state else "NEVER_SEEN",
                expected_interval_seconds=(
                    state.expected_interval_seconds if state else 1800
                ),
                site_timezone=site.timezone,
                site_armed=site.is_armed,
            )
        )
    return snapshots


async def apply_sweep(
    session: AsyncSession, snapshots: list[DeviceSnapshot], result: SweepResult
) -> list[UUID]:
    """Write the sweep's conclusions. Returns the alarm ids raised."""
    by_id = {s.device_id: s for s in snapshots}
    now = datetime.now(UTC)

    for device_id, new_state in result.state_changes.items():
        snapshot = by_id[device_id]
        row = await session.get(models.DeviceSilenceState, device_id)
        if row is None:
            row = models.DeviceSilenceState(
                device_id=device_id,
                tenant_id=snapshot.tenant_id,
                last_message_at=snapshot.last_message_at,
            )
            session.add(row)
        row.state = new_state
        row.state_changed_at = now

    alarm_ids: list[UUID] = []
    for alarm in result.alarms:
        alarm_ids.append(await _raise_alarm(session, alarm, now))

    await session.flush()
    return alarm_ids


async def _raise_alarm(session: AsyncSession, alarm: SilenceAlarm, now: datetime) -> UUID:
    """One event plus one alarm, bypassing the rule engine on purpose."""
    site = await session.get(models.Site, alarm.site_id)
    severity = SEVERITY_BY_EVENT.get(alarm.event_type, Severity.HIGH)

    event = models.Event(
        id=uuid4(),
        tenant_id=alarm.tenant_id,
        site_id=alarm.site_id,
        # A site-level outage has no single device; the first quiet one stands for it
        # so the row stays attributable, and `device_ids` records the full set.
        device_id=alarm.device_id or alarm.device_ids[0],
        event_type=alarm.event_type,
        vendor_label=None,
        channel_number=None,
        occurred_at=now,
        source="watchdog",
        # Deterministic: a re-run of the same sweep minute cannot double up.
        source_message_id=(
            f"watchdog:{alarm.event_type}:"
            f"{alarm.device_id or alarm.site_id}:{now:%Y%m%d%H%M}"
        ),
        dedup_key=f"{alarm.event_type}:{alarm.device_id or alarm.site_id}",
        status="ALERTED",
        risk_score=90 if alarm.is_security_relevant else 60,
        severity=severity.value,
        risk_reasons=[alarm.reason],
        parse_status="PARSED",
        raw_payload={
            "silent_for_seconds": (
                int(alarm.silent_for.total_seconds()) if alarm.silent_for else None
            ),
            "device_ids": [str(d) for d in alarm.device_ids],
            "security_relevant": alarm.is_security_relevant,
        },
    )
    session.add(event)
    await session.flush()

    row = models.Alarm(
        id=uuid4(),
        tenant_id=alarm.tenant_id,
        site_id=alarm.site_id,
        event_id=event.id,
        severity=severity.value,
        status="ALERTED",
        opened_at=now,
    )
    session.add(row)

    payload = build_push(
        event_id=event.id,
        alarm_id=row.id,
        severity=severity,
        site_name=site.name if site else "Şantiye",
        camera_name=alarm.device_name or (site.name if site else "Şantiye"),
        event_type=alarm.event_type,
        occurred_at=now,
        risk_score=event.risk_score,
        reasons=[alarm.reason],
        has_snapshot=False,
    )
    hub.publish(alarm.tenant_id, alarm_frame(payload.to_dict(), event_id=event.id))
    log.warning("watchdog raised %s for site %s: %s", alarm.event_type, alarm.site_id, alarm.reason)
    return row.id


async def sweep_once(
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    *,
    now: datetime | None = None,
) -> SweepResult:
    async with session_factory() as session:
        snapshots = await load_snapshots(session)
        result = plan_sweep(snapshots, now=now)
        if result.alarms or result.state_changes:
            await apply_sweep(session, snapshots, result)
            await session.commit()
    if result.recovered:
        log.info("%d recorder(s) came back on their own", len(result.recovered))
    return result


async def serve(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    log.info("watchdog sweeping every %ds", SWEEP_INTERVAL_SECONDS)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        try:
            await sweep_once()
        except Exception:
            # The sweep must outlive any single bad row; the next minute tries again.
            log.exception("watchdog sweep failed")
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(SWEEP_INTERVAL_SECONDS):
                await stop.wait()

    log.info("watchdog stopped")


def main() -> None:  # pragma: no cover - process entrypoint
    asyncio.run(serve())


if __name__ == "__main__":  # pragma: no cover
    main()
