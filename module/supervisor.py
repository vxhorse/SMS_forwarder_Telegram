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
        """Long-running body. Never returns under normal operation."""
        ...

    async def teardown(self) -> None:
        """Release resources. Must be idempotent and must not raise."""
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
        self._last_logged = 0.0

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
        self._last_logged = 0.0


class Supervisor:
    """Drives components: reconnection, backoff, health reporting, watchdog.

    There are exactly three ways the process may exit, and they all live here
    or in main.py:
      1. SIGTERM / SIGINT
      2. FatalConfigError, which restarting cannot fix but which must be visible
      3. The watchdog, once a component has been down past the threshold
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

        while not self.shutdown_event.is_set():
            try:
                await service.connect_once()
                self.health.mark_up(service.name)
                backoff.reset()
                throttle.reset()
                await service.run()
                # A run body that returns has ended unexpectedly; reconnect.
                raise RuntimeError("run body returned unexpectedly")
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
                try:
                    await service.teardown()
                except Exception as teardown_exc:
                    logger.warning(
                        f"Component {service.name} teardown error: {teardown_exc}"
                    )

                delay = backoff.next_delay()
                try:
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass  # Backoff elapsed normally; try again.

        logger.info(f"Supervision loop for {service.name} has stopped")

    async def watchdog_loop(
        self,
        threshold: float = config.WATCHDOG_DOWN_SECONDS,
        interval: float = config.WATCHDOG_CHECK_INTERVAL,
    ) -> None:
        """Exit the process once a component has been down past the threshold.

        The threshold is far above any normal reconnection time, so this only
        fires when something is genuinely stuck rather than merely flapping.
        """
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
