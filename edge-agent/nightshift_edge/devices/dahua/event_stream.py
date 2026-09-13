"""Long-lived listener for `eventManager.cgi?action=attach`.

Responsibilities (spec §12, §48):

* keep exactly one attach connection per device alive,
* re-connect with the mandated backoff ``1s -> 2s -> 5s -> 10s -> 30s`` plus jitter,
* treat a missing heartbeat as a dead connection (a silent TCP socket is the common
  Dahua failure mode - the read never errors, events just stop),
* surface *every* event, including unknown codes, to the callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import secrets
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

from nightshift_edge.devices.dahua.cgi_client import (
    DahuaAuthError,
    DahuaCgiClient,
    DahuaError,
    DahuaTransportError,
)
from nightshift_edge.devices.dahua.event_parser import DahuaEventStreamParser
from nightshift_edge.devices.dahua.models import DahuaRawEvent

log = logging.getLogger(__name__)

EVENT_MANAGER_PATH = "eventManager.cgi"

#: Spec §48 "CGI stream koptu" — reconnect ladder in seconds.
BACKOFF_LADDER: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

#: Discovery default. Narrow this only after a device's behaviour is verified (§2.1).
ALL_CODES = "[All]"


def build_attach_query(codes: str | Sequence[str] = ALL_CODES, heartbeat: int = 5) -> str:
    """Build the raw (un-encoded) attach query string.

    Dahua expects literal brackets and commas; percent-encoding them makes some
    firmwares answer with an empty stream.
    """
    if not isinstance(codes, str):
        codes = "[" + ",".join(codes) + "]"
    elif not codes.startswith("["):
        codes = f"[{codes}]"
    return f"action=attach&codes={codes}&heartbeat={heartbeat}"


def backoff_delay(attempt: int, *, jitter: float = 0.25, rng: random.Random | None = None) -> float:
    """Delay before reconnect attempt ``attempt`` (0-based), with +/- jitter."""
    base = BACKOFF_LADDER[min(attempt, len(BACKOFF_LADDER) - 1)]
    if jitter <= 0:
        return base
    source = rng or _RNG
    return max(0.1, base * (1.0 + source.uniform(-jitter, jitter)))


_RNG = random.Random(secrets.randbits(64))  # noqa: S311 - jitter only, not security


@dataclass(slots=True)
class EventStreamStats:
    """Per-connection counters exported to telemetry (spec §45)."""

    connects: int = 0
    disconnects: int = 0
    auth_failures: int = 0
    events: int = 0
    heartbeats: int = 0
    unknown_codes: dict[str, int] = field(default_factory=dict)
    last_event_monotonic: float | None = None
    connected: bool = False


class DahuaEventStream:
    """Reconnecting attach listener for one device."""

    def __init__(
        self,
        client: DahuaCgiClient,
        *,
        codes: str | Sequence[str] = ALL_CODES,
        heartbeat_seconds: int = 5,
        watchdog_multiplier: float = 3.0,
        on_state_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._client = client
        self._query = build_attach_query(codes, heartbeat_seconds)
        self._heartbeat = heartbeat_seconds
        self._watchdog = max(15.0, heartbeat_seconds * watchdog_multiplier)
        self._on_state_change = on_state_change
        self.stats = EventStreamStats()

    @property
    def query(self) -> str:
        return self._query

    async def listen(self) -> AsyncIterator[DahuaRawEvent]:
        """Yield events forever, reconnecting on failure.

        Cancel the consuming task to stop. Auth failures also back off (and are
        counted) rather than hot-looping, so we never lock the XVR account out.
        """
        attempt = 0
        while True:
            try:
                async for event in self._connect_once():
                    attempt = 0
                    yield event
            except asyncio.CancelledError:
                raise
            except DahuaAuthError as exc:
                self.stats.auth_failures += 1
                log.error("dahua event stream auth failure: %s", exc)
            except (DahuaTransportError, DahuaError) as exc:
                log.warning("dahua event stream dropped: %s", exc)
            except Exception:
                log.exception("unexpected error in dahua event stream")
            finally:
                self._set_connected(False)

            delay = backoff_delay(attempt)
            attempt += 1
            log.info("reconnecting dahua event stream in %.1fs (attempt %d)", delay, attempt)
            await asyncio.sleep(delay)

    async def _connect_once(self) -> AsyncIterator[DahuaRawEvent]:
        parser = DahuaEventStreamParser()
        async with self._client.stream(EVENT_MANAGER_PATH, raw_query=self._query) as response:
            boundary = parser.boundary_from_content_type(response.headers.get("content-type"))
            if boundary:
                parser.set_boundary(boundary)
            self.stats.connects += 1
            self._set_connected(True)
            log.info("dahua event stream connected (%s)", response.headers.get("content-type", "?"))

            aiter_chunks = response.aiter_bytes()
            while True:
                try:
                    chunk = await asyncio.wait_for(aiter_chunks.__anext__(), timeout=self._watchdog)
                except StopAsyncIteration:
                    self.stats.disconnects += 1
                    log.info("dahua event stream closed by device")
                    return
                except TimeoutError as exc:
                    self.stats.disconnects += 1
                    raise DahuaTransportError(
                        f"no data for {self._watchdog:.0f}s (heartbeat={self._heartbeat}s)"
                    ) from exc

                for event in parser.feed(chunk):
                    self._record(event)
                    yield event

    def _record(self, event: DahuaRawEvent) -> None:
        loop_time = asyncio.get_running_loop().time()
        self.stats.last_event_monotonic = loop_time
        if event.is_heartbeat:
            self.stats.heartbeats += 1
        else:
            self.stats.events += 1

    def _set_connected(self, connected: bool) -> None:
        if self.stats.connected == connected:
            return
        self.stats.connected = connected
        if self._on_state_change:
            with contextlib.suppress(Exception):
                self._on_state_change(connected)


async def collect_events(
    client: DahuaCgiClient,
    *,
    duration_seconds: float,
    codes: str | Sequence[str] = ALL_CODES,
    heartbeat_seconds: int = 5,
    on_event: Callable[[DahuaRawEvent], None] | None = None,
) -> list[DahuaRawEvent]:
    """Listen for a fixed window and return what arrived (used by the probe, §50.9).

    Reconnects during the window; a transport error does not abort the probe.
    """
    stream = DahuaEventStream(client, codes=codes, heartbeat_seconds=heartbeat_seconds)
    collected: list[DahuaRawEvent] = []

    async def _run() -> None:
        async for event in stream.listen():
            collected.append(event)
            if on_event:
                on_event(event)

    task = asyncio.create_task(_run())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=duration_seconds)
    except TimeoutError:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return collected
