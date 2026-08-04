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
from collections import deque
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
        churn_window: float = config.WATCHDOG_CHURN_WINDOW,
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
        # When the snapshot was last written, and None until it has been.
        self._last_refresh: Optional[float] = None
        # When every component was last up at the same moment, and None until
        # they have been. A system that has never been fully up is waiting, not
        # stalled, and must never be treated as the latter - that would
        # reinstate the startup deadline this project exists to remove.
        self._all_up_since: Optional[float] = None
        self.churn_window = churn_window
        # When each component's connected sessions ended, most recent last.
        # Only sessions that actually connected are recorded here - see
        # Supervisor.run_service for why a component whose dependency has not
        # appeared yet must never contribute to this.
        self._session_ends = {name: deque() for name in service_names}

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
            # The moment the system became whole. The snapshot is not written
            # while anything is down, so the age it carries here measures the
            # outage; stall_duration() reads from this instead when it is the
            # later of the two. Recorded at the transition, where the moment is
            # exact, rather than left to be sampled by whoever reads the age.
            if self.all_up():
                self._all_up_since = now
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

    def record_session_end(self, name: str) -> None:
        """Record that one connected session of this component has ended.

        Counted separately from mark_down because they answer different
        questions. mark_down says the component is not working now;
        this says it has stopped working again, which is the only evidence
        that survives a component whose every failure is followed by a
        recovery long enough to reset everything else.
        """
        ends = self._session_ends.get(name)
        if ends is None:
            logger.warning(f"Ignoring session report for unregistered component: {name}")
            return
        ends.append(self._clock())

    def reconnect_counts(self) -> dict:
        """How many connected sessions each component has ended in the window.

        Pruned on read rather than on write, so a component that stopped
        failing does not keep an old count until something happens to it.
        """
        now = self._clock()
        counts = {}
        for name, ends in self._session_ends.items():
            while ends and now - ends[0] > self.churn_window:
                ends.popleft()
            counts[name] = len(ends)
        return counts

    def churning(self, threshold: int) -> Optional[str]:
        """Name a component that has ended `threshold` sessions in the window.

        Per component rather than in total: two components each reconnecting
        occasionally is not one component failing repeatedly, and a reading
        that added them together could name neither.
        """
        for name, count in self.reconnect_counts().items():
            if count >= threshold:
                return name
        return None

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
            # Counts only. Diagnostic, and the one place this is visible before
            # the watchdog acts on it.
            "reconnects": self.reconnect_counts(),
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
        """Seconds since the least recently advanced component loop advanced.

        This is the reading that attributes a stall to a component. A loop
        blocked on something that never returns raises nothing, so the
        component stays marked up and no other reading in this process changes
        at all; its own progress stamp is the only thing that stops moving.

        A component that is down is not measured here. Its loop is not running,
        so of course it is not advancing; that state belongs to down_duration()
        and the far longer tolerance chosen for it.

        The snapshot file is deliberately not part of this reading. It is
        written by whichever loop reaches the call first, so it says nothing
        about any single component - and, more to the point, the one failure
        that makes it age on its own is a file that cannot be written, which
        restarting the process cannot fix. See snapshot_age().

        None means either that the system has not yet been fully up even once
        - a legitimate waiting state, since hardware can appear long after the
        process does - or that every component is currently down, which
        leaves nothing here to measure; that second case is down_duration()'s
        business, not this reading's. Only a system with at least one
        component currently up, and that was healthy at some point before, is
        ever stalled.
        """
        if self._all_up_since is None:
            return None
        now = self._clock()
        ages = [
            now - service["progress"]
            for service in self._services.values()
            if service["up"]
        ]
        return max(ages) if ages else None

    def snapshot_age(self) -> Optional[float]:
        """Seconds since the snapshot was last written, or since the system
        last became whole, whichever is later.

        Kept apart from stall_duration() because the two need different
        answers. A snapshot that stops being written while every component
        loop keeps reporting progress means the loops are fine and the file is
        not - the write is the step that failed - and restarting the process
        cannot make an unwritable path writable. What it can do is repeat the
        whole startup every few minutes for ever, which is worse than the fault
        it is reacting to: an unhealthy container that keeps forwarding
        messages beats one that stops to reinitialise the modem on a timer.

        The snapshot is not written at all while a component is down, so the
        age it carries at the moment of a recovery measures the outage rather
        than anything that is wrong now. Measuring from the recovery instead is
        what discounts it, and doing so here rather than downstream is what
        makes it exact: this is where the moment is known. It expires by
        itself, because the time since the recovery grows while the outage it
        discounts does not.

        None means the system has not yet been fully up even once.
        """
        if self._all_up_since is None:
            return None
        # A snapshot that has never been written has no age of its own, and the
        # absence of one is not a reason to stop measuring: a process that
        # cannot write HEALTH_FILE at all is one whose healthcheck will never
        # pass again.
        written = self._all_up_since
        if self._last_refresh is not None and self._last_refresh > written:
            written = self._last_refresh
        return self._clock() - written

    def clear_file(self) -> None:
        """Remove the snapshot file. Safe to call repeatedly."""
        try:
            os.remove(self._health_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"Could not remove health file: {exc}")
