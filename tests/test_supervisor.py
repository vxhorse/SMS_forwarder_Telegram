import asyncio
import importlib
import logging
import time

import pytest

import config
from module.health import HealthState
from module.supervisor import Backoff, FatalConfigError, Supervisor, _LogThrottle


def _no_jitter():
    """An rng returning 0.5 makes the jitter factor exactly 1.0."""
    return 0.5


def test_backoff_doubles_and_caps():
    backoff = Backoff(minimum=1.0, maximum=30.0, rng=_no_jitter)
    delays = [backoff.next_delay() for _ in range(7)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_backoff_reset_returns_to_minimum():
    backoff = Backoff(minimum=1.0, maximum=30.0, rng=_no_jitter)
    backoff.next_delay()
    backoff.next_delay()
    backoff.reset()
    assert backoff.next_delay() == 1.0


def test_backoff_jitter_stays_within_bounds():
    low = Backoff(minimum=10.0, maximum=10.0, jitter=0.2, rng=lambda: 0.0)
    high = Backoff(minimum=10.0, maximum=10.0, jitter=0.2, rng=lambda: 1.0)
    assert low.next_delay() == pytest.approx(8.0)
    assert high.next_delay() == pytest.approx(12.0)


def test_fatal_config_error_is_an_exception():
    assert issubclass(FatalConfigError, Exception)


def test_default_backoff_factory_uses_configured_bounds():
    """Exercise the factory every other test replaces, so its wiring is covered."""
    supervisor = Supervisor(HealthState(["device"]), asyncio.Event())
    backoff = supervisor.backoff_factory()
    # Jitter is +/-20%, so compare with room for it.
    assert backoff.next_delay() == pytest.approx(config.RECONNECT_BACKOFF_MIN, rel=0.25)
    for _ in range(20):
        delay = backoff.next_delay()
    assert delay == pytest.approx(config.RECONNECT_BACKOFF_MAX, rel=0.25)


def test_log_throttle_measures_its_window_from_construction():
    """The window starts now, not at monotonic zero.

    Measuring from zero would compare against the host's uptime, so on any
    process up longer than the interval the first throttled message would go
    straight through and the burst would not actually be throttled.
    """
    throttle = _LogThrottle(verbose_count=1, interval=1.0)
    assert throttle.should_log() is True  # Inside the verbose burst.
    assert throttle.should_log() is False  # Throttled: the window has not elapsed.
    assert throttle.count == 2


def test_watchdog_check_interval_has_a_floor(monkeypatch):
    """The interval is operator-settable, and zero would busy-spin the watchdog."""
    monkeypatch.setenv("WATCHDOG_CHECK_INTERVAL", "0")
    try:
        assert importlib.reload(config).WATCHDOG_CHECK_INTERVAL >= 1.0
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# Nothing below synchronises on elapsed time. Every test waits on an explicit
# signal raised by the code under test, so the results do not depend on how
# fast the machine running them happens to be. The timeout passed to
# asyncio.wait_for is only a failsafe: it turns a hang caused by a regression
# into a failure instead of a stuck run.
_FAILSAFE = 5.0


class _Milestone:
    """Counts occurrences and lets a test await the Nth one instead of sleeping."""

    def __init__(self):
        self.count = 0
        self._waiters = []

    def tick(self) -> None:
        self.count += 1
        for target, event in self._waiters:
            if self.count >= target:
                event.set()

    def at(self, target: int) -> asyncio.Event:
        event = asyncio.Event()
        if self.count >= target:
            event.set()
        else:
            self._waiters.append((target, event))
        return event


class _ProbeBackoff:
    """Backoff stand-in that records the schedule but never actually waits.

    It drives a real Backoff so the recorded delays are the ones production
    would use, then hands the supervisor zero so the test spends no wall time
    sitting on a timer.
    """

    def __init__(self, minimum=1.0, maximum=8.0):
        self._inner = Backoff(minimum=minimum, maximum=maximum, jitter=0.0)
        self.delays = []
        self.resets = 0
        self.attempts = _Milestone()
        self.recoveries = _Milestone()

    def next_delay(self) -> float:
        self.delays.append(self._inner.next_delay())
        self.attempts.tick()
        return 0.0

    def reset(self) -> None:
        self.resets += 1
        self.recoveries.tick()
        self._inner.reset()


class _SlowBackoff:
    """Hands out a delay far longer than the test is willing to wait.

    Used to prove the supervisor waits out its backoff on the shutdown event
    rather than sleeping through it.
    """

    def __init__(self, shutdown, delay=30.0):
        self._shutdown = shutdown
        self.delay = delay
        self.calls = 0

    def next_delay(self) -> float:
        self.calls += 1
        # Request shutdown exactly as the supervisor is about to wait it out.
        self._shutdown.set()
        return self.delay

    def reset(self) -> None:
        pass


class _ManualClock:
    """Monotonic stand-in that only ever moves when the test moves it."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StepClock:
    """Monotonic stand-in whose reading advances by a fixed step per call."""

    def __init__(self, step=1.0):
        self._step = step
        self.readings = _Milestone()

    def __call__(self) -> float:
        value = self.readings.count * self._step
        self.readings.tick()
        return value


class FakeService:
    """A managed service whose behaviour the test scripts in advance."""

    def __init__(self, name="device", connect_errors=None, run_errors=None):
        self.name = name
        self._connect_errors = list(connect_errors or [])
        self._run_errors = list(run_errors or [])
        self.connect_calls = 0
        self.run_calls = 0
        self.teardown_calls = 0
        # Ticked once a run body gets past its scripted errors, which is the
        # signal that the component is connected and serving.
        self.serving = _Milestone()
        self._park = asyncio.Event()

    async def connect_once(self):
        self.connect_calls += 1
        if self._connect_errors:
            error = self._connect_errors.pop(0)
            if error is not None:
                raise error

    async def run(self):
        self.run_calls += 1
        if self._run_errors:
            error = self._run_errors.pop(0)
            if error is not None:
                raise error
        self._park.clear()
        self.serving.tick()
        # Park on an event rather than a timer: a real long-running body never
        # returns, and an event leaves no pending timer behind when cancelled.
        await self._park.wait()

    def break_session(self):
        """End the current run body, which the supervisor treats as a failure."""
        self._park.set()

    async def teardown(self):
        self.teardown_calls += 1


class SlowTeardownService(FakeService):
    """A service whose teardown does not finish until the test releases it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.teardown_started = _Milestone()
        self.teardown_completed = False
        self._release = asyncio.Event()

    async def teardown(self):
        self.teardown_started.tick()
        await self._release.wait()
        self.teardown_calls += 1
        self.teardown_completed = True

    def release(self):
        self._release.set()


class _RecordingHealth(HealthState):
    """HealthState that remembers which errors it was told about."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.down_errors = []

    def mark_down(self, name, error=None):
        self.down_errors.append(error)
        super().mark_down(name, error)


def _make_supervisor(names=("device",), backoff=None, clock=time.monotonic, health=None):
    if health is None:
        health = HealthState(list(names), clock=clock)
    shutdown = asyncio.Event()
    supervisor = Supervisor(health, shutdown)
    # Collapse backoff to zero so the tests do not actually wait.
    probe = backoff if backoff is not None else _ProbeBackoff()
    supervisor.backoff_factory = lambda: probe
    return supervisor, health, shutdown


async def _cancel(task):
    """Cancel a supervision task and retrieve its outcome, leaving no stray task."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_retries_with_backoff_until_connected(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_STABLE_SECONDS", 0.0)
    probe = _ProbeBackoff()
    supervisor, health, _ = _make_supervisor(backoff=probe)
    service = FakeService(connect_errors=[OSError("no device"), OSError("no device"), None])
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.recoveries.at(1).wait(), timeout=_FAILSAFE)
    assert service.connect_calls == 3
    assert health.all_up() is True
    # Waiting for a dependency is not a failure: nothing decided to exit.
    assert supervisor.exit_reason is None
    # The delay grew between the two failed attempts.
    assert probe.delays == [1.0, 2.0]
    await _cancel(task)


async def test_runtime_error_triggers_teardown_and_reconnect(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_STABLE_SECONDS", 0.0)
    probe = _ProbeBackoff()
    supervisor, _, _ = _make_supervisor(backoff=probe)
    service = FakeService(run_errors=[RuntimeError("serial read failed")])
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(service.serving.at(1).wait(), timeout=_FAILSAFE)
    assert service.teardown_calls >= 1
    assert service.connect_calls >= 2
    assert supervisor.exit_reason is None
    await _cancel(task)


async def test_backoff_resets_after_a_successful_connection(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_STABLE_SECONDS", 0.0)
    probe = _ProbeBackoff()
    supervisor, _, _ = _make_supervisor(backoff=probe)
    service = FakeService(connect_errors=[OSError("no device"), OSError("no device"), None])
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.recoveries.at(1).wait(), timeout=_FAILSAFE)
    assert probe.delays == [1.0, 2.0]
    service.break_session()
    await asyncio.wait_for(probe.attempts.at(3).wait(), timeout=_FAILSAFE)
    # The session that held reset the schedule, so the failure after it starts
    # again from the minimum instead of continuing to grow.
    assert probe.delays == [1.0, 2.0, 1.0]
    await _cancel(task)


async def test_fatal_config_error_propagates():
    supervisor, _, _ = _make_supervisor()
    service = FakeService(connect_errors=[FatalConfigError("BOT_TOKEN missing")])
    with pytest.raises(FatalConfigError):
        await supervisor.run_service(service)
    assert supervisor.exit_reason == "fatal_config"
    assert service.connect_calls == 1


async def test_missing_dependency_retries_without_ever_giving_up():
    probe = _ProbeBackoff()
    supervisor, health, _ = _make_supervisor(backoff=probe)
    service = FakeService(connect_errors=[OSError("no device")] * 500)
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.attempts.at(50).wait(), timeout=_FAILSAFE)
    # Fifty consecutive failures and the supervisor is still trying: there is
    # no attempt limit and no startup deadline.
    assert service.connect_calls >= 50
    assert supervisor.exit_reason is None
    assert health.all_up() is False
    assert task.done() is False
    await _cancel(task)


async def test_shutdown_stops_the_supervision_loop():
    probe = _ProbeBackoff()
    supervisor, _, shutdown = _make_supervisor(backoff=probe)
    service = FakeService(connect_errors=[OSError("no device")] * 500)
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.attempts.at(3).wait(), timeout=_FAILSAFE)
    shutdown.set()
    # The loop returns rather than raising: a requested shutdown is not a failure.
    assert await asyncio.wait_for(task, timeout=_FAILSAFE) is None
    assert supervisor.exit_reason is None


async def test_shutdown_interrupts_a_long_backoff():
    """A pending shutdown must cut the backoff short, not be slept through.

    Sleeping out a 30 s backoff would push the process past the container
    runtime's stop grace period and turn a clean stop into a kill.
    """
    supervisor, _, shutdown = _make_supervisor()
    backoff = _SlowBackoff(shutdown, delay=30.0)
    supervisor.backoff_factory = lambda: backoff
    service = FakeService(connect_errors=[OSError("no device")] * 5)
    task = asyncio.create_task(supervisor.run_service(service))
    # Far below the 30 s delay: this only passes if the wait is interruptible.
    assert await asyncio.wait_for(task, timeout=2.0) is None
    assert backoff.calls == 1


async def test_failure_marks_the_component_down():
    probe = _ProbeBackoff()
    supervisor, health, _ = _make_supervisor(backoff=probe, clock=_StepClock())
    service = FakeService(run_errors=[RuntimeError("serial read failed")] * 500)
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.attempts.at(3).wait(), timeout=_FAILSAFE)
    assert health.down_duration() > 0
    assert supervisor.exit_reason is None
    await _cancel(task)


async def test_the_original_failure_reaches_the_health_record():
    """What actually broke must be reported, not a generic stand-in for it.

    The recorded error is the only description of the fault an operator gets,
    so replacing it with a placeholder would leave a broken component
    indistinguishable from any other.
    """
    probe = _ProbeBackoff()
    health = _RecordingHealth(["device"])
    supervisor, _, _ = _make_supervisor(backoff=probe, health=health)
    failure = RuntimeError("serial read failed")
    service = FakeService(run_errors=[failure] * 500)
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.attempts.at(1).wait(), timeout=_FAILSAFE)
    assert health.down_errors[0] is failure
    await _cancel(task)


async def test_a_stable_session_that_breaks_is_marked_down(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_STABLE_SECONDS", 0.0)
    probe = _ProbeBackoff()
    supervisor, health, _ = _make_supervisor(backoff=probe)
    service = FakeService()
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.recoveries.at(1).wait(), timeout=_FAILSAFE)
    assert health.all_up() is True
    service.break_session()
    await asyncio.wait_for(probe.attempts.at(1).wait(), timeout=_FAILSAFE)
    assert health.all_up() is False
    await _cancel(task)


async def test_flapping_component_never_counts_as_recovered():
    """Connecting is not recovering.

    A component whose connect succeeds and whose run body then fails at once,
    every time, is still broken. If the connection alone counted as success the
    backoff would never grow, the log throttle would never engage, and
    HealthState would see an up/down transition every cycle.
    """
    probe = _ProbeBackoff()
    supervisor, health, _ = _make_supervisor(backoff=probe)
    service = FakeService(run_errors=[RuntimeError("serial read failed")] * 5000)
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.attempts.at(6).wait(), timeout=_FAILSAFE)
    assert probe.delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
    assert probe.resets == 0
    assert health.all_up() is False
    await _cancel(task)


async def test_watchdog_trips_on_a_flapping_component():
    """The watchdog must still fire when the component reconnects but never works."""
    clock = _StepClock(step=1.0)
    probe = _ProbeBackoff()
    supervisor, _, _ = _make_supervisor(backoff=probe, clock=clock)
    service = FakeService(run_errors=[RuntimeError("serial read failed")] * 5000)
    served = asyncio.create_task(supervisor.run_service(service))
    watched = asyncio.create_task(supervisor.watchdog_loop(threshold=25.0, interval=0))
    await asyncio.wait_for(watched, timeout=_FAILSAFE)
    assert supervisor.exit_reason == "watchdog"
    # The watchdog's shutdown also stops the component's supervision loop.
    assert await asyncio.wait_for(served, timeout=_FAILSAFE) is None


async def test_teardown_runs_when_a_healthy_component_is_shut_down(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_STABLE_SECONDS", 0.0)
    probe = _ProbeBackoff()
    supervisor, health, shutdown = _make_supervisor(backoff=probe)
    service = FakeService()
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.recoveries.at(1).wait(), timeout=_FAILSAFE)
    assert health.all_up() is True
    assert service.teardown_calls == 0
    shutdown.set()
    assert await asyncio.wait_for(task, timeout=_FAILSAFE) is None
    # A component stopped while healthy still has to release its handle.
    assert service.teardown_calls == 1
    assert supervisor.exit_reason is None


async def test_teardown_runs_when_the_supervision_task_is_cancelled(monkeypatch):
    monkeypatch.setattr(config, "SERVICE_STABLE_SECONDS", 0.0)
    probe = _ProbeBackoff()
    supervisor, _, _ = _make_supervisor(backoff=probe)
    service = FakeService()
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.recoveries.at(1).wait(), timeout=_FAILSAFE)
    await _cancel(task)
    # Cancellation still propagates, and the handle is still released.
    assert task.cancelled() is True
    assert service.teardown_calls == 1


async def test_teardown_is_not_truncated_by_a_second_cancellation(monkeypatch):
    """Releasing the handle has to finish even if the stop is insisted on twice."""
    monkeypatch.setattr(config, "SERVICE_STABLE_SECONDS", 0.0)
    probe = _ProbeBackoff()
    supervisor, _, _ = _make_supervisor(backoff=probe)
    service = SlowTeardownService()
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.recoveries.at(1).wait(), timeout=_FAILSAFE)

    task.cancel()
    await asyncio.wait_for(service.teardown_started.at(1).wait(), timeout=_FAILSAFE)
    task.cancel()  # A second cancel lands while teardown is still in flight.
    service.release()

    await asyncio.gather(task, return_exceptions=True)
    assert service.teardown_completed is True
    assert task.cancelled() is True


async def test_watchdog_trips_after_threshold():
    clock = _StepClock(step=1.0)
    supervisor, _, shutdown = _make_supervisor(clock=clock)
    task = asyncio.create_task(supervisor.watchdog_loop(threshold=3.0, interval=0))
    await asyncio.wait_for(task, timeout=_FAILSAFE)
    assert supervisor.exit_reason == "watchdog"
    assert shutdown.is_set() is True
    # One reading built the initial state, then exactly three inspections: the
    # watchdog held its fire at 1s and 2s and only tripped once it reached 3s.
    assert clock.readings.count == 4


async def test_watchdog_stays_quiet_while_components_are_up():
    clock = _StepClock(step=1.0)
    supervisor, health, shutdown = _make_supervisor(clock=clock)
    health.mark_up("device")
    task = asyncio.create_task(supervisor.watchdog_loop(threshold=3.0, interval=0))
    # Let the clock run far past the threshold; a healthy system never trips.
    await asyncio.wait_for(clock.readings.at(15).wait(), timeout=_FAILSAFE)
    assert shutdown.is_set() is False
    assert supervisor.exit_reason is None
    await _cancel(task)


class _StallHealth:
    """Minimal health double: answers only what the watchdog asks."""

    def __init__(
        self, down=0.0, stall=None, snapshot=None, churner=None, reconnects=None
    ):
        self.down = down
        self.stall = stall
        self.snapshot = snapshot
        self.churner = churner
        self.reconnects = reconnects if reconnects is not None else {}
        # Read by the watchdog's own log line, so the double has to carry it
        # exactly as the real HealthState does.
        self.churn_window = config.WATCHDOG_CHURN_WINDOW
        # Counts the inspections so a test can tell "the watchdog looked and
        # held its fire" apart from "the watchdog never got as far as looking",
        # which would otherwise satisfy the same assertions.
        self.down_reads = 0
        self.stall_reads = 0
        self.snapshot_reads = 0
        self.churn_reads = 0

    def down_duration(self):
        self.down_reads += 1
        return self.down

    def stall_duration(self):
        self.stall_reads += 1
        return self.stall

    def snapshot_age(self):
        self.snapshot_reads += 1
        return self.snapshot

    def churning(self, threshold):
        self.churn_reads += 1
        return self.churner

    def reconnect_counts(self):
        return self.reconnects


# The watchdog is driven with interval=0, so every scheduling step below is one
# or more full inspections. Fifty of them is far more cycles than the threshold
# under test needs, and none of them costs wall time.
_INERT_STEPS = 50
_MIN_INSPECTIONS = 5


async def test_watchdog_exits_when_the_stall_reading_crosses_its_threshold():
    """Renamed from ...when_the_snapshot_stops_being_refreshed: that is no
    longer true. `_StallHealth` sets `stall` directly, bypassing HealthState
    entirely, so this exercises the watchdog's own comparison against
    stall_duration() - a component loop gone quiet - not the file, which
    cannot produce this exit any more. See
    test_an_unwritable_snapshot_is_reported_but_does_not_exit for what an
    unwritable snapshot now does instead: reported, not exited on.
    """
    shutdown = asyncio.Event()
    health = _StallHealth(down=0.0, stall=300.0)
    sup = Supervisor(health, shutdown)
    await asyncio.wait_for(
        sup.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0),
        timeout=_FAILSAFE,
    )
    assert sup.exit_reason == "stalled"
    assert shutdown.is_set()


async def test_watchdog_ignores_a_system_that_has_never_been_healthy():
    # stall_duration() is None: hardware has not appeared yet. Exiting here
    # would be the startup deadline this project removed.
    shutdown = asyncio.Event()
    health = _StallHealth(down=0.0, stall=None)
    sup = Supervisor(health, shutdown)
    task = asyncio.create_task(
        sup.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0)
    )
    for _ in range(_INERT_STEPS):
        await asyncio.sleep(0)
    assert not task.done()
    assert sup.exit_reason is None
    # It kept inspecting and kept deciding not to act, rather than never
    # reaching the check at all.
    assert health.stall_reads >= _MIN_INSPECTIONS
    shutdown.set()
    await task


async def test_watchdog_holds_fire_below_the_stall_threshold():
    shutdown = asyncio.Event()
    health = _StallHealth(down=0.0, stall=239.0)
    sup = Supervisor(health, shutdown)
    task = asyncio.create_task(
        sup.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0)
    )
    for _ in range(_INERT_STEPS):
        await asyncio.sleep(0)
    assert not task.done()
    assert sup.exit_reason is None
    assert health.stall_reads >= _MIN_INSPECTIONS
    shutdown.set()
    await task


async def test_a_component_down_past_its_threshold_is_reported_as_down():
    """A component down and a stall can be reported at once - modelled here
    with a double that sets both readings directly, since a real HealthState
    keeps the stall reading gated on nothing being down. The reason recorded
    has to be the one that names what actually happened, or the logs send an
    operator looking for a block that is not there.

    This does not pin the order of the two checks: the stall check is gated on
    nothing being reported down, so with a component down it cannot fire
    whichever order they are in. The test below pins the order.
    """
    shutdown = asyncio.Event()
    health = _StallHealth(down=4000.0, stall=300.0)
    sup = Supervisor(health, shutdown)
    await asyncio.wait_for(
        sup.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0),
        timeout=_FAILSAFE,
    )
    assert sup.exit_reason == "watchdog"


async def test_the_down_reason_wins_when_both_criteria_are_met_at_once():
    """The two criteria are mutually exclusive at any sane configuration, so a
    down threshold of zero is the only way to satisfy both at the same moment
    and make the order observable. The down reason is the more specific of the
    two and must be the one recorded.
    """
    shutdown = asyncio.Event()
    health = _StallHealth(down=0.0, stall=300.0)
    sup = Supervisor(health, shutdown)
    await asyncio.wait_for(
        sup.watchdog_loop(threshold=0.0, interval=0.0, stall_threshold=240.0),
        timeout=_FAILSAFE,
    )
    assert sup.exit_reason == "watchdog"


async def test_a_component_that_is_down_is_not_also_reported_as_stalled():
    """The snapshot is only written while every component is up, so anything
    that is down stops it being refreshed as a matter of course. Treating that
    age as a stall would put a second ceiling on a reconnecting component, one
    fifteen times shorter than the tolerance chosen for it, and would blame a
    block for a failure that is nothing of the kind.
    """
    shutdown = asyncio.Event()
    # Well past the stall threshold, well inside the down threshold: exactly
    # the window a long reconnection sits in.
    health = _StallHealth(down=600.0, stall=600.0)
    sup = Supervisor(health, shutdown)
    task = asyncio.create_task(
        sup.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0)
    )
    for _ in range(_INERT_STEPS):
        await asyncio.sleep(0)
    assert not task.done()
    assert sup.exit_reason is None
    assert health.down_reads >= _MIN_INSPECTIONS
    shutdown.set()
    await task


async def test_the_watchdog_exits_when_one_component_stops_advancing(tmp_path):
    """The failure the shared snapshot cannot see, driven through the real
    HealthState rather than a double.

    The device loop stops advancing without raising, so it stays marked up and
    nothing is reported down. The Telegram loop carries on and keeps rewriting
    the snapshot, which is what makes the shared reading useless here: it never
    ages, so a criterion built on it alone reports a perfectly healthy process
    that is forwarding nothing.
    """
    clock = _ManualClock()
    health = HealthState(
        ["device", "telegram"],
        health_file=str(tmp_path / "healthy"),
        clock=clock,
    )
    shutdown = asyncio.Event()
    supervisor = Supervisor(health, shutdown)

    health.mark_up("device")
    health.mark_up("telegram")
    health.record_progress("device")
    health.record_progress("telegram")
    health.refresh_file()

    async def telegram_keeps_going():
        """One Telegram cycle per scheduling step, for as long as it takes."""
        while not shutdown.is_set():
            clock.advance(10.0)
            health.record_progress("telegram")
            health.refresh_file()
            await asyncio.sleep(0)

    driver = asyncio.create_task(telegram_keeps_going())
    watched = asyncio.create_task(
        supervisor.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0)
    )
    try:
        await asyncio.wait_for(watched, timeout=_FAILSAFE)
    finally:
        shutdown.set()
        await asyncio.gather(driver, return_exceptions=True)

    assert supervisor.exit_reason == "stalled"
    # Nothing was ever reported down, so this cannot have been the other
    # criterion wearing the wrong name.
    assert health.down_duration() == 0.0


class _CountingHealth(HealthState):
    """The real HealthState, counting the inspections the watchdog makes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stall_reads = 0

    def stall_duration(self):
        self.stall_reads += 1
        return super().stall_duration()


class _LogCapture:
    """Collect the supervisor's log records without printing them."""

    def __init__(self):
        from module import supervisor as supervisor_module

        self._logger = supervisor_module.logger
        self._handler = logging.Handler()
        self.records = []
        self._handler.emit = self.records.append
        self._level = self._logger.level

    def __enter__(self):
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc_info):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)
        return False

    @property
    def text(self):
        return " ".join(record.getMessage() for record in self.records)


_OUTAGE = 600.0


def _recovered_health(tmp_path):
    """A real HealthState that has just come back from a ten-minute outage.

    Nothing refreshed the snapshot while the device was down, because it is not
    written at all in that state, so the file's age at this moment is the whole
    outage. The Telegram loop ran throughout.
    """
    clock = _ManualClock()
    health = _CountingHealth(
        ["device", "telegram"],
        health_file=str(tmp_path / "healthy"),
        clock=clock,
    )
    for name in ("device", "telegram"):
        health.mark_up(name)
        health.record_progress(name)
    health.refresh_file()

    health.mark_down("device")
    clock.advance(_OUTAGE)
    health.record_progress("telegram")
    health.mark_up("device")
    return health, clock


async def test_a_recovery_does_not_read_as_a_stall(tmp_path):
    """The age an outage leaves on the snapshot must not be read as a stall the
    moment the component comes back, or the process is restarted for having
    reconnected. Driven through the real HealthState, which is where the moment
    of the recovery is known: an inspection here can only sample it.
    """
    health, clock = _recovered_health(tmp_path)
    shutdown = asyncio.Event()
    supervisor = Supervisor(health, shutdown)

    task = asyncio.create_task(
        supervisor.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0)
    )
    for _ in range(_INERT_STEPS):
        clock.advance(1.0)  # Well inside the threshold, and genuinely elapsing.
        await asyncio.sleep(0)

    assert not task.done()
    assert supervisor.exit_reason is None
    # It kept inspecting the recovered state rather than never looking at it.
    assert health.stall_reads >= _MIN_INSPECTIONS
    shutdown.set()
    await task


async def test_a_stall_beginning_after_a_recovery_is_still_caught(tmp_path):
    """Discounting the outage delays detection to one threshold past the
    recovery. It must not disable it: a component that comes back and then
    stops advancing is exactly the failure this criterion is for."""
    health, clock = _recovered_health(tmp_path)
    shutdown = asyncio.Event()
    supervisor = Supervisor(health, shutdown)

    async def time_passes():
        """Nothing advances and nothing is written; only the clock moves."""
        while not shutdown.is_set():
            clock.advance(10.0)
            await asyncio.sleep(0)

    driver = asyncio.create_task(time_passes())
    watched = asyncio.create_task(
        supervisor.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0)
    )
    try:
        await asyncio.wait_for(watched, timeout=_FAILSAFE)
    finally:
        shutdown.set()
        await asyncio.gather(driver, return_exceptions=True)

    assert supervisor.exit_reason == "stalled"


async def test_the_stall_line_names_the_figure_it_tripped_on(tmp_path):
    """The number printed beside the threshold has to be the number the
    threshold was compared against. An age carrying an outage that was
    discounted cannot be reconciled with the threshold beside it: it reads as a
    240s threshold that somehow took 900s to fire, and sends whoever is
    debugging it looking for a block that lasted that long.
    """
    health, clock = _recovered_health(tmp_path)
    clock.advance(300.0)
    shutdown = asyncio.Event()
    supervisor = Supervisor(health, shutdown)

    with _LogCapture() as log:
        await asyncio.wait_for(
            supervisor.watchdog_loop(
                threshold=3600.0, interval=0.0, stall_threshold=240.0
            ),
            timeout=_FAILSAFE,
        )

    assert supervisor.exit_reason == "stalled"
    assert "300s" in log.text
    assert "240s" in log.text
    assert f"{_OUTAGE + 300.0:.0f}s" not in log.text


def _legitimate_refresh_gap(cfg) -> float:
    """The longest gap the heartbeat can leave between two refreshes.

    Counted as the rounds it is made of, rather than as the closed form the
    configuration uses, so that the two have to agree about the loop rather
    than merely about each other.

    Only a round whose liveness probe went unanswered skips the refresh, and
    it costs the interval before it plus the single deadline it spent waiting.
    MODEM_PROBE_FAILURES - 1 of them can pass before the next gives up and
    raises. The round that does refresh costs an interval and two deadlines,
    because an answered liveness probe is followed by the registration probe,
    which waits the same deadline for its own answer. That holds whether or not
    MODEM_REGISTRATION_CHECK is on: the round that asks one question is the
    shorter of the two, and a threshold has to clear the longer one. Until the
    count runs out the component is working and simply has not refreshed the
    snapshot.
    """
    missed = max(0, cfg.MODEM_PROBE_FAILURES - 1) * (
        cfg.MODEM_PROBE_INTERVAL + cfg.MODEM_PROBE_TIMEOUT
    )
    refreshing = cfg.MODEM_PROBE_INTERVAL + 2 * cfg.MODEM_PROBE_TIMEOUT
    return missed + refreshing


def test_the_refresh_budget_is_the_worst_gap_the_heartbeat_can_leave():
    """Every floor below is measured against this figure, so it has to be the
    gap the loop can actually produce. Understating it - by counting one
    deadline for the round that asks two questions, or by leaving the probe's
    own write and lock outside any deadline at all, which is what made the gap
    unbounded before - puts the watchdog's threshold under the time a working
    component legitimately spends not refreshing the snapshot.
    """
    assert config.WATCHDOG_REFRESH_BUDGET == _legitimate_refresh_gap(config)


def test_watchdog_stall_threshold_clears_the_probes_own_retry_budget(monkeypatch):
    """The threshold is operator-settable, and any value inside that budget
    restarts a process that is only riding out a slow modem."""
    monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "0")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.WATCHDOG_STALL_SECONDS >= 2 * _legitimate_refresh_gap(reloaded)
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_the_stall_floor_still_clears_the_budget_at_the_shortest_window(monkeypatch):
    """The floor has to be derived from the same settings the budget is, not
    from the refresh interval alone. At the shortest configurable staleness
    window the probe interval is clamped down but the reply deadline is not,
    so a floor scaled off the interval alone lands under the budget and the
    watchdog restarts a working process.
    """
    monkeypatch.setenv("HEALTH_STALE_SECONDS", "2")
    monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "0")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.WATCHDOG_STALL_SECONDS >= 2 * _legitimate_refresh_gap(reloaded)
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def _legitimate_telegram_gap(cfg) -> float:
    """The longest gap the polling loop can leave between two progress reports.

    Counted as the calls it is made of rather than as the closed form the
    configuration uses, so the two have to agree about the loop rather than
    merely about each other.

    The loop reports when a poll returns and after each update it handles, so
    one gap holds at most one poll or the handling of one update. A poll costs
    a request deadline. Handling an update costs whatever its slowest handler
    does, and the slowest sends a message: one attempt per allowed try, each
    behind the same deadline, with a wait between them. The two are added
    because answering a button press and then replying to it does both.
    """
    poll = cfg.TELEGRAM_REQUEST_TIMEOUT
    send = cfg.TELEGRAM_SEND_ATTEMPTS * cfg.TELEGRAM_REQUEST_TIMEOUT + max(
        0, cfg.TELEGRAM_SEND_ATTEMPTS - 1
    ) * cfg.TELEGRAM_SEND_RETRY_DELAY
    return poll + send


def test_the_telegram_budget_is_the_worst_gap_the_polling_loop_can_leave():
    """The floor below is measured against this figure, so it has to be the gap
    the loop can actually produce. Understating it - by counting the poll alone
    and forgetting that one update can be retried behind three deadlines - puts
    the stall threshold under the time a working component legitimately spends
    between two reports."""
    assert config.TELEGRAM_PROGRESS_BUDGET == _legitimate_telegram_gap(config)


def test_the_stall_floor_clears_one_telegram_polling_iteration():
    """Now that each loop is measured on its own, the Telegram loop's own pace
    is load-bearing. It was not before: the device heartbeat rewrote the shared
    snapshot every half-minute whatever Telegram was doing, so a long poll never
    showed up in the reading. A threshold under one working iteration would
    restart the process for polling.

    Strictly greater, not merely at least. Two real handlers reach the budget
    exactly - answering a button press and then replying to it - and the
    watchdog compares with >=, so a threshold equal to the budget makes the
    worst legitimate gap a tripping gap.
    """
    assert config.WATCHDOG_STALL_SECONDS > config.TELEGRAM_PROGRESS_BUDGET


def test_the_stall_floor_clears_telegram_at_the_shortest_window(monkeypatch):
    """The floor's other terms are derived from the modem probe settings, which
    are clamped down by the staleness window. The Telegram loop is not clamped
    by anything, so at the shortest window a floor built from the probe alone
    lands under a single long poll."""
    monkeypatch.setenv("HEALTH_STALE_SECONDS", "2")
    monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "0")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.WATCHDOG_STALL_SECONDS > reloaded.TELEGRAM_PROGRESS_BUDGET
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_watchdog_stall_threshold_has_a_ceiling(monkeypatch):
    """A stall is a component lost without saying so. It must not go unnoticed
    for longer than the operator's stated tolerance for one that says so,
    which is what WATCHDOG_DOWN_SECONDS is.
    """
    monkeypatch.setenv("HEALTH_STALE_SECONDS", "10000")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.WATCHDOG_STALL_SECONDS <= reloaded.WATCHDOG_DOWN_SECONDS
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_the_stall_floor_outranks_the_ceiling(monkeypatch):
    """The two bounds can conflict, and which one gives has to be the safe one.
    Holding the threshold above the probe budget costs a slower restart;
    cutting it below restarts a process that is working.
    """
    monkeypatch.setenv("WATCHDOG_DOWN_SECONDS", "1")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.WATCHDOG_STALL_SECONDS >= 2 * _legitimate_refresh_gap(reloaded)
    finally:
        monkeypatch.undo()
        importlib.reload(config)


class _FakeMonotonic:
    """Stand-in for the `time` module as `_LogThrottle` uses it - only
    `.monotonic()` is ever called on it, and only this advances it, never a
    real clock.

    `_StallHealth`'s condition is a constant the test sets directly, unlike
    the real HealthState, whose age cannot cross a threshold before real time
    has covered the same span. Patching the module's `time` name lets a test
    reproduce that span deterministically instead of needing an actual wait,
    without adding a clock parameter to `_LogThrottle` itself - which is used
    elsewhere and has no other reason to take one.
    """

    def __init__(self, now: float = 0.0):
        self._now = now

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


async def test_an_unwritable_snapshot_is_reported_but_does_not_exit(monkeypatch):
    """A file that cannot be written is not a reason to restart: the loops are
    demonstrably running - that is what the fresh progress stamps say - and a
    restart cannot make the path writable. It repeats the whole startup every
    few minutes instead, which is a worse outcome than the fault. The
    healthcheck still fails on the file's own mtime, so the fault stays
    visible without anything being restarted for it.
    """
    from module import supervisor as supervisor_module

    fake_time = _FakeMonotonic()
    monkeypatch.setattr(supervisor_module, "time", fake_time)

    shutdown = asyncio.Event()
    health = _StallHealth(down=0.0, stall=0.0, snapshot=900.0)
    sup = Supervisor(health, shutdown)
    with _LogCapture() as log:
        task = asyncio.create_task(
            sup.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0)
        )
        # Let the loop construct its throttle - seeding it from the clock -
        # before that clock moves, the same order production runs in: the
        # throttle always exists well before the condition it gates could
        # ever be true.
        for _ in range(3):
            await asyncio.sleep(0)
        fake_time.advance(240.0)
        for _ in range(_INERT_STEPS):
            await asyncio.sleep(0)
        assert not task.done()
        assert sup.exit_reason is None
        assert health.snapshot_reads >= _MIN_INSPECTIONS
        shutdown.set()
        await task
    assert "HEALTH_FILE" in log.text
    assert any(record.levelno >= logging.ERROR for record in log.records)


async def test_the_unwritable_snapshot_report_is_throttled(monkeypatch):
    """One line as soon as the fault is noticed, then at most one more per
    stall_threshold after that - not one per inspection, which would flood a
    log kept on an in-memory filesystem."""
    from module import supervisor as supervisor_module

    fake_time = _FakeMonotonic()
    monkeypatch.setattr(supervisor_module, "time", fake_time)

    shutdown = asyncio.Event()
    health = _StallHealth(down=0.0, stall=0.0, snapshot=900.0)
    sup = Supervisor(health, shutdown)

    with _LogCapture() as log:

        def report_count() -> int:
            return len([r for r in log.records if "HEALTH_FILE" in r.getMessage()])

        task = asyncio.create_task(
            sup.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0)
        )
        for _ in range(3):
            await asyncio.sleep(0)
        fake_time.advance(240.0)
        for _ in range(_INERT_STEPS):
            await asyncio.sleep(0)
        assert report_count() == 1  # Noticed once, immediately.

        # Well inside the throttle window: however many more times the
        # watchdog inspects the same unwritable file, no second line yet.
        fake_time.advance(239.0)
        for _ in range(_INERT_STEPS):
            await asyncio.sleep(0)
        assert report_count() == 1

        # The window has now elapsed: exactly one more line, not one per
        # inspection since the first.
        fake_time.advance(1.0)
        for _ in range(_INERT_STEPS):
            await asyncio.sleep(0)
        assert report_count() == 2

        shutdown.set()
        await task


async def test_a_component_that_keeps_reconnecting_is_counted(tmp_path, monkeypatch):
    """The failure both other criteria are blind to, driven end to end.

    Each session is judged stable before it fails, which is the whole shape of
    this failure: that judgement re-stamps every clock the other two criteria
    measure from, so a component doing this for ever leaves down_duration() and
    stall_duration() both reading as if it had just come back.

    Synchronised on the supervisor's own mark_up rather than on elapsed time,
    so the session really has been judged stable before it ends, and the test
    terminates on a condition it controls.

    One half of a pair: the sibling below drives the same component through the
    same failure without ever letting a session reach that judgement, and
    requires the opposite result. Between them they say that what is counted is
    the judgement, not the connection that preceded it.
    """
    clock = _ManualClock()
    judged_stable = asyncio.Event()

    class SignallingHealth(HealthState):
        """The real HealthState, announcing the moment a session is accepted."""

        def mark_up(self, name):
            super().mark_up(name)
            judged_stable.set()

    health = SignallingHealth(
        ["device", "telegram"],
        health_file=str(tmp_path / "healthy"),
        clock=clock,
        churn_window=10_000.0,
    )
    # The window is what the session has to outlast to be accepted; zero makes
    # the acceptance happen on the next scheduling round instead of in a minute.
    monkeypatch.setattr(config, "SERVICE_STABLE_SECONDS", 0.0)

    shutdown = asyncio.Event()
    supervisor = Supervisor(health, shutdown)
    supervisor.backoff_factory = lambda: Backoff(minimum=0.0, maximum=0.0)

    sessions = 0

    class Flapper:
        name = "device"

        async def connect_once(self):
            return None

        async def run(self):
            nonlocal sessions
            sessions += 1
            judged_stable.clear()
            await judged_stable.wait()
            if sessions >= 4:
                shutdown.set()
            raise RuntimeError("the modem stopped answering")

        async def teardown(self):
            return None

    await asyncio.wait_for(supervisor.run_service(Flapper()), timeout=_FAILSAFE)
    assert sessions == 4
    assert health.reconnect_counts()["device"] == 4
    assert health.churning(threshold=4) == "device"


async def test_a_session_that_fails_before_it_is_judged_stable_is_not_counted(
    tmp_path, monkeypatch
):
    """The other half of the pair above, and what the criterion means.

    Connecting is not recovering. A session that ends before it has lasted
    SERVICE_STABLE_SECONDS never reaches mark_up, so it re-stamps nothing and
    the component's down clock goes on running across every one of these
    cycles - here from process start, because no session in this scenario ever
    reaches mark_up; in general from the last recovery, since mark_down
    re-stamps a component that was up. That is the criterion the failure
    belongs to, with the hour of tolerance chosen for it, and the known
    limitation beside WATCHDOG_CHURN_SESSIONS in config.py covers what a
    component that alternates between the two leaves behind.

    Counting these as well would put a second, far shorter ceiling underneath
    that hour - the count would reach its threshold inside one backoff ladder -
    and the process would exit and be restarted every few minutes for a fault
    that is already being measured, tearing down every other component with it
    on each pass.

    No real time passes either way: the session always ends on its own failure,
    so the stable window is never waited out, however long it is set to.
    """
    clock = _ManualClock()
    health = HealthState(
        ["device", "telegram"],
        health_file=str(tmp_path / "healthy"),
        clock=clock,
        churn_window=10_000.0,
    )
    monkeypatch.setattr(config, "SERVICE_STABLE_SECONDS", 3600.0)

    shutdown = asyncio.Event()
    supervisor = Supervisor(health, shutdown)
    supervisor.backoff_factory = lambda: Backoff(minimum=0.0, maximum=0.0)

    sessions = 0

    class FailsBeforeItSettles:
        name = "device"

        async def connect_once(self):
            return None

        async def run(self):
            nonlocal sessions
            sessions += 1
            # The clock moves only where this test moves it, and it moves here
            # so the criterion that does cover this shape has something to
            # accumulate.
            clock.advance(10.0)
            if sessions >= 20:
                shutdown.set()
            raise RuntimeError("the API rejected the poll")

        async def teardown(self):
            return None

    await asyncio.wait_for(
        supervisor.run_service(FailsBeforeItSettles()), timeout=_FAILSAFE
    )
    assert sessions == 20
    assert health.reconnect_counts()["device"] == 0
    assert health.churning(threshold=2) is None
    # Twenty sessions ended and the down clock never lost a second of them,
    # which is what makes not counting them safe rather than merely quieter.
    assert health.down_duration() == 200.0


async def test_a_component_that_never_connects_is_never_counted(tmp_path):
    """The safety property this criterion stands on.

    A USB modem can appear minutes after the container does, and connect_once()
    fails on every attempt until it does. Counting those would put a ceiling on
    how long this process may wait for its hardware - which is the startup
    deadline this project exists to remove, arrived at from a new direction.
    """
    clock = _ManualClock()
    health = HealthState(
        ["device", "telegram"],
        health_file=str(tmp_path / "healthy"),
        clock=clock,
        churn_window=10_000.0,
    )
    shutdown = asyncio.Event()
    supervisor = Supervisor(health, shutdown)
    supervisor.backoff_factory = lambda: Backoff(minimum=0.0, maximum=0.0)

    attempts = 0

    class NeverArrives:
        name = "device"

        async def connect_once(self):
            nonlocal attempts
            attempts += 1
            if attempts >= 50:
                shutdown.set()
            raise FileNotFoundError("the port is not present yet")

        async def run(self):  # pragma: no cover - never reached
            raise AssertionError("run must not be reached")

        async def teardown(self):
            return None

    await asyncio.wait_for(supervisor.run_service(NeverArrives()), timeout=_FAILSAFE)
    assert attempts >= 50
    assert health.reconnect_counts()["device"] == 0
    assert health.churning(threshold=2) is None


async def test_the_watchdog_exits_on_a_churning_component():
    shutdown = asyncio.Event()
    health = _StallHealth(
        down=0.0, stall=0.0, churner="device", reconnects={"device": 5}
    )
    sup = Supervisor(health, shutdown)
    with _LogCapture() as log:
        await asyncio.wait_for(
            sup.watchdog_loop(
                threshold=3600.0,
                interval=0.0,
                stall_threshold=240.0,
                churn_sessions=5,
            ),
            timeout=_FAILSAFE,
        )
    assert sup.exit_reason == "churning"
    assert "device" in log.text


async def test_the_watchdog_holds_fire_below_the_churn_threshold():
    shutdown = asyncio.Event()
    health = _StallHealth(down=0.0, stall=0.0, churner=None)
    sup = Supervisor(health, shutdown)
    task = asyncio.create_task(
        sup.watchdog_loop(
            threshold=3600.0, interval=0.0, stall_threshold=240.0, churn_sessions=5
        )
    )
    for _ in range(_INERT_STEPS):
        await asyncio.sleep(0)
    assert not task.done()
    assert sup.exit_reason is None
    assert health.churn_reads >= _MIN_INSPECTIONS
    shutdown.set()
    await task


async def test_the_churn_line_names_the_count_it_tripped_on():
    """The number printed beside the threshold has to be the number the
    threshold was compared against, exactly as
    test_the_stall_line_names_the_figure_it_tripped_on requires of the sibling
    line.

    The watchdog only inspects every WATCHDOG_CHECK_INTERVAL, so a component
    flapping faster than that can end several sessions between two looks and
    the count is then well above the threshold. Printing the threshold in the
    count's place understates by an unbounded amount, and tells an operator the
    component reconnected the minimum number of times that would have tripped
    it rather than what it actually did.
    """
    shutdown = asyncio.Event()
    health = _StallHealth(
        down=0.0, stall=0.0, churner="device", reconnects={"device": 9}
    )
    sup = Supervisor(health, shutdown)
    with _LogCapture() as log:
        await asyncio.wait_for(
            sup.watchdog_loop(
                threshold=3600.0,
                interval=0.0,
                stall_threshold=240.0,
                churn_sessions=5,
            ),
            timeout=_FAILSAFE,
        )
    assert sup.exit_reason == "churning"
    assert "9 connected sessions" in log.text
    # The threshold still has to be there to read the count against - it is
    # just no longer standing in for it.
    assert "threshold 5" in log.text


async def test_the_churn_line_does_not_diagnose_the_other_two_clocks():
    """Every counted session was judged recovered, so each one did re-stamp the
    other two clocks - but a component that churns spends the gaps between its
    sessions down, and this inspection can land in one of them, which is the
    state set up here. A line announcing that the other two clocks read zero
    would then be false at the moment it was printed, about the very fault
    being debugged, so the line states the criterion instead.
    """
    shutdown = asyncio.Event()
    health = _StallHealth(
        down=120.0, stall=0.0, churner="device", reconnects={"device": 5}
    )
    sup = Supervisor(health, shutdown)
    with _LogCapture() as log:
        await asyncio.wait_for(
            sup.watchdog_loop(
                threshold=3600.0,
                interval=0.0,
                stall_threshold=240.0,
                churn_sessions=5,
            ),
            timeout=_FAILSAFE,
        )
    assert sup.exit_reason == "churning"
    assert "down clock" not in log.text
    assert "stall clock" not in log.text
