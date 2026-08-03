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
        # "progress" is the last time this component's own loop reported that
        # it advanced, and is None until it has. It is deliberately not seeded
        # with the current reading: a component that has never run has not made
        # progress, and pretending it has would start the stall clock on a
        # process that is still waiting for its hardware.
        self._services = {
            name: {"up": False, "since": now, "progress": None}
            for name in service_names
        }
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
            now = self._clock()
            service["up"] = True
            service["since"] = now
            # Re-initialised here rather than left where the previous session
            # put it. A component that has just come back has made no progress
            # under this session yet, and the age its last one left behind
            # measures the outage, not this session; carried across the
            # recovery it would be read as a stall the moment the component
            # reconnected. This is also what lets stall_duration() assume that
            # anything reported up has a progress stamp.
            service["progress"] = now
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

    def record_progress(self, name: str) -> None:
        """Record that one component's own loop advanced.

        Called from that loop, and never from refresh_file(), because only a
        stamp the loop writes itself says anything about the loop. refresh_file()
        cannot: it is gated on every component being up, so it falls silent for
        reasons that have nothing to do with the component being measured, and
        every component loop calls it, so one loop that is still running keeps
        the shared stamp fresh on behalf of one that has stopped. A component
        blocked on a write raises nothing and stays marked up, so that shared
        stamp is precisely what makes such a component look healthy.
        """
        service = self._services.get(name)
        if service is None:
            logger.warning(f"Ignoring progress report for unregistered component: {name}")
            return
        service["progress"] = self._clock()

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
        """Seconds since the least recently advanced thing last advanced.

        Two kinds of thing are measured and the longest of them is returned:

        - every component that is up, from the last time its own loop reported
          progress. This is the reading that attributes a stall to a component.
          A loop blocked on something that never returns raises nothing, so the
          component stays marked up and no other reading in this process
          changes at all.
        - the snapshot file, from the last time it was written. It says nothing
          about any single component, because either component loop writes it
          for both, and is kept only because it is also the reading that fails
          when HEALTH_FILE itself can no longer be written.

        A component that is down is not measured here. Its loop is not running,
        so of course it is not advancing; that state belongs to down_duration()
        and the far longer tolerance chosen for it.

        None means the system has not yet been fully up even once, which is a
        legitimate waiting state - hardware can appear long after the process
        does. Only a system that was healthy and then stopped advancing is
        stalled.
        """
        if self._last_refresh is None:
            return None
        now = self._clock()
        ages = [now - self._last_refresh]
        # Anything reported up has a progress stamp: mark_up is the only way to
        # become up, and it writes one.
        ages.extend(
            now - service["progress"]
            for service in self._services.values()
            if service["up"]
        )
        return max(ages)

    def clear_file(self) -> None:
        """Remove the snapshot file. Safe to call repeatedly."""
        try:
            os.remove(self._health_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"Could not remove health file: {exc}")
