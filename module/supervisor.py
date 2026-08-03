"""Unified service supervision.

The central idea is to separate "a dependency is not ready yet" from
"something broke while running". The first is a normal waiting state that
retries forever with backoff; the second triggers a per-component reconnect.
Neither of them terminates the process.
"""

import random
from typing import Callable


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
