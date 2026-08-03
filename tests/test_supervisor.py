import asyncio
import time

import pytest

from module.health import HealthState
from module.supervisor import Backoff, FatalConfigError, Supervisor


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

    def next_delay(self) -> float:
        self.delays.append(self._inner.next_delay())
        self.attempts.tick()
        return 0.0

    def reset(self) -> None:
        self.resets += 1
        self._inner.reset()


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
        # Set once a run body gets past its scripted errors, which is the
        # signal that the component is connected and serving.
        self.running = asyncio.Event()
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
        self.running.set()
        # Park on an event rather than a timer: a real long-running body never
        # returns, and an event leaves no pending timer behind when cancelled.
        await self._park.wait()

    async def teardown(self):
        self.teardown_calls += 1


def _make_supervisor(names=("device",), backoff=None, clock=time.monotonic):
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


async def test_retries_with_backoff_until_connected():
    probe = _ProbeBackoff()
    supervisor, health, _ = _make_supervisor(backoff=probe)
    service = FakeService(connect_errors=[OSError("no device"), OSError("no device"), None])
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(service.running.wait(), timeout=_FAILSAFE)
    assert service.connect_calls == 3
    assert health.all_up() is True
    # Waiting for a dependency is not a failure: nothing decided to exit.
    assert supervisor.exit_reason is None
    # The delay grew between the two failed attempts.
    assert probe.delays == [1.0, 2.0]
    assert probe.resets == 1
    await _cancel(task)


async def test_runtime_error_triggers_teardown_and_reconnect():
    probe = _ProbeBackoff()
    supervisor, _, _ = _make_supervisor(backoff=probe)
    service = FakeService(run_errors=[RuntimeError("serial read failed")])
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(service.running.wait(), timeout=_FAILSAFE)
    assert service.teardown_calls >= 1
    assert service.connect_calls >= 2
    assert supervisor.exit_reason is None
    await _cancel(task)


async def test_backoff_resets_after_a_successful_connection():
    probe = _ProbeBackoff()
    supervisor, _, _ = _make_supervisor(backoff=probe)
    service = FakeService(
        connect_errors=[OSError("no device"), OSError("no device"), None],
        run_errors=[RuntimeError("serial read failed")],
    )
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(service.running.wait(), timeout=_FAILSAFE)
    # Two failures grew the delay; the connection that followed reset it, so
    # the failure after it starts again from the minimum instead of the cap.
    assert probe.delays == [1.0, 2.0, 1.0]
    assert probe.resets == 2
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
    supervisor, health, shutdown = _make_supervisor(backoff=probe)
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


async def test_failure_marks_the_component_down():
    probe = _ProbeBackoff()
    supervisor, health, _ = _make_supervisor(backoff=probe, clock=_StepClock())
    service = FakeService(run_errors=[RuntimeError("serial read failed")] * 500)
    task = asyncio.create_task(supervisor.run_service(service))
    await asyncio.wait_for(probe.attempts.at(3).wait(), timeout=_FAILSAFE)
    assert health.down_duration() > 0
    assert supervisor.exit_reason is None
    await _cancel(task)


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
