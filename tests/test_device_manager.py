import asyncio
import logging
import time

import pytest

import config
from module.device_manager import DeviceManager


class FakeReader:
    """Yields queued lines, then hangs to simulate a silent modem."""

    def __init__(self, lines=None):
        self.lines = list(lines or [])

    async def readline(self):
        if not self.lines:
            await asyncio.sleep(3600)
        return self.lines.pop(0)


class DelayedReader(FakeReader):
    """Answers only after a delay, like a command the modem processes slowly.

    The delay is the one place these tests touch real time. _send_and_wait has
    no injectable wait to borrow -- its deadline is enforced by asyncio.wait_for
    over the reader -- so the only way to exercise the deadline is a reader that
    genuinely answers late. The delay sits far from both deadlines it is
    compared against, so the outcome does not depend on machine speed.
    """

    RESPONSE_DELAY = 0.05

    async def readline(self):
        await asyncio.sleep(self.RESPONSE_DELAY)
        return await super().readline()


class FakeWriter:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class ClosedReader:
    """A stream at end of file: readline yields empty bytes, over and over."""

    async def readline(self):
        return b""


async def _noop_callback(*args, **kwargs):
    return True


def _make(lines=None, port="/hostdev/ttyUSB2"):
    manager = DeviceManager(_noop_callback, port=port)
    manager.reader = FakeReader(lines)
    manager.writer = FakeWriter()
    return manager


async def test_send_and_wait_returns_on_ok():
    manager = _make([b"\r\n", b"OK\r\n"])
    assert await manager._send_and_wait("AT", timeout=1.0) == [b"OK"]
    assert manager.writer.written == [b"AT\r\n"]


async def test_send_and_wait_collects_lines_before_ok():
    manager = _make([b"+CSQ: 21,99\r\n", b"OK\r\n"])
    assert await manager._send_and_wait("AT+CSQ", timeout=1.0) == [b"+CSQ: 21,99", b"OK"]


async def test_send_and_wait_returns_on_error():
    manager = _make([b"ERROR\r\n"])
    assert await manager._send_and_wait("AT+BAD", timeout=1.0) == [b"ERROR"]


async def test_send_and_wait_returns_on_cme_error():
    manager = _make([b"+CME ERROR: 10\r\n"])
    assert await manager._send_and_wait("AT+BAD", timeout=1.0) == [b"+CME ERROR: 10"]


async def test_send_and_wait_times_out_without_raising():
    manager = _make([])
    assert await manager._send_and_wait("AT", timeout=0.05) == []


async def test_send_and_wait_returns_as_soon_as_the_modem_answers():
    """The defect being guarded is a fixed two second sleep per command.

    Nothing here waits: this is an upper bound on a call that should take
    microseconds, generous enough that only a fixed delay can breach it.
    """
    manager = _make([b"OK\r\n"])
    started = time.monotonic()
    assert await manager._send_and_wait("AT", timeout=30.0) == [b"OK"]
    assert time.monotonic() - started < 1.0


async def test_send_and_wait_clips_a_slow_command_at_a_short_deadline():
    manager = _make()
    manager.reader = DelayedReader([b"OK\r\n"])
    assert await manager._send_and_wait("AT&F", timeout=0.01) == []


async def test_send_and_wait_gives_a_slow_command_the_longer_deadline():
    """AT&F, AT+CFUN and AT&W answer late; the slow deadline must not clip them."""
    manager = _make()
    manager.reader = DelayedReader([b"OK\r\n"])
    assert config.AT_SLOW_COMMAND_TIMEOUT > config.AT_COMMAND_TIMEOUT
    answer = await manager._send_and_wait("AT&F", timeout=config.AT_SLOW_COMMAND_TIMEOUT)
    assert answer == [b"OK"]


async def test_send_and_wait_reports_a_command_that_never_answers():
    """Hitting the deadline must be visible, not silently swallowed."""
    from module import device_manager as dm_module

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    dm_module.logger.addHandler(handler)
    try:
        manager = _make([])
        assert await manager._send_and_wait("AT+CPIN?", timeout=0.05) == []
    finally:
        dm_module.logger.removeHandler(handler)

    assert any(
        record.levelno >= logging.WARNING and "AT+CPIN?" in record.getMessage()
        for record in records
    )


async def test_send_and_wait_raises_when_the_stream_closes():
    """A lost device must be reported at once.

    End of file is returned instantly and repeatedly, so counting it as a blank
    line would burn the whole deadline in a full-speed loop.
    """
    manager = _make()
    manager.reader = ClosedReader()
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        await manager._send_and_wait("AT", timeout=5.0)
    assert time.monotonic() - started < 1.0


async def test_probe_modem_accepts_ok():
    manager = _make([b"OK\r\n"])
    await manager._probe_modem()


async def test_probe_modem_raises_when_silent():
    manager = _make([])
    manager.probe_timeout = 0.05
    with pytest.raises(RuntimeError):
        await manager._probe_modem()


async def test_wait_for_port_returns_immediately_when_present():
    manager = _make()
    manager._port_exists = lambda path: True
    await asyncio.wait_for(manager._wait_for_port("/hostdev/ttyUSB2"), timeout=1.0)


async def test_wait_for_port_waits_without_a_deadline():
    """Reproduces the root cause: the container may start ~30s before the device."""
    manager = _make()
    calls = {"n": 0}

    def exists(path):
        calls["n"] += 1
        return calls["n"] > 5

    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    manager._port_exists = exists
    manager._sleep = fake_sleep

    await asyncio.wait_for(manager._wait_for_port("/hostdev/ttyUSB2"), timeout=2.0)
    assert calls["n"] == 6
    assert len(slept) == 5
    assert slept == sorted(slept)
    assert max(slept) <= 30.0 * 1.2


async def test_wait_for_port_has_no_retry_ceiling():
    """A bounded retry count is exactly the defect this method exists to remove.

    Five hundred negative checks is far past any plausible ceiling, and the
    delay must stay capped: waiting forever must not mean waiting ever longer.
    """
    manager = _make()
    checks = {"n": 0}

    def exists(path):
        checks["n"] += 1
        return checks["n"] > 500

    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    manager._port_exists = exists
    manager._sleep = fake_sleep

    await asyncio.wait_for(manager._wait_for_port("/hostdev/ttyUSB2"), timeout=5.0)
    assert len(slept) == 500
    assert max(slept) <= config.RECONNECT_BACKOFF_MAX * 1.2


async def test_resolve_port_uses_explicit_setting():
    manager = DeviceManager(_noop_callback, port="/hostdev/ttyUSB7")
    manager._port_exists = lambda path: True
    assert await manager.resolve_port() == "/hostdev/ttyUSB7"


async def test_resolve_port_falls_back_to_discovery(monkeypatch):
    from module import device_manager as dm_module

    async def fake_discover(dev_root, baudrate, timeout, **kwargs):
        return "/hostdev/ttyUSB2"

    monkeypatch.setattr(dm_module, "discover_port", fake_discover)
    manager = DeviceManager(_noop_callback, port="")
    assert await manager.resolve_port() == "/hostdev/ttyUSB2"


async def test_resolve_port_raises_when_discovery_finds_nothing(monkeypatch):
    from module import device_manager as dm_module

    async def fake_discover(dev_root, baudrate, timeout, **kwargs):
        return None

    monkeypatch.setattr(dm_module, "discover_port", fake_discover)
    manager = DeviceManager(_noop_callback, port="")
    with pytest.raises(RuntimeError):
        await manager.resolve_port()
