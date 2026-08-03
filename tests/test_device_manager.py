import asyncio
import logging
import time
from datetime import datetime, timedelta

import pytest
from gsmmodem.pdu import encodeSmsSubmitPdu

import config
from module.device_manager import DeviceManager
from module.health import HealthState

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

    result = await manager._drain_stored_sms()
    assert (result.entries, result.forwarded) == (2, 2)
    assert result.complete is True
    assert len(forwarded) == 2


async def test_drain_returns_zero_when_store_is_empty():
    manager = _make([b"OK\r\n"])
    result = await manager._drain_stored_sms()
    assert (result.entries, result.forwarded) == (0, 0)
    assert result.complete is True


async def test_drain_survives_a_corrupt_pdu():
    manager = _make([b"+CMGL: 0,1,,26\r\n", b"not a valid pdu\r\n", b"OK\r\n"])
    result = await manager._drain_stored_sms()
    assert (result.entries, result.forwarded) == (1, 0)


async def test_drain_survives_a_trailing_header_without_a_pdu():
    """The terminating OK is consumed as the entry's PDU and fails to decode,
    which is the same outcome as any other unreadable entry: nothing is
    forwarded and the drain does not raise."""
    manager = _make([b"+CMGL: 0,1,,26\r\n", b"OK\r\n"])
    result = await manager._drain_stored_sms()
    assert result.forwarded == 0


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

    result = await manager._drain_stored_sms()
    assert (result.entries, result.forwarded) == (2, 1)
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

    assert (await manager._drain_stored_sms()).forwarded == 1
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


async def test_forward_reports_a_delivery_the_callback_refused():
    """Decoding a PDU is not delivering it. The callback returns whether the
    message actually reached its destination, and the store may only be erased
    on the strength of that answer."""
    async def refuse(sender, timestamp, content):
        return False

    manager = DeviceManager(refuse, port="/hostdev/ttyUSB2")
    assert await manager._forward_pdu(DELIVER_PDU) is False


async def test_a_held_concatenated_part_counts_as_progress():
    """A fragment that is still waiting for its siblings has not failed. Only
    the part that completes the message can report a delivery outcome, so
    holding one must not read as an undelivered entry and block the erase."""
    manager = _make()
    held = await manager._handle_concat_sms_part(
        "+8613800138000", datetime(2024, 11, 2, 13, 15, 51), "AAAA", 7, 2, 1
    )
    assert held is True


async def test_a_completed_concatenated_message_reports_its_delivery():
    outcomes = []

    async def refuse(sender, timestamp, content):
        outcomes.append(content)
        return False

    manager = DeviceManager(refuse, port="/hostdev/ttyUSB2")
    when = datetime(2024, 11, 2, 13, 15, 51)
    assert await manager._handle_concat_sms_part("+8613800138000", when, "AAAA", 7, 2, 1) is True
    assert await manager._handle_concat_sms_part("+8613800138000", when, "BBBB", 7, 2, 2) is False
    assert len(outcomes) == 1


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


def _listing_responder(entries):
    """Answer AT+CMGL=4 with `entries` stored messages, and OK to everything else."""
    listing = []
    for index in range(entries):
        listing.append(f"+CMGL: {index},1,,26".encode())
        listing.append(STORED_PDU.encode())
    listing.append(b"OK")

    def responder(command):
        return list(listing) if command == "AT+CMGL=4" else [b"OK"]

    return responder


async def test_setup_does_not_erase_a_store_it_could_not_deliver():
    """Reading the store and delivering it are two different things. The modem
    comes back before the outbound link does, so every forward can fail while
    the listing succeeds; erasing on the strength of a decode destroys exactly
    the messages the drain exists to recover."""
    attempts = []

    async def refuse(sender, timestamp, content):
        attempts.append(timestamp)
        return False

    manager, sent = _recording_manager(refuse, _listing_responder(1))
    await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert len(attempts) == 1
    assert "AT+CMGD=1,4" not in commands
    # The rest of the sequence still runs: an undelivered store is a reason to
    # keep the messages, not to abandon initialisation.
    assert commands[-1] == "AT&W"


async def test_setup_erases_the_store_once_every_message_is_delivered():
    """The other half of the gate. A drain that delivered everything must still
    erase, or the store is replayed on every reconnect forever."""
    delivered = []

    async def accept(sender, timestamp, content):
        delivered.append(timestamp)
        return True

    manager, sent = _recording_manager(accept, _listing_responder(2))
    await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert len(delivered) == 2
    assert "AT+CMGD=1,4" in commands


async def test_setup_does_not_erase_when_only_some_messages_were_delivered():
    """A partial success is still a loss: erasing would destroy the entries
    that did not get through while keeping none of them recoverable."""
    outcomes = iter([True, False])
    attempted = []

    async def flaky(sender, timestamp, content):
        result = next(outcomes)
        attempted.append(result)
        return result

    manager, sent = _recording_manager(flaky, _listing_responder(2))
    await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert attempted == [True, False]
    assert "AT+CMGD=1,4" not in commands


async def test_a_skipped_erase_says_why_the_messages_will_arrive_again():
    """An operator seeing every stored message a second time has to be able to
    find out from the log that the store was deliberately left intact."""
    from module import device_manager as dm_module

    async def refuse(sender, timestamp, content):
        return False

    manager, _sent = _recording_manager(refuse, _listing_responder(1))
    with _LogCapture(dm_module) as captured:
        await manager.setup_sms()

    skipped = [
        record for record in captured.records
        if record.levelno >= logging.WARNING and "store" in record.getMessage()
    ]
    assert skipped, "the skipped erase must be reported at warning or above"
    assert any("again" in record.getMessage() for record in skipped)


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


async def _no_delay(_delay):
    """Stand in for asyncio.sleep: yields to the loop without spending time."""
    await asyncio.sleep(0)


class _LogCapture:
    """Collect this module's log records without printing them."""

    def __init__(self, dm_module):
        self._logger = dm_module.logger
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


async def test_device_manager_is_a_managed_service():
    manager = _make()
    assert manager.name == "device"
    for method in ("connect_once", "run", "teardown"):
        assert callable(getattr(manager, method))


async def test_probe_settings_come_from_config():
    manager = _make()
    assert manager.probe_interval == config.MODEM_PROBE_INTERVAL
    assert manager.probe_timeout == config.MODEM_PROBE_TIMEOUT
    assert manager.probe_failures == config.MODEM_PROBE_FAILURES


async def test_the_probe_interval_cannot_outrun_the_health_staleness_window(monkeypatch):
    """The probe is the only thing that refreshes the health snapshot, so an
    interval longer than the staleness window would fail the container
    healthcheck on a process that is working perfectly."""
    import importlib

    original = config.MODEM_PROBE_INTERVAL
    monkeypatch.setenv("MODEM_PROBE_INTERVAL", "600")
    try:
        importlib.reload(config)
        assert config.MODEM_PROBE_INTERVAL <= config.HEALTH_STALE_SECONDS / 2

        monkeypatch.setenv("MODEM_PROBE_INTERVAL", "0")
        importlib.reload(config)
        assert config.MODEM_PROBE_INTERVAL >= 1.0
    finally:
        monkeypatch.undo()
        importlib.reload(config)

    assert config.MODEM_PROBE_INTERVAL == original


async def test_csq_updates_health_and_signal_strength():
    health = HealthState(["device", "telegram"])
    manager = DeviceManager(_noop_callback, health=health, port="/hostdev/ttyUSB2")
    await manager.process_message(b"+CSQ: 21,99\r\n")
    assert health.snapshot()["rssi"] == 21
    assert manager._probe_event.is_set() is True


async def test_malformed_csq_does_not_raise():
    health = HealthState(["device", "telegram"])
    manager = DeviceManager(_noop_callback, health=health, port="/hostdev/ttyUSB2")
    await manager.process_message(b"+CSQ: garbage\r\n")
    assert health.snapshot()["rssi"] is None


async def test_a_csq_reply_still_counts_as_an_answer_when_it_is_malformed():
    """The probe asks whether the modem answers, not what it answered."""
    manager = _make()
    await manager.process_message(b"+CSQ: garbage\r\n")
    assert manager._probe_event.is_set() is True


async def test_a_csq_reply_is_not_mistaken_for_pdu_data():
    """A heartbeat reply can land between a +CMT header and its PDU. Appending
    it to the pending PDU would corrupt the message and lose the heartbeat."""
    manager = _make()
    await manager.process_message(b"+CMT: ,26\r\n")
    await manager.process_message(b"+CSQ: 21,99\r\n")
    assert manager._probe_event.is_set() is True
    assert manager.pending_sms["pdu"] == b""


async def test_a_send_confirmation_is_not_mistaken_for_pdu_data():
    """The same collision as the heartbeat reply, from the other direction: a
    +CMGS for a message the user just sent can arrive between a +CMT header and
    its PDU. Appending it to the pending PDU corrupts the inbound message and
    swallows the confirmation, so the send path reports a failure for a message
    the modem accepted."""
    manager = _make()
    await manager.process_message(b"+CMT: ,26\r\n")
    await manager.process_message(b"+CMGS: 42\r\n")
    assert manager.sms_sent_event.is_set() is True
    assert manager.pending_sms["pdu"] == b""


async def test_heartbeat_raises_after_consecutive_failures():
    manager = _make([])
    manager.probe_interval = 0.01
    manager.probe_timeout = 0.01
    manager.probe_failures = 2
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(manager.heartbeat_loop(), timeout=2.0)


async def test_an_answered_heartbeat_resets_the_failure_count():
    """Three misses spread over an hour must not look like three in a row."""
    manager = _make()
    manager.probe_timeout = 0.01
    manager.probe_failures = 2
    manager._sleep = _no_delay
    answers = iter([False, True, False, False])
    probes = []

    async def fake_probe(command):
        probes.append(command)
        if next(answers):
            manager._probe_event.set()

    manager.send_at_command_async = fake_probe

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(manager.heartbeat_loop(), timeout=2.0)

    # Without the reset the second miss would be the second in a row and the
    # loop would give up one probe earlier.
    assert probes == ["AT+CSQ"] * 4


async def test_a_single_missed_heartbeat_keeps_the_session():
    """One unanswered probe is normal on a busy modem. Tearing the connection
    down for it would reconnect the service every few minutes for no reason."""
    manager = _make()
    manager.probe_timeout = 0.01
    manager.probe_failures = 3
    manager._sleep = _no_delay
    probes = []
    settled = asyncio.Event()

    async def fake_probe(command):
        probes.append(command)
        if len(probes) == 1:
            return  # the first probe goes unanswered
        manager._probe_event.set()
        if len(probes) >= 6:
            settled.set()

    manager.send_at_command_async = fake_probe

    task = asyncio.create_task(manager.heartbeat_loop())
    try:
        await asyncio.wait_for(settled.wait(), timeout=2.0)
        assert task.done() is False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_run_parks_while_the_modem_keeps_answering():
    """The supervisor treats a run body that returns as a failed session, and a
    session shorter than SERVICE_STABLE_SECONDS as flapping. A healthy run must
    therefore never end, while its heartbeat keeps going underneath it."""
    manager = _make([])
    manager._sleep = _no_delay
    probes = []
    settled = asyncio.Event()

    async def fake_probe(command):
        probes.append(command)
        manager._probe_event.set()
        if len(probes) >= 3:
            settled.set()

    manager.send_at_command_async = fake_probe

    task = asyncio.create_task(manager.run())
    try:
        await asyncio.wait_for(settled.wait(), timeout=2.0)
        assert task.done() is False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_read_loop_reports_a_closed_stream_instead_of_spinning():
    """A stream at end of file returns empty immediately and, crucially,
    without awaiting anything. Treating that as "nothing arrived" leaves the
    loop with no suspension point, so it never gives the event loop back: the
    heartbeat stops, the supervisor never sees this body finish, and the
    shutdown signal is never observed. It must be reported as a lost device.

    Note for anyone changing the guard: without it this test does not fail, it
    hangs, and no timeout written inside the test can rescue it -- a timeout
    callback needs the event loop, which is precisely what is being starved.
    """
    class CountingClosedReader(ClosedReader):
        def __init__(self):
            self.calls = 0

        async def readline(self):
            self.calls += 1
            return await super().readline()

    manager = _make()
    manager.reader = CountingClosedReader()
    manager._sleep = _no_delay

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(manager.read_loop(), timeout=2.0)

    # Reported on the first empty read, not retried: every retry would return
    # instantly, and that is what produces the spin.
    assert manager.reader.calls == 1


async def test_read_loop_still_retries_a_transient_read_error():
    """End of file is fatal, but an ordinary read error is not: the retry path
    that handles an unplug raising through pyserial has to survive the fix."""
    class FlakyReader:
        def __init__(self):
            self.calls = 0
            self.lines = [b"OK\r\n"]

        async def readline(self):
            self.calls += 1
            if self.calls <= 2:
                raise OSError("transient")
            if self.lines:
                return self.lines.pop(0)
            # Park rather than repeat: a fixture that returns instantly on
            # every call would starve the loop exactly as the defect does.
            await asyncio.sleep(3600)

    manager = _make()
    manager.reader = FlakyReader()
    manager._sleep = _no_delay

    task = asyncio.create_task(manager.read_loop())
    try:
        line = await asyncio.wait_for(manager.message_queue.get(), timeout=2.0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert line == b"OK\r\n"
    # Two failures survived rather than ending the session.
    assert manager.reader.calls >= 3


async def test_teardown_clears_per_session_parse_state():
    """A disconnect can land between a +CMT header and the PDU it announced.
    Carrying that into the next session makes the first line after reconnect
    get swallowed as PDU data, and that line's message is lost."""
    manager = _make()
    await manager.process_message(b"+CMT: ,26\r\n")
    await manager.message_queue.put(b"+CREG: 1\r\n")
    assert manager.pending_sms["pdu"] is not None

    await manager.teardown()

    assert manager.pending_sms["pdu"] is None
    assert manager.pending_sms["expected_length"] is None
    assert manager.message_queue.empty() is True

    # The next session routes normally rather than absorbing its first line.
    await manager.process_message(b'+CREG: 1,"2AF3","01A2B3C4",7\r\n')
    assert manager.pending_sms["pdu"] is None


async def test_the_send_path_holds_the_port_across_the_prompt_window():
    """Everything written between AT+CMGS and the terminating Ctrl+Z is taken
    by the modem as message data, so nothing else may write during it."""
    manager = _make()
    manager._sleep = _no_delay
    in_window = asyncio.Event()

    async def record(command):
        if command.startswith("AT+CMGS="):
            in_window.set()

    manager.send_at_command_async = record

    send = asyncio.create_task(manager.handle_send_sms("+8613800138000", "x"))
    try:
        await asyncio.wait_for(in_window.wait(), timeout=2.0)
        assert manager._at_lock.locked() is True
    finally:
        manager.sms_sent_event.set()
        await asyncio.wait_for(send, timeout=2.0)
    assert manager._at_lock.locked() is False


async def test_the_heartbeat_waits_for_the_port_to_be_free():
    """The probe is the second writer this component gained. It must queue
    behind a message being sent rather than writing into the middle of it."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99

    await manager._at_lock.acquire()
    task = asyncio.create_task(manager.heartbeat_loop())
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert manager.writer.written == []
        assert task.done() is False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        manager._at_lock.release()


async def test_concat_buffer_uses_a_monotonic_clock():
    """Storing a wall-clock datetime would let a large NTP correction expire
    every in-flight part at once."""
    from module.device_manager import ConcatSmsBuffer

    before = time.monotonic()
    buffer = ConcatSmsBuffer(sender="+8613800138000", ref_num=1, max_parts=2,
                             timestamp=None)
    after = time.monotonic()

    assert isinstance(buffer.first_received, float)
    assert before <= buffer.first_received <= after
    assert buffer.is_expired(timeout_seconds=60) is False


async def test_concat_buffer_expires_on_elapsed_time():
    from module.device_manager import ConcatSmsBuffer

    buffer = ConcatSmsBuffer(sender="+8613800138000", ref_num=1, max_parts=2,
                             timestamp=None)
    assert buffer.is_expired(timeout_seconds=60) is False
    buffer.first_received -= 61
    assert buffer.is_expired(timeout_seconds=60) is True


async def test_concat_buffer_ignores_a_wall_clock_jump(monkeypatch):
    """The board has no battery-backed RTC, so it boots years in the past and
    leaps forward the moment the clock is synchronised. A wall-clock expiry
    would discard every half-assembled message at that instant."""
    from module import device_manager as dm_module

    buffer = dm_module.ConcatSmsBuffer(sender="+8613800138000", ref_num=1,
                                       max_parts=2, timestamp=None)

    jump = timedelta(days=5 * 365)
    real_time = time

    class JumpedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + jump

    class JumpedTime:
        """Wall clock leaps forward; the monotonic clock cannot."""
        time = staticmethod(lambda: real_time.time() + jump.total_seconds())
        monotonic = staticmethod(real_time.monotonic)

    monkeypatch.setattr(dm_module, "datetime", JumpedDateTime)
    monkeypatch.setattr(dm_module, "time", JumpedTime)

    assert buffer.is_expired(timeout_seconds=60) is False


async def test_teardown_closes_the_serial_transport():
    """_probe_modem can raise with the port already open. Leaving it held means
    the next attempt cannot open it."""
    manager = _make([b"OK\r\n"])
    writer = manager.writer
    await manager.teardown()
    assert writer.closed is True
    assert manager.writer is None
    assert manager.reader is None


async def test_teardown_is_idempotent():
    manager = _make([b"OK\r\n"])
    await manager.teardown()
    await manager.teardown()
    assert manager.writer is None


async def test_teardown_without_a_connection_does_nothing():
    """teardown() runs after every failed attempt, including ones where
    connect_once() never got as far as opening the port."""
    manager = DeviceManager(_noop_callback, port="/hostdev/ttyUSB2")
    await manager.teardown()
    assert manager.writer is None


async def test_teardown_does_not_raise_when_closing_fails():
    manager = _make()

    def boom():
        raise OSError("device went away")

    manager.writer.close = boom
    await manager.teardown()
    assert manager.writer is None


async def test_a_failed_handshake_still_releases_the_port(monkeypatch):
    """The specific leak this task inherited: _probe_modem raises after the
    transport is open, so only teardown can give the port back."""
    from module import device_manager as dm_module

    writer = FakeWriter()

    async def fake_open(url, baudrate):
        return FakeReader([]), writer

    monkeypatch.setattr(
        dm_module.serial_asyncio, "open_serial_connection", fake_open
    )
    manager = DeviceManager(_noop_callback, port="/hostdev/ttyUSB2")
    manager._port_exists = lambda path: True
    manager.probe_timeout = 0.05

    with pytest.raises(RuntimeError):
        await manager.connect_once()
    assert writer.closed is False

    await manager.teardown()
    assert writer.closed is True


async def test_a_notify_failure_cannot_break_the_device_path():
    manager = _make()

    async def failing_notify(text):
        raise RuntimeError("channel is down")

    manager.notify = failing_notify
    await manager.teardown()
    assert manager.writer is None


async def test_obsolete_methods_are_gone():
    """Reconnection must have exactly one owner, the supervisor."""
    for name in ("send_at_command", "reconnect", "close"):
        assert not hasattr(DeviceManager, name)


async def test_a_serial_line_is_described_without_its_payload():
    from module.device_manager import _describe_line

    assert _describe_line(DELIVER_PDU.encode()) == f"[{len(DELIVER_PDU)} bytes]"
    assert _describe_line(b"+CMT: ,26").startswith("+CMT ")
    assert _describe_line(b"RING") == "RING"


async def test_only_a_known_urc_name_is_ever_echoed():
    """These describers run on lines the router did not recognise, and a line
    it did not recognise may be a message. Matching the shape of a URC is not
    enough: a body beginning with a number has that shape too."""
    from module.device_manager import _describe_line

    # Reads as a URC keyword to any shape-based test, but is not one.
    numeric = b"+8613800138000 trailing"
    assert _describe_line(numeric) == f"[{len(numeric)} bytes]"

    unlisted = b"+NOTAURC: 12345"
    assert _describe_line(unlisted) == f"[{len(unlisted)} bytes]"

    # A name with no colon after it is not a URC either.
    assert _describe_line(b"+CMT and more") == f"[{len(b'+CMT and more')} bytes]"

    assert _describe_line(b"+CSQ: 21,99").startswith("+CSQ ")


async def test_an_unhandled_serial_line_is_logged_without_its_payload():
    """Unrecognised lines are worth logging on unfamiliar hardware, but one of
    them may itself be a message."""
    from module import device_manager as dm_module

    with _LogCapture(dm_module) as captured:
        manager = _make()
        await manager.process_message(DELIVER_PDU.encode() + b"\r\n")

    assert DELIVER_PDU not in captured.text
    assert DELIVER_PDU[:16] not in captured.text
    assert any(
        record.levelno >= logging.WARNING and "nhandled" in record.getMessage()
        for record in captured.records
    )


async def test_an_unparsable_message_header_is_logged_without_its_payload():
    from module import device_manager as dm_module

    with _LogCapture(dm_module) as captured:
        manager = _make()
        await manager.handle_incoming_sms_header(b'+CMT: "unexpected shape"')

    assert "unexpected shape" not in captured.text
    assert "+CMT" in captured.text


async def test_an_outgoing_pdu_is_not_logged_when_it_is_written():
    """The write path carries commands and, once per message, the encoded PDU.
    A PDU in the log is the message in the log."""
    from module import device_manager as dm_module

    pdu_hex = encodeSmsSubmitPdu("+8613800138000", "TESTMSG")[0].data.hex().upper()

    with _LogCapture(dm_module) as captured:
        manager = _make()
        await manager.send_at_command_async("AT+CMGS=23")
        await manager.send_at_command_async(pdu_hex + chr(26))

    assert "AT+CMGS=23" in captured.text
    assert pdu_hex not in captured.text
    assert pdu_hex[:16] not in captured.text


async def test_no_log_line_carries_a_message_body():
    """The service forwards one-time codes and bank notifications. A body in
    the log is a disclosure, and this file used to log the whole merged text
    of every long message at INFO."""
    from module import device_manager as dm_module

    # Not message content: a marker chosen so the assertion can look for it.
    marker = "QQZZWWXX"
    when = datetime(2024, 11, 2, 13, 15, 51)

    with _LogCapture(dm_module) as captured:
        manager = _make()
        await manager._handle_concat_sms_part(
            "+8613800138000", when, marker[:4], 7, 2, 1
        )
        await manager._handle_concat_sms_part(
            "+8613800138000", when, marker[4:], 7, 2, 2
        )

    assert marker not in captured.text
    assert marker[:4] not in captured.text
    assert marker[4:] not in captured.text
    # The diagnostics that replaced it must still be there.
    assert "ref=7" in captured.text
    assert "2/2 received" in captured.text
    assert f"{len(marker)} character(s)" in captured.text


async def test_an_expired_concat_buffer_is_logged_without_its_payload():
    from module import device_manager as dm_module

    marker = "QQZZWWXX"
    manager = _make()
    await manager._handle_concat_sms_part(
        "+8613800138000", datetime(2024, 11, 2, 13, 15, 51), marker, 7, 2, 1
    )
    buffer = manager.concat_sms_cache[("+8613800138000", 7)]
    buffer.first_received -= DeviceManager.CONCAT_SMS_TIMEOUT + 1

    with _LogCapture(dm_module) as captured:
        await manager._cleanup_expired_concat_cache()

    assert manager.concat_sms_cache == {}
    assert marker not in captured.text
