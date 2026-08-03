"""Tests for the process assembly.

Nothing here opens a serial port, a socket or a device node: run() is driven
with stand-in components. Every wait is bounded by a condition the test itself
controls, and no test spends a real interval to synchronise.
"""

import asyncio
import signal
import time

import pytest

import config
import main
from module.health import HealthState
from module.supervisor import FatalConfigError, Supervisor

# Failsafe only. Every test below finishes on an explicit signal raised by the
# code under test, so this bound just turns a hang caused by a regression into
# a failure instead of a stuck run.
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


class _ZeroBackoff:
    """Backoff stand-in that hands out no delay at all.

    The schedule itself is covered in tests/test_supervisor.py; here it would
    only make the tests wait.
    """

    def next_delay(self) -> float:
        return 0.0

    def reset(self) -> None:
        pass


class _FakeComponent:
    """A managed component whose connection outcome the test scripts."""

    def __init__(self, name, connect_error=None):
        self.name = name
        self._connect_error = connect_error
        self.connects = _Milestone()
        self.serving = _Milestone()
        self.teardowns = _Milestone()
        # Parked on an event rather than a timer: a real run body never
        # returns, and an event leaves no pending timer behind when cancelled.
        self._park = asyncio.Event()

    async def connect_once(self):
        self.connects.tick()
        if self._connect_error is not None:
            raise self._connect_error

    async def run(self):
        self.serving.tick()
        await self._park.wait()

    async def teardown(self):
        self.teardowns.tick()


class _StalledComponent(_FakeComponent):
    """A component parked where a real one waits for its dependency.

    Waiting for a device node to appear happens inside connect_once(), which
    the supervision loop awaits, so neither the component nor the loop can
    observe a stop request while it is going on. Only cancellation ends it.
    """

    def __init__(self, name):
        super().__init__(name)
        self._stall = asyncio.Event()  # Never set: the dependency never arrives.

    async def connect_once(self):
        self.connects.tick()
        await self._stall.wait()


def _install(monkeypatch, tmp_path, device, telegram):
    """Replace the real components with stand-ins, keeping the real supervisor."""
    health = HealthState(
        [device.name, telegram.name], health_file=str(tmp_path / "healthy")
    )
    shutdown = asyncio.Event()
    supervisor = Supervisor(health, shutdown)
    supervisor.backoff_factory = _ZeroBackoff
    parts = (health, supervisor, device, telegram, shutdown)
    monkeypatch.setattr(main, "build_services", lambda: parts)
    return parts


# --- Wiring ----------------------------------------------------------------


def test_builds_two_supervised_components():
    health, supervisor, device, telegram, shutdown = main.build_services()
    assert device.name == "device"
    assert telegram.name == "telegram"
    assert health.all_up() is False
    assert supervisor.shutdown_event is shutdown


def test_both_components_share_one_health_state():
    health, _, device, telegram, _ = main.build_services()
    assert device.health is health
    assert telegram.health is health


def test_registered_names_match_the_health_state():
    health, _, device, telegram, _ = main.build_services()
    assert set(main.SERVICE_NAMES) == {device.name, telegram.name}
    assert set(health.snapshot()["services"]) == set(main.SERVICE_NAMES)


def test_both_callback_directions_are_wired():
    """Inbound goes to Telegram, outbound goes to the modem. A broken
    direction fails silently, so it is asserted explicitly."""
    _, _, device, telegram, _ = main.build_services()
    assert device.receive_sms_callback == telegram.handle_forwarding_sms
    assert telegram.send_sms_callback == device.handle_send_sms


async def test_device_state_notifications_reach_telegram():
    """Asserted by sending one, because the device side is wrapped: what has
    to hold is that the text arrives, not which object it was handed to."""
    _, _, device, telegram, _ = main.build_services()
    delivered = []

    async def capture(text):
        delivered.append(text)

    telegram.notify = capture
    await device.notify("modem connected")
    assert delivered == ["modem connected"]


async def test_a_stalled_notification_cannot_hold_up_the_caller(monkeypatch):
    """The device path awaits a notification when it connects and again while
    it is shutting down. The Telegram client retries a failed send several
    times with a delay between attempts, so an unreachable API would slow every
    reconnect cycle and could hold a stopping process past the point where the
    container runtime stops waiting and kills it.
    """
    monkeypatch.setattr(config, "NOTIFY_TIMEOUT", 0.01)
    _, _, device, telegram, _ = main.build_services()
    stalled = asyncio.Event()  # Never set: the send never completes.

    async def never_returns(text):
        await stalled.wait()

    telegram.notify = never_returns
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        # The outer bound is only a failsafe, so that an assembly with no
        # deadline of its own fails instead of hanging. The elapsed reading
        # below is what tells the two apart: the configured deadline is three
        # orders of magnitude shorter than the failsafe.
        await asyncio.wait_for(device.notify("modem connected"), timeout=_FAILSAFE)
    assert time.monotonic() - started < _FAILSAFE / 2


def test_the_notify_deadline_has_a_floor(monkeypatch):
    """The deadline is operator-settable, and zero would fail every
    notification before it was even attempted."""
    import importlib

    monkeypatch.setenv("NOTIFY_TIMEOUT", "0")
    try:
        assert importlib.reload(config).NOTIFY_TIMEOUT >= 1.0
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_the_devices_write_only_running_flag_is_gone():
    """DeviceManager.is_running had exactly one reader, the readiness gate in
    the old assembly. With the gate gone it was written and never read, which
    reads as live state. The Telegram client's flag of the same name is not
    the same case: its polling loop still reads it."""
    _, _, device, telegram, _ = main.build_services()
    assert not hasattr(device, "is_running")
    assert hasattr(telegram, "is_running")


def test_the_readiness_gate_is_gone():
    """Initialisation used to take a fixed thirty-eight seconds against a
    forty second deadline, so a single serial retry guaranteed a timeout."""
    _, _, device, telegram, _ = main.build_services()
    assert not hasattr(main, "SMSForwarder")
    for component in (device, telegram):
        assert not hasattr(component, "priming_event")


# --- Lifecycle -------------------------------------------------------------


async def test_every_component_is_supervised_without_waiting_for_the_others(
    monkeypatch, tmp_path
):
    """The old assembly started the modem, waited for it to report ready and
    only then started the Telegram client, so a modem that had not enumerated
    yet meant the bot never started at all. Supervision has to begin for both
    regardless of what either one is doing, and a component that cannot connect
    must not take the other one, or the process, down with it.
    """
    device = _FakeComponent("device", connect_error=OSError("device node absent"))
    telegram = _FakeComponent("telegram")
    _, supervisor, _, _, shutdown = _install(monkeypatch, tmp_path, device, telegram)

    task = asyncio.create_task(main.run())
    # The Telegram client is serving while the modem has failed repeatedly.
    await asyncio.wait_for(telegram.serving.at(1).wait(), timeout=_FAILSAFE)
    await asyncio.wait_for(device.connects.at(5).wait(), timeout=_FAILSAFE)
    assert task.done() is False
    assert supervisor.exit_reason is None

    shutdown.set()
    assert await asyncio.wait_for(task, timeout=_FAILSAFE) == 0


async def test_nothing_gives_up_while_a_dependency_is_missing(monkeypatch, tmp_path):
    """A container is routinely created before its USB device has enumerated
    and before its outbound proxy answers. Fifty consecutive failures on both
    components and the process is still trying: there is no attempt ceiling and
    nothing decided that waiting was an error.
    """
    device = _FakeComponent("device", connect_error=OSError("device node absent"))
    telegram = _FakeComponent("telegram", connect_error=ConnectionError("proxy refused"))
    health, supervisor, _, _, shutdown = _install(
        monkeypatch, tmp_path, device, telegram
    )

    task = asyncio.create_task(main.run())
    await asyncio.wait_for(device.connects.at(50).wait(), timeout=_FAILSAFE)
    await asyncio.wait_for(telegram.connects.at(50).wait(), timeout=_FAILSAFE)
    assert task.done() is False
    assert supervisor.exit_reason is None
    assert health.all_up() is False

    shutdown.set()
    assert await asyncio.wait_for(task, timeout=_FAILSAFE) == 0


async def test_a_stop_request_reaches_a_component_parked_on_its_dependency(
    monkeypatch, tmp_path
):
    """The state a cold-booted container spends its first seconds in: no device
    node, so the modem component is parked waiting for one. Stopping then must
    not wait for that component to come back on its own, because it cannot -
    the wait is inside the attempt the supervision loop is awaiting, and a real
    one lasts as long as a full reconnection backoff. The container runtime
    kills a process that outlives its stop grace period, so a stop that waits
    is a stop that never completes.
    """
    device = _StalledComponent("device")
    telegram = _FakeComponent("telegram")
    _, supervisor, _, _, shutdown = _install(monkeypatch, tmp_path, device, telegram)

    task = asyncio.create_task(main.run())
    await asyncio.wait_for(device.connects.at(1).wait(), timeout=_FAILSAFE)
    await asyncio.wait_for(telegram.serving.at(1).wait(), timeout=_FAILSAFE)

    shutdown.set()
    assert await asyncio.wait_for(task, timeout=_FAILSAFE) == 0
    assert supervisor.exit_reason is None
    # Still torn down: the handle has to be released even though the attempt
    # that would have opened it never finished.
    assert device.teardowns.count >= 1
    assert telegram.teardowns.count >= 1


async def test_sigterm_shuts_the_process_down_cleanly(monkeypatch, tmp_path):
    """The container runtime stops this process by signalling it, so the signal
    has to reach the shutdown event rather than being left to the default
    disposition, which would kill the process outright."""
    device = _FakeComponent("device")
    telegram = _FakeComponent("telegram")
    health, supervisor, _, _, _ = _install(monkeypatch, tmp_path, device, telegram)
    health_file = tmp_path / "healthy"
    health_file.write_text("{}")

    task = asyncio.create_task(main.run())
    await asyncio.wait_for(device.serving.at(1).wait(), timeout=_FAILSAFE)
    await asyncio.wait_for(telegram.serving.at(1).wait(), timeout=_FAILSAFE)

    # Checked before the signal is raised, not after: an assembly that never
    # installed a handler would leave the default disposition in place and the
    # signal would take the test runner down with it.
    assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
    signal.raise_signal(signal.SIGTERM)

    assert await asyncio.wait_for(task, timeout=_FAILSAFE) == 0
    assert supervisor.exit_reason is None
    assert device.teardowns.count >= 1
    assert telegram.teardowns.count >= 1
    # A stopping process must stop claiming to be healthy.
    assert health_file.exists() is False
    assert health.all_up() is False


async def test_a_tripped_watchdog_exits_one(monkeypatch, tmp_path):
    """Also proves the watchdog is started at all: the stand-in below only runs
    if run() creates that task. When it fires is settled in
    tests/test_supervisor.py; what is under test here is the exit code, because
    only a non-zero one makes the container runtime restart everything."""
    device = _FakeComponent("device")
    telegram = _FakeComponent("telegram")
    _, supervisor, _, _, shutdown = _install(monkeypatch, tmp_path, device, telegram)
    tripped = _Milestone()

    async def trip():
        supervisor.exit_reason = "watchdog"
        shutdown.set()
        tripped.tick()

    monkeypatch.setattr(supervisor, "watchdog_loop", trip)

    assert await asyncio.wait_for(main.run(), timeout=_FAILSAFE) == 1
    assert tripped.count == 1
    assert device.teardowns.count >= 1


async def test_a_configuration_error_exits_two(monkeypatch, tmp_path):
    """Restarting cannot supply a token that was never configured, so this is
    the one failure that has to be told apart from every other one."""
    device = _FakeComponent("device")
    telegram = _FakeComponent(
        "telegram", connect_error=FatalConfigError("BOT_TOKEN is not configured")
    )
    _, supervisor, _, _, _ = _install(monkeypatch, tmp_path, device, telegram)

    assert await asyncio.wait_for(main.run(), timeout=_FAILSAFE) == 2
    assert supervisor.exit_reason == "fatal_config"
    # The other component still gets its handle released.
    assert device.teardowns.count >= 1


async def test_an_unexpected_supervision_failure_exits_one(monkeypatch, tmp_path):
    """Nothing should reach this path, which is exactly why it must not be
    reported as a clean exit: a supervisor that died is not a shutdown."""
    device = _FakeComponent("device")
    telegram = _FakeComponent("telegram")
    _, supervisor, _, _, _ = _install(monkeypatch, tmp_path, device, telegram)

    async def collapse():
        raise RuntimeError("supervision task collapsed")

    monkeypatch.setattr(supervisor, "watchdog_loop", collapse)

    assert await asyncio.wait_for(main.run(), timeout=_FAILSAFE) == 1
    assert supervisor.exit_reason is None
    assert device.teardowns.count >= 1
    assert telegram.teardowns.count >= 1
