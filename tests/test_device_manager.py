import asyncio
import logging
import time
from datetime import datetime

import pytest
from gsmmodem.pdu import encodeSmsSubmitPdu

import config
from module.device_manager import DeviceManager

# Self-generated PDU with meaningless body text. Never use real message content.
STORED_PDU = encodeSmsSubmitPdu("+8613800138000", "TESTMSG")[0].data.hex().upper()

# A DELIVER PDU carrying a service-centre timestamp of 2024-11-02 13:15:51 +08.
# Unlike a SUBMIT PDU this one actually has a carrier timestamp, which is what
# makes it usable for asserting that the timestamp survives the drain.
DELIVER_PDU = "0791683108200155040D91683103943254F60008421120315115238A02597D"


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
    assert max(slept) <= config.RECONNECT_BACKOFF_MAX * 1.2


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


async def test_resolve_port_uses_explicit_setting(monkeypatch):
    from module import device_manager as dm_module

    # Without this, a regression in the explicit-port branch would silently
    # fall through to real discovery and scan the machine's own device tree.
    async def fail_discover(*args, **kwargs):
        raise AssertionError("discovery must not run when a port is configured")

    monkeypatch.setattr(dm_module, "discover_port", fail_discover)
    manager = DeviceManager(_noop_callback, port="/hostdev/ttyUSB7")
    assert await manager.resolve_port() == "/hostdev/ttyUSB7"


async def test_resolve_port_falls_back_to_discovery(monkeypatch):
    from module import device_manager as dm_module

    async def fake_discover(dev_root, baudrate, timeout, **kwargs):
        return "/hostdev/ttyUSB2"

    # An empty port argument falls back to the module level SMS_PORT, so the
    # test pins it instead of relying on that variable being unset in whatever
    # shell or job runs the suite.
    monkeypatch.setattr(dm_module, "SMS_PORT", "")
    monkeypatch.setattr(dm_module, "discover_port", fake_discover)
    manager = DeviceManager(_noop_callback, port="")
    assert await manager.resolve_port() == "/hostdev/ttyUSB2"


async def test_resolve_port_raises_when_discovery_finds_nothing(monkeypatch):
    from module import device_manager as dm_module

    async def fake_discover(dev_root, baudrate, timeout, **kwargs):
        return None

    monkeypatch.setattr(dm_module, "SMS_PORT", "")
    monkeypatch.setattr(dm_module, "discover_port", fake_discover)
    manager = DeviceManager(_noop_callback, port="")
    with pytest.raises(RuntimeError):
        await manager.resolve_port()


async def test_drain_runs_before_the_delete_command():
    """AT+CMGD=1,4 wipes the modem's store. Messages that arrived while the
    process was down must be read out before that happens."""
    commands = DeviceManager.SETUP_COMMANDS
    marker = DeviceManager.DRAIN_MARKER
    assert marker in commands
    assert "AT+CMGD=1,4" in commands
    assert commands.index("AT+CMGF=0") < commands.index(marker)
    assert commands.index('AT+CPMS="ME","ME","ME"') < commands.index(marker)
    assert commands.index(marker) < commands.index("AT+CMGD=1,4")


async def test_setup_no_longer_sets_the_modem_clock():
    """Nothing reads the modem RTC, and a wrong boot clock would be written
    straight into it. Network time updates handle this instead."""
    assert not any(cmd.startswith("AT+CCLK") for cmd in DeviceManager.SETUP_COMMANDS)


async def test_drain_parses_cmgl_and_forwards_each_message():
    forwarded = []

    async def collect(sender, timestamp, content):
        forwarded.append((sender, timestamp, content))
        return True

    manager = DeviceManager(collect, port="/hostdev/ttyUSB2")
    manager.reader = FakeReader([
        b"+CMGL: 0,1,,26\r\n",
        f"{STORED_PDU}\r\n".encode(),
        b"+CMGL: 1,1,,26\r\n",
        f"{STORED_PDU}\r\n".encode(),
        b"OK\r\n",
    ])
    manager.writer = FakeWriter()

    assert await manager._drain_stored_sms() == 2
    assert len(forwarded) == 2


async def test_drain_returns_zero_when_store_is_empty():
    manager = _make([b"OK\r\n"])
    assert await manager._drain_stored_sms() == 0


async def test_drain_survives_a_corrupt_pdu():
    manager = _make([b"+CMGL: 0,1,,26\r\n", b"not a valid pdu\r\n", b"OK\r\n"])
    assert await manager._drain_stored_sms() == 0


async def test_drain_survives_a_trailing_header_without_a_pdu():
    manager = _make([b"+CMGL: 0,1,,26\r\n", b"OK\r\n"])
    assert await manager._drain_stored_sms() == 0


async def test_drain_keeps_going_after_an_undecodable_entry():
    """One unreadable entry must not cost us the rest of the store."""
    forwarded = []

    async def collect(sender, timestamp, content):
        forwarded.append(sender)
        return True

    manager = DeviceManager(collect, port="/hostdev/ttyUSB2")
    manager.reader = FakeReader([
        b"+CMGL: 0,1,,26\r\n",
        b"not a valid pdu\r\n",
        b"+CMGL: 1,1,,26\r\n",
        f"{STORED_PDU}\r\n".encode(),
        b"OK\r\n",
    ])
    manager.writer = FakeWriter()

    assert await manager._drain_stored_sms() == 1
    assert len(forwarded) == 1


async def test_drain_forwards_a_stored_message_with_its_carrier_timestamp():
    """The composed property this task exists for: a message recovered from
    storage must arrive stamped with the time the carrier gave it, not the
    moment it happened to be recovered. Covering _forward_pdu and the drain
    separately does not establish this, because the drain's other fixtures are
    SUBMIT PDUs, which carry no timestamp at all.
    """
    captured = []

    async def collect(sender, timestamp, content):
        captured.append(timestamp)
        return True

    manager = DeviceManager(collect, port="/hostdev/ttyUSB2")
    manager.reader = FakeReader([
        b"+CMGL: 0,1,,26\r\n",
        f"{DELIVER_PDU}\r\n".encode(),
        b"OK\r\n",
    ])
    manager.writer = FakeWriter()

    assert await manager._drain_stored_sms() == 1
    assert captured[0].startswith("2024-11-02 13:15:51")
    assert not captured[0].startswith(str(datetime.now().year))


async def test_forward_uses_the_operator_timestamp_not_local_time():
    """The library returns the delivery time under 'time'. Reading 'date'
    silently fell through to the local clock on every single message, which
    also destroyed the point of draining the store on startup."""
    captured = {}

    async def collect(sender, timestamp, content):
        captured["timestamp"] = timestamp
        return True

    manager = DeviceManager(collect, port="/hostdev/ttyUSB2")
    # A DELIVER PDU carrying a service-centre timestamp of 2024-11-02 13:15:51 +08.
    deliver = "0791683108200155040D91683103943254F60008421120315115238A02597D"
    assert await manager._forward_pdu(deliver) is True
    assert captured["timestamp"].startswith("2024-11-02 13:15:51")
    assert not captured["timestamp"].startswith(str(datetime.now().year))


def _recording_manager(callback=_noop_callback, responder=None):
    """A manager whose AT layer is replaced by a recorder.

    setup_sms is driven through this rather than through a fake stream so the
    assertions are about the sequence itself, and so no test can block on a
    reader that never answers.
    """
    manager = DeviceManager(callback, port="/hostdev/ttyUSB2")
    manager.writer = FakeWriter()
    sent = []

    async def fake_send(command, timeout):
        sent.append((command, timeout))
        return responder(command) if responder else [b"OK"]

    manager._send_and_wait = fake_send
    return manager, sent


async def test_setup_drains_the_store_before_erasing_it():
    """The ordering that matters is the one setup_sms actually performs: the
    marker has to trigger a read of the store, not be sent as an AT command."""
    forwarded = []

    async def collect(sender, timestamp, content):
        forwarded.append(sender)
        return True

    def responder(command):
        if command == "AT+CMGL=4":
            return [b"+CMGL: 0,1,,26", STORED_PDU.encode(), b"OK"]
        return [b"OK"]

    manager, sent = _recording_manager(collect, responder)
    await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert DeviceManager.DRAIN_MARKER not in commands
    assert commands.index('AT+CPMS="ME","ME","ME"') < commands.index("AT+CMGL=4")
    assert commands.index("AT+CMGL=4") < commands.index("AT+CMGD=1,4")
    assert len(forwarded) == 1


async def test_setup_gives_slow_commands_the_longer_deadline():
    manager, sent = _recording_manager()
    await manager.setup_sms()

    deadlines = dict(sent)
    assert deadlines["AT&F"] == config.AT_SLOW_COMMAND_TIMEOUT
    assert deadlines["AT&W"] == config.AT_SLOW_COMMAND_TIMEOUT
    assert deadlines["ATE0"] == config.AT_COMMAND_TIMEOUT


async def test_setup_aborts_when_a_required_command_goes_unanswered():
    """_send_and_wait reports a timeout as an empty list. Ignoring that lets a
    modem that has stopped answering pass initialisation, leaving a process
    that believes it is healthy while forwarding nothing."""
    def responder(command):
        return [] if command == "AT+CMGF=0" else [b"OK"]

    manager, sent = _recording_manager(responder=responder)
    with pytest.raises(RuntimeError):
        await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert "AT+CMGD=1,4" not in commands


async def test_setup_does_not_erase_a_store_it_could_not_read():
    """A silent modem is not an empty store. Reading the drain's timeout as
    "nothing there" would hand control straight to the erase, destroying
    unread messages - the loss this whole sequence exists to prevent."""
    def responder(command):
        return [] if command == "AT+CMGL=4" else [b"OK"]

    manager, sent = _recording_manager(responder=responder)
    with pytest.raises(RuntimeError):
        await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert "AT+CMGD=1,4" not in commands


async def test_setup_does_not_erase_a_store_that_refused_to_be_listed():
    """An error is an answer, but not the answer that permits an erase. A
    modem whose store is still busy after a reset replies +CMS ERROR: 14 to
    AT+CMGL=4, which says the listing failed - not that there is nothing to
    list. Testing only the silent case misses this, because an error reply
    comes back as a non-empty response.
    """
    def responder(command):
        return [b"+CMS ERROR: 14"] if command == "AT+CMGL=4" else [b"OK"]

    manager, sent = _recording_manager(responder=responder)
    with pytest.raises(RuntimeError):
        await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert "AT+CMGD=1,4" not in commands


async def test_setup_aborts_when_full_functionality_is_refused():
    """AT+CFUN=1 is the other command with no degraded mode: a modem that
    refuses it has no radio, so setup completing would leave a process that
    forwards nothing while looking healthy."""
    def responder(command):
        return [b"ERROR"] if command == "AT+CFUN=1" else [b"OK"]

    manager, sent = _recording_manager(responder=responder)
    with pytest.raises(RuntimeError):
        await manager.setup_sms()


async def test_setup_continues_past_an_unsupported_optional_command():
    """Vendor specific commands are answered with ERROR by other modules, and
    an optional command may not answer at all. Neither is a reason to give up
    on a modem that is otherwise working."""
    def responder(command):
        if command.startswith("AT+Q"):
            return [b"ERROR"]
        if command == "AT+CSDH=1":
            return []
        return [b"OK"]

    manager, sent = _recording_manager(responder=responder)
    await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert commands[-1] == "AT&W"
