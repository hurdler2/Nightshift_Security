"""From a committed alarm to a phone that buzzes (spec §11).

`MailIntake` calls this after the rows are durable, which is the ordering that
matters: a guard who taps the notification must find the alarm already there.

It runs in its own session rather than the intake's. The alarm is already committed;
if writing the delivery records fails, the alarm must survive that failure, and
sharing a session would put both at the mercy of the same rollback.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.notifications.transport import (
    DispatchReport,
    PushDispatcher,
    recipients_for_site,
)
from app.services.intake import IntakeOutcome

log = logging.getLogger(__name__)


class PushNotifier:
    """The `notifier` an intake is built with; safe to call for any outcome."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: PushDispatcher,
    ) -> None:
        self._sessions = session_factory
        self._dispatcher = dispatcher

    @property
    def dispatcher(self) -> PushDispatcher:
        """Shared with the escalation worker, so both use one transport wiring."""
        return self._dispatcher

    async def __call__(self, intake: IntakeOutcome) -> DispatchReport | None:
        return await self.notify(intake)

    async def notify(self, intake: IntakeOutcome) -> DispatchReport | None:
        payload = intake.outcome.push
        if payload is None or intake.alarm_id is None:
            return None

        identity = intake.result.identity
        async with self._sessions() as session:
            recipients = await recipients_for_site(
                session, tenant_id=identity.tenant_id, site_id=identity.site_id
            )
            report = await self._dispatcher.dispatch(
                session,
                alarm_id=intake.alarm_id,
                tenant_id=identity.tenant_id,
                payload=payload,
                recipients=recipients,
            )
            await session.commit()

        if not report.reached_anyone:
            # Nobody was reached. The escalation loop (spec §12) is what turns this
            # into a second attempt and eventually a phone call; logging it loudly is
            # the minimum, because a silent alarm is the worst outcome in the product.
            log.error(
                "alarm %s reached nobody (%d recipients, %d failed)",
                intake.alarm_id,
                len(recipients),
                report.failed,
            )
        return report


def build_notifier(
    session_factory: async_sessionmaker[AsyncSession],
    dispatcher: PushDispatcher,
) -> PushNotifier:
    return PushNotifier(session_factory, dispatcher)


__all__ = ["PushNotifier", "build_notifier"]
