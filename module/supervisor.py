"""Unified service supervision.

The central idea is to separate "a dependency is not ready yet" from
"something broke while running". The first is a normal waiting state that
retries forever with backoff; the second triggers a per-component reconnect.
Neither of them terminates the process.
"""

import asyncio
import random
import time
from typing import Callable, Optional, Protocol

import config
from logger import setup_logger
from module.health import HealthState

logger = setup_logger(__name__)


class FatalConfigError(Exception):
    """Configuration is wrong. Restarting cannot fix it, so this is the only
    exception allowed to terminate the process."""


class Backoff:
    """Exponential backoff timer with jitter. Must be reset explicitly on success."""

    def __init__(
        self,
        minimum: float = 1.0,
        maximum: float = 30.0,
        jitter: float = 0.2,
        rng: Callable[[], float] = random.random,
    ):
        self._minimum = minimum
        self._maximum = maximum
        self._jitter = jitter
        self._rng = rng
        self._current = minimum

    def next_delay(self) -> float:
        """Return the delay for this attempt and double the base for the next one."""
        base = self._current
        self._current = min(self._current * 2, self._maximum)
        # Jitter keeps independent components from retrying in lockstep.
        factor = 1 + self._jitter * (2 * self._rng() - 1)
        return base * factor

    def reset(self) -> None:
        """Call after a successful connection."""
        self._current = self._minimum


class ManagedService(Protocol):
    """Contract for a supervised component."""

    name: str

    async def connect_once(self) -> None:
        """Establish one connection. Raise on failure; the supervisor retries."""
        ...

    async def run(self) -> None:
        """Long-running body. Never returns under normal operation.

        Returning at all counts as a failure, so a component that has to
        re-establish something periodically must loop inside this body rather
        than returning to be called again. The supervisor stops a healthy body
        by cancelling it, which is the only way to interrupt one, so this must
        let CancelledError propagate; teardown() then releases the resources.
        """
        ...

    async def teardown(self) -> None:
        """Release resources. Must be idempotent and must not raise.

        Called after every failed attempt, including attempts where
        connect_once() never succeeded, and once more when supervision ends.
        """
        ...


class _LogThrottle:
    """Log the first few failures verbatim, then at most once per interval.

    Repeated reconnection attempts must not flood the log; some hosts keep
    logs on a small in-memory filesystem where volume matters.
    """

    def __init__(self, verbose_count: int = 5, interval: float = 60.0):
        self._verbose_count = verbose_count
        self._interval = interval
        self._count = 0
        # Seeded with the current reading rather than zero: comparing against
        # zero would let the first throttled message straight through on any
        # process that has already been up longer than the interval.
        self._last_logged = time.monotonic()

    def should_log(self) -> bool:
        self._count += 1
        if self._count <= self._verbose_count:
            return True
        now = time.monotonic()
        if now - self._last_logged >= self._interval:
            self._last_logged = now
            return True
        return False

    @property
    def count(self) -> int:
        return self._count

    def reset(self) -> None:
        self._count = 0
        self._last_logged = time.monotonic()


class Supervisor:
    """Drives components: reconnection, backoff, health reporting, watchdog.

    There are exactly three ways the process may exit, and they all live here
    or in main.py:
      1. SIGTERM / SIGINT
      2. FatalConfigError, which restarting cannot fix but which must be visible
      3. The watchdog, once a component has been down past its threshold, a
         component loop has stopped advancing past its own, or a component has
         ended too many connected sessions inside the churn window. An
         unwritable health snapshot is reported by the same watchdog but is not
         among these three: restarting cannot fix an unwritable path.
    """

    def __init__(self, health: HealthState, shutdown_event: asyncio.Event):
        self.health = health
        self.shutdown_event = shutdown_event
        self.exit_reason: Optional[str] = None
        self.backoff_factory: Callable[[], Backoff] = lambda: Backoff(
            minimum=config.RECONNECT_BACKOFF_MIN,
            maximum=config.RECONNECT_BACKOFF_MAX,
        )

    async def run_service(self, service: ManagedService) -> None:
        """Supervise one component until shutdown or a fatal configuration error."""
        backoff = self.backoff_factory()
        throttle = _LogThrottle()

        try:
            while not self.shutdown_event.is_set():
                # Whether this attempt got as far as a connected session. Only
                # those are counted as reconnections: a component whose
                # dependency has not appeared yet fails inside connect_once()
                # on every attempt, for as long as that takes, and that is a
                # waiting state this project must never put a ceiling on.
                # Something that has never connected is already covered by the
                # down criterion and its far longer tolerance.
                session_began = False
                try:
                    await service.connect_once()
                    session_began = True
                    await self._serve_session(service, backoff, throttle)
                except FatalConfigError:
                    self.exit_reason = "fatal_config"
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if session_began:
                        self.health.record_session_end(service.name)
                    self.health.mark_down(service.name, exc)
                    if throttle.should_log():
                        logger.warning(
                            f"Component {service.name} failed "
                            f"(attempt {throttle.count}): {exc}; reconnecting"
                        )
                    await self._safe_teardown(service)

                    delay = backoff.next_delay()
                    # Waiting on the shutdown event rather than sleeping keeps a
                    # long backoff from holding the process past the container
                    # runtime's stop grace period.
                    try:
                        await asyncio.wait_for(self.shutdown_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass  # Backoff elapsed normally; try again.

            logger.info(f"Supervision loop for {service.name} has stopped")
        finally:
            # Also reached on a fatal configuration error and on cancellation.
            # A component stopped while healthy would otherwise keep whatever
            # handle it holds, and the next attempt to open it would fail.
            await self._safe_teardown(service)

    async def _serve_session(
        self, service: ManagedService, backoff: Backoff, throttle: _LogThrottle
    ) -> None:
        """Run one connected session, returning only once shutdown is requested.

        This is the authoritative statement of when a component counts as
        recovered, and the only place one is ever marked up.

        Connecting is not the same as recovering. The component is reported up,
        and the backoff and log throttle are reset, only once the session has
        lasted config.SERVICE_STABLE_SECONDS. A component that connects and
        then fails immediately, over and over, would otherwise reset all three
        on every cycle: the backoff would never grow past its minimum, the
        throttle would never engage, and HealthState would re-stamp its down
        timestamp each time, so down_duration() could never reach the watchdog
        threshold and a permanently broken component would look merely busy.

        That is why a component may refresh the health file from its own loop
        but must never call mark_up() there: reporting itself up once per cycle
        would defeat the rule from the inside.

        Anything that ends the session is raised to the supervision loop, which
        treats it as a failed attempt.
        """
        run_task = asyncio.ensure_future(service.run())
        shutdown_task = asyncio.ensure_future(self.shutdown_event.wait())
        stable = False
        try:
            while True:
                await asyncio.wait(
                    (run_task, shutdown_task),
                    timeout=None if stable else config.SERVICE_STABLE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if run_task.done():
                    # Re-raises whatever ended the session, if anything did.
                    run_task.result()
                    raise RuntimeError("run body returned unexpectedly")
                if shutdown_task.done():
                    return
                # Neither happened inside the window: the session has held.
                stable = True
                self.health.mark_up(service.name)
                backoff.reset()
                throttle.reset()
        finally:
            for task in (run_task, shutdown_task):
                task.cancel()
            # Let the body finish unwinding before the caller tears it down.
            await asyncio.gather(run_task, shutdown_task, return_exceptions=True)

    async def _safe_teardown(self, service: ManagedService) -> None:
        """Release a component's resources without letting it break the loop.

        The teardown runs as a shielded task so a cancellation arriving while
        it is in flight cannot truncate it half-way: releasing the handle is
        what lets the next attempt take it. Any cancellation seen along the way
        is re-raised once the teardown has finished, so cancellation still
        propagates.
        """
        task = asyncio.ensure_future(service.teardown())
        cancelled: Optional[asyncio.CancelledError] = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                break  # Reported below, from the task's own result.
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.warning(f"Component {service.name} teardown error: {error}")
        if cancelled is not None:
            raise cancelled

    async def watchdog_loop(
        self,
        threshold: float = config.WATCHDOG_DOWN_SECONDS,
        interval: float = config.WATCHDOG_CHECK_INTERVAL,
        stall_threshold: float = config.WATCHDOG_STALL_SECONDS,
        churn_sessions: int = config.WATCHDOG_CHURN_SESSIONS,
    ) -> None:
        """Exit the process on any of the three ways a component can be lost;
        report, without exiting, the one way the snapshot file can be lost.

        A component that fails loudly is caught by down_duration: it raised,
        the supervisor marked it down, and the clock has been running since.

        A component that blocks without raising is caught by stall_duration.
        It is still marked up, so down_duration reads zero, but its own loop
        has stopped reporting that it advanced. Without this second check that
        failure is invisible and the process sits there forever.

        A component that fails, recovers, and fails again for ever is caught by
        churning, and is invisible to both of the above by construction. A
        session counts as recovered once it has lasted SERVICE_STABLE_SECONDS,
        and that judgement re-stamps every clock the first two measure from -
        so any failure taking longer than that window to raise reaches the
        point where it looks recovered before the point where it fails, and
        repeats with the down clock at zero and the stall clock at zero. The
        first two ask what state a component is in now; this one asks how many
        times it has been in that state lately, which is the only reading such
        a loop leaves behind.

        A snapshot that stops being written while every component loop keeps
        advancing is caught by snapshot_age, but only reported: the loops are
        demonstrably running, so the write itself is what failed, and a
        restart cannot make an unwritable path writable. It would instead
        repeat the whole startup - reinitialising the modem and re-reading its
        stored messages - every few minutes for ever, which is worse than the
        fault it is reacting to. The healthcheck fails on the file's own mtime
        regardless, so the fault stays visible without a restart.

        Both exit thresholds sit far above any normal cycle, so neither fires
        on a component that is merely reconnecting.

        None of the readings is adjusted here. Each is measured by HealthState
        against its own clock, and the one correction a stall reading needs -
        discounting the age an outage leaves on the snapshot - is applied where
        the moment of the recovery is known exactly rather than sampled from
        here between two inspections.
        """
        # Seeded per loop rather than per process: the threshold it throttles
        # against is a parameter of this loop. verbose_count=0 rather than the
        # usual burst: the condition below cannot turn true before real time
        # since construction has already reached stall_threshold - the same
        # span _LogThrottle would then require before letting a second message
        # through - so a verbose first message would leave a second message
        # right behind it, immediately, on the very next inspection.
        snapshot_throttle = _LogThrottle(verbose_count=0, interval=stall_threshold)

        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(self.shutdown_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

            duration = self.health.down_duration()
            if duration >= threshold:
                logger.error(
                    f"Watchdog tripped: a component has been down for "
                    f"{duration:.0f}s (threshold {threshold:.0f}s); exiting so the "
                    f"container runtime can restart everything"
                )
                self.exit_reason = "watchdog"
                self.shutdown_event.set()
                return

            # Not gated on the down reading, unlike the two below. A component
            # that keeps reconnecting spends much of its time down, so waiting
            # for a moment when nothing is would mean sampling exactly the
            # phase this criterion is looking for and missing it. The count is
            # a history rather than an instantaneous reading, so it is
            # meaningful whatever the component happens to be doing right now.
            churner = self.health.churning(churn_sessions)
            if churner is not None:
                # The observed count, not the threshold it cleared. This loop
                # only looks every `interval`, so a component flapping faster
                # than that ends several sessions between two inspections and
                # the count is then well above the threshold; printing the
                # threshold here would understate what happened by an amount
                # nothing bounds. The key is always present - it comes from the
                # same mapping churning() just read.
                observed = self.health.reconnect_counts()[churner]
                # States the criterion rather than diagnosing the other two.
                # A component that fails inside SERVICE_STABLE_SECONDS is never
                # marked up, so its down clock has been accumulating all along
                # and this one merely reached its threshold first - a line
                # claiming otherwise would describe a fault that is not the one
                # being debugged.
                logger.error(
                    f"Watchdog tripped: component {churner} has ended "
                    f"{observed} connected sessions within "
                    f"{self.health.churn_window:.0f}s (threshold "
                    f"{churn_sessions}); exiting so the container runtime can "
                    f"restart everything"
                )
                self.exit_reason = "churning"
                self.shutdown_event.set()
                return

            # Only asked while nothing is reported down, which down_duration
            # reading zero is exactly the statement of. The snapshot is written
            # only while every component is up, so anything that is down stops
            # it being refreshed too; acting on the age in that state would put
            # a second, far shorter ceiling on a component that is merely
            # reconnecting, and would report it as the wrong kind of failure.
            # What is down is already on the clock above, with the tolerance
            # chosen for it.
            stalled = self.health.stall_duration() if duration == 0.0 else None

            # None means the system has not been fully up even once. That is a
            # process still waiting for its dependencies, which must never be
            # killed for waiting.
            if stalled is not None and stalled >= stall_threshold:
                logger.error(
                    f"Watchdog tripped: a component loop has not advanced for "
                    f"{stalled:.0f}s (threshold {stall_threshold:.0f}s) while "
                    f"still reporting up; exiting so the container runtime can "
                    f"restart everything"
                )
                self.exit_reason = "stalled"
                self.shutdown_event.set()
                return

            # Only reached when no component loop is behind, which is what
            # makes this reading attributable: every loop is advancing and the
            # snapshot still is not being written, so the write itself is what
            # failed. Reported rather than acted on. Restarting cannot make an
            # unwritable path writable, and doing it on a timer would repeat
            # the whole startup - reinitialising the modem and re-reading its
            # stored messages - every few minutes for ever, which is a worse
            # outcome than the fault. The healthcheck fails on the file's own
            # mtime regardless, so this stays visible without that.
            age = self.health.snapshot_age() if duration == 0.0 else None
            if age is not None and age >= stall_threshold:
                if snapshot_throttle.should_log():
                    logger.error(
                        f"HEALTH_FILE has not been written for {age:.0f}s "
                        f"(threshold {stall_threshold:.0f}s) while every "
                        f"component loop is still advancing: the snapshot "
                        f"cannot be written. The container healthcheck will "
                        f"fail until it can. Not restarting - a restart cannot "
                        f"fix an unwritable path"
                    )
