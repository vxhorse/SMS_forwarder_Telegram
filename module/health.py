"""Single source of truth for component health.

All internal timing uses a monotonic clock. Systems without a battery-backed
RTC can boot with a wildly wrong wall clock that jumps once NTP synchronises;
anything measuring durations with the wall clock would be thrown off by that
jump. The only wall-clock dependency in the health path is the mtime of the
snapshot file, which the healthcheck compares (see healthcheck.py).
"""

import json
import os
import time
from typing import Callable, Optional

import config
from logger import setup_logger

logger = setup_logger(__name__)


class HealthState:
    """Tracks per-component up/down state and maintains the snapshot file."""

    def __init__(
        self,
        service_names: list,
        health_file: str = config.HEALTH_FILE,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self._health_file = health_file
        self._rssi: Optional[int] = None
        self._registration: Optional[int] = None
        now = clock()
        # Every component starts down, so the watchdog clock effectively runs
        # from process start. A device that never appears is therefore visible
        # as a restart count rather than a silently idle container.
        self._services = {name: {"up": False, "since": now} for name in service_names}
        # None until the snapshot has been written at least once. A system that
        # has never been fully up is waiting, not stalled, and must never be
        # treated as the latter - that would reinstate the startup deadline
        # this project exists to remove.
        self._last_refresh: Optional[float] = None

    def mark_up(self, name: str) -> None:
        """Mark a component ready. Idempotent: does not reset the timestamp."""
        service = self._services.get(name)
        if service is None:
            logger.warning(f"Ignoring health report for unregistered component: {name}")
            return
        if not service["up"]:
            service["up"] = True
            service["since"] = self._clock()
            logger.info(f"Component is up: {name}")

    def mark_down(self, name: str, error: Optional[BaseException] = None) -> None:
        """Mark a component lost. Idempotent: does not reset the timestamp."""
        service = self._services.get(name)
        if service is None:
            logger.warning(f"Ignoring health report for unregistered component: {name}")
            return
        if service["up"]:
            service["up"] = False
            service["since"] = self._clock()
            logger.error(f"Component is down: {name}" + (f" ({error})" if error else ""))

    def record_rssi(self, value: Optional[int]) -> None:
        """Record the most recent signal strength. Diagnostic only."""
        self._rssi = value

    def record_registration(self, state: Optional[int]) -> None:
        """Record the most recent network registration state. Diagnostic only.

        Deliberately not part of the up/down decision: whether being off the
        network ends a session is the modem component's judgement, taken over
        several readings, and duplicating it here would let one dip in a
        handover take the whole snapshot down.
        """
        self._registration = state

    def all_up(self) -> bool:
        return all(service["up"] for service in self._services.values())

    def down_duration(self) -> float:
        """Seconds the longest-down component has been down. Zero if all are up."""
        now = self._clock()
        durations = [
            now - service["since"]
            for service in self._services.values()
            if not service["up"]
        ]
        return max(durations) if durations else 0.0

    def snapshot(self) -> dict:
        return {
            "services": {name: service["up"] for name, service in self._services.items()},
            "rssi": self._rssi,
            "registration": self._registration,
        }

    def refresh_file(self) -> None:
        """Refresh the snapshot only while every component is up.

        Written atomically so the healthcheck never reads a partial file.
        """
        if not self.all_up():
            return
        tmp_path = f"{self._health_file}.tmp"
        try:
            with open(tmp_path, "w") as handle:
                json.dump(self.snapshot(), handle)
            os.replace(tmp_path, self._health_file)
        except OSError as exc:
            logger.warning(f"Could not write health file: {exc}")
            return
        self._last_refresh = self._clock()

    def stall_duration(self) -> Optional[float]:
        """Seconds since the snapshot was last written, or None if never.

        None means the system has not yet been fully up even once, which is a
        legitimate waiting state - hardware can appear long after the process
        does. Only a system that was healthy and then stopped refreshing is
        stalled.
        """
        if self._last_refresh is None:
            return None
        return self._clock() - self._last_refresh

    def clear_file(self) -> None:
        """Remove the snapshot file. Safe to call repeatedly."""
        try:
            os.remove(self._health_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"Could not remove health file: {exc}")
