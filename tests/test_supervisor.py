import asyncio
import importlib
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

    def __init__(self, down=0.0, stall=None):
        self.down = down
        self.stall = stall
        # Counts the inspections so a test can tell "the watchdog looked and
        # held its fire" apart from "the watchdog never got as far as looking",
        # which would otherwise satisfy the same assertions.
        self.down_reads = 0
        self.stall_reads = 0

    def down_duration(self):
        self.down_reads += 1
        return self.down

    def stall_duration(self):
        self.stall_reads += 1
        return self.stall


# The watchdog is driven with interval=0, so every scheduling step below is one
# or more full inspections. Fifty of them is far more cycles than the threshold
# under test needs, and none of them costs wall time.
_INERT_STEPS = 50
_MIN_INSPECTIONS = 5


async def test_watchdog_exits_when_the_snapshot_stops_being_refreshed():
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


async def test_the_down_threshold_still_wins_when_both_apply():
    shutdown = asyncio.Event()
    health = _StallHealth(down=4000.0, stall=300.0)
    sup = Supervisor(health, shutdown)
    await asyncio.wait_for(
        sup.watchdog_loop(threshold=3600.0, interval=0.0, stall_threshold=240.0),
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


def test_watchdog_stall_threshold_has_a_floor(monkeypatch):
    """The threshold is operator-settable, and zero would make it fire the
    moment the first snapshot is written, killing a working process."""
    monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "0")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.WATCHDOG_STALL_SECONDS >= 4 * reloaded.MODEM_PROBE_INTERVAL
    finally:
        monkeypatch.undo()
        importlib.reload(config)
