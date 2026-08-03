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
      3. The watchdog, once a component has been down past the threshold or
         the health snapshot has gone unrefreshed past its own
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
                try:
                    await service.connect_once()
                    await self._serve_session(service, backoff, throttle)
                except FatalConfigError:
                    self.exit_reason = "fatal_config"
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
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
    ) -> None:
        """Exit the process on either of the two ways a component can be lost.

        A component that fails loudly is caught by down_duration: it raised,
        the supervisor marked it down, and the clock has been running since.

        A component that blocks without raising is caught by the snapshot age.
        It is still marked up, so down_duration reads zero, but nothing is
        refreshing the file any more. Without this second check that failure
        is invisible and the process sits there forever.

        Both thresholds sit far above any normal cycle, so neither fires on a
        component that is merely reconnecting.
        """
        # The snapshot age that was already on the clock when the last recovery
        # was observed, or None when there is nothing to discount. See the
        # stall check below.
        inherited_age: Optional[float] = None
        seen_down = False

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

            # Only asked while nothing is reported down, which down_duration
            # reading zero is exactly the statement of. The snapshot is written
            # only while every component is up, so anything that is down stops
            # it being refreshed too; acting on the age in that state would put
            # a second, far shorter ceiling on a component that is merely
            # reconnecting, and would report it as the wrong kind of failure.
            # What is down is already on the clock above, with the tolerance
            # chosen for it.
            stalled = self.health.stall_duration() if duration == 0.0 else None

            # An outage's age outlives the outage. Nothing re-stamps the
            # snapshot when a component comes back: it is not written at all
            # while anything is down, and the component loops only refresh it
            # on their own next cycle, which is a cycle away. So the first
            # inspections after a recovery find an age as old as the whole
            # outage sitting beside nothing being down, and reading that as a
            # stall would restart the process for having reconnected. Whatever
            # age the outage left behind is therefore discounted until a
            # refresh actually lands, at which point the reading no longer
            # contains it and there is nothing left to discount.
            if duration > 0.0:
                seen_down = True
                inherited_age = None
            elif seen_down:
                seen_down = False
                inherited_age = stalled
            elif inherited_age is not None and (
                stalled is None or stalled < inherited_age
            ):
                inherited_age = None

            # None means the system has not been fully up even once. That is a
            # process still waiting for its dependencies, which must never be
            # killed for waiting.
            if stalled is not None:
                # Measured against the same reading the age comes from, so both
                # sides use HealthState's clock and no second time source is
                # involved. Detection is delayed to one threshold past a
                # recovery, never suppressed: this term grows with the age.
                accrued = stalled
                if inherited_age is not None:
                    accrued = stalled - inherited_age
                if accrued >= stall_threshold:
                    logger.error(
                        f"Watchdog tripped: the health snapshot has not been "
                        f"refreshed for {stalled:.0f}s (threshold "
                        f"{stall_threshold:.0f}s) while every component still "
                        f"reports up. Either a component loop has stopped "
                        f"making progress without failing, or HEALTH_FILE can "
                        f"no longer be written; exiting so the container "
                        f"runtime can restart everything"
                    )
                    self.exit_reason = "stalled"
                    self.shutdown_event.set()
                    return
