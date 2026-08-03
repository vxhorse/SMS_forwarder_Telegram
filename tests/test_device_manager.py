import ast
import asyncio
import importlib
import logging
import os
import pathlib
import time
from datetime import datetime, timedelta

import pytest
from gsmmodem.pdu import encodeSmsSubmitPdu

import config
from module.device_manager import DeviceManager, ForwardOutcome
from module.health import HealthState
from tests.ast_helpers import dotted_name

# A reserved test number, not anyone's. Long enough that a masked form and the
# full form are clearly different strings.
NUMBER = "+8613800138000"

# Self-generated PDU with meaningless body text. Never use real message content.
STORED_PDU = encodeSmsSubmitPdu(NUMBER, "TESTMSG")[0].data.hex().upper()

# One long message as the modem stores it: two entries, each carrying a
# concatenation header that says which half it is. Body text is meaningless.
CONCAT_MESSAGE_LENGTH = 200
CONCAT_PDUS = [
    pdu.data.hex().upper()
    for pdu in encodeSmsSubmitPdu(NUMBER, "T" * CONCAT_MESSAGE_LENGTH)
]

# Not a PDU at all. Decoding it fails the same way on every attempt, which is
# what makes an entry permanently unrecoverable rather than merely undelivered.
CORRUPT_PDU = b"not a valid pdu"

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


class FlowControlledWriter(FakeWriter):
    """A port that takes one command's bytes and never lets them go.

    This is what serial flow control does when the modem stops reading. The
    write itself returns, because it only hands the bytes to the transport;
    the drain that waits for that buffer to empty never returns and never
    raises. Nothing is reported anywhere, because reporting it would need the
    same port.

    :param blocked: the command whose flush never completes. Every other
        command is flushed normally, which is what a modem that goes quiet
        part-way through a session actually does.
    :param answer: awaited once a command has been flushed, standing in for
        the modem's reply arriving on the read path.
    """

    def __init__(self, blocked: str = "", answer=None):
        super().__init__()
        self._blocked = f"{blocked}\r\n".encode() if blocked else None
        self._answer = answer

    async def drain(self):
        last = self.written[-1] if self.written else b""
        if last == self._blocked:
            await asyncio.Event().wait()
        if self._answer is not None:
            await self._answer(last)


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
    assert (result.entries, result.delivered, result.undecodable) == (2, 2, 0)
    assert result.complete is True
    assert len(forwarded) == 2


async def test_drain_returns_zero_when_store_is_empty():
    manager = _make([b"OK\r\n"])
    result = await manager._drain_stored_sms()
    assert (result.entries, result.delivered, result.undecodable) == (0, 0, 0)
    assert result.complete is True


async def test_drain_survives_a_corrupt_pdu():
    """An entry nothing can read counts as undecodable, not as undelivered, so
    it never becomes a reason to keep the store."""
    manager = _make([b"+CMGL: 0,1,,26\r\n", CORRUPT_PDU + b"\r\n", b"OK\r\n"])
    result = await manager._drain_stored_sms()
    assert (result.entries, result.delivered, result.undecodable) == (1, 0, 1)
    assert result.deliverable == 0
    assert result.complete is True


async def test_drain_survives_a_trailing_header_without_a_pdu():
    """The terminating OK is consumed as the entry's PDU and fails to decode,
    which is the same outcome as any other unreadable entry: nothing is
    forwarded and the drain does not raise."""
    manager = _make([b"+CMGL: 0,1,,26\r\n", b"OK\r\n"])
    result = await manager._drain_stored_sms()
    assert (result.delivered, result.undecodable) == (0, 1)
    assert result.complete is True


async def test_drain_keeps_going_after_an_undecodable_entry():
    """One unreadable entry must not cost us the rest of the store."""
    forwarded = []

    async def collect(sender, timestamp, content):
        forwarded.append(sender)
        return True

    manager = DeviceManager(collect, port="/hostdev/ttyUSB2")
    manager.reader = FakeReader([
        b"+CMGL: 0,1,,26\r\n",
        CORRUPT_PDU + b"\r\n",
        b"+CMGL: 1,1,,26\r\n",
        f"{STORED_PDU}\r\n".encode(),
        b"OK\r\n",
    ])
    manager.writer = FakeWriter()

    result = await manager._drain_stored_sms()
    assert (result.entries, result.delivered, result.undecodable) == (2, 1, 1)
    assert result.complete is True
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

    assert (await manager._drain_stored_sms()).delivered == 1
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
    assert await manager._forward_pdu(deliver) is ForwardOutcome.DELIVERED
    assert captured["timestamp"].startswith("2024-11-02 13:15:51")
    assert not captured["timestamp"].startswith(str(datetime.now().year))


async def test_forward_reports_a_delivery_the_callback_refused():
    """Decoding a PDU is not delivering it. The callback returns whether the
    message actually reached its destination, and the store may only be erased
    on the strength of that answer."""
    async def refuse(sender, timestamp, content):
        return False

    manager = DeviceManager(refuse, port="/hostdev/ttyUSB2")
    assert await manager._forward_pdu(DELIVER_PDU) is ForwardOutcome.UNDELIVERED


async def test_forward_separates_an_undecodable_pdu_from_an_undelivered_one():
    """The two failures have opposite futures. A message that decoded may be
    delivered on the next attempt, so it is worth keeping the store for. A PDU
    that will not decode fails identically every time, and holding the store
    against it would replay everything beside it forever."""
    manager = _make()
    outcome = await manager._forward_pdu(CORRUPT_PDU.decode())
    assert outcome is ForwardOutcome.UNDECODABLE


async def test_forward_treats_a_raising_callback_as_retryable():
    """The decode and the forward are guarded separately for this case. A
    failure thrown on the way out says nothing about the PDU, which decoded
    perfectly well; reporting it as undecodable would let the store be erased
    while the message was still recoverable."""
    async def explode(sender, timestamp, content):
        raise RuntimeError("downstream unavailable")

    manager = DeviceManager(explode, port="/hostdev/ttyUSB2")
    assert await manager._forward_pdu(DELIVER_PDU) is ForwardOutcome.UNDELIVERED


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


GOOD_PDU = STORED_PDU.encode()


def _listing_responder(*pdus):
    """Answer AT+CMGL=4 with one stored entry per PDU given, OK to the rest."""
    listing = []
    for index, pdu in enumerate(pdus):
        listing.append(f"+CMGL: {index},1,,26".encode())
        listing.append(pdu)
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

    manager, sent = _recording_manager(refuse, _listing_responder(GOOD_PDU))
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

    manager, sent = _recording_manager(accept, _listing_responder(GOOD_PDU, GOOD_PDU))
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

    manager, sent = _recording_manager(flaky, _listing_responder(GOOD_PDU, GOOD_PDU))
    await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert attempted == [True, False]
    assert "AT+CMGD=1,4" not in commands


async def test_setup_erases_a_store_whose_only_failure_cannot_be_decoded():
    """The case a gate counting every entry gets wrong. An entry that will not
    decode will not decode next time either, so holding the store against it
    never ends: every reconnect would re-deliver everything beside it, and the
    storage area would fill until the modem stopped accepting new messages.
    The readable entry was delivered, so the erase proceeds."""
    delivered = []

    async def accept(sender, timestamp, content):
        delivered.append(timestamp)
        return True

    manager, sent = _recording_manager(
        accept, _listing_responder(CORRUPT_PDU, GOOD_PDU)
    )
    await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert len(delivered) == 1
    assert "AT+CMGD=1,4" in commands


async def test_setup_erases_a_store_in_which_nothing_could_be_decoded():
    """Nothing in this store can ever be recovered, so keeping it buys no
    message back and costs the storage area permanently."""
    delivered = []

    async def accept(sender, timestamp, content):
        delivered.append(timestamp)
        return True

    manager, sent = _recording_manager(
        accept, _listing_responder(CORRUPT_PDU, CORRUPT_PDU)
    )
    await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert delivered == []
    assert "AT+CMGD=1,4" in commands


async def test_setup_does_not_erase_when_a_readable_entry_went_undelivered():
    """The other side of the same store: the undecodable entry does not hold
    it, but the one that decoded and never reached anyone does."""
    attempts = []

    async def refuse(sender, timestamp, content):
        attempts.append(timestamp)
        return False

    manager, sent = _recording_manager(
        refuse, _listing_responder(CORRUPT_PDU, GOOD_PDU)
    )
    await manager.setup_sms()

    commands = [command for command, _ in sent]
    assert len(attempts) == 1
    assert "AT+CMGD=1,4" not in commands


async def test_a_long_message_survives_a_drain_slower_than_the_part_timeout():
    """The parts of one long message are separate entries in the listing, so a
    drain slow enough to cross the part timeout between them would discard the
    half already read. The second half would then open a fresh buffer and
    report itself as a fragment still waiting - progress, by the drain's
    reckoning - so every entry would look accounted for and the store holding
    the only copy would be erased with nothing ever delivered.

    A negative timeout stands in for that delay: it makes every buffer expired
    the moment it is created, which is the same condition without spending the
    time to reach it.
    """
    received = []

    async def collect(sender, timestamp, content):
        received.append(content)
        return True

    manager = DeviceManager(collect, port="/hostdev/ttyUSB2")
    manager.CONCAT_SMS_TIMEOUT = -1
    manager.reader = FakeReader([
        b"+CMGL: 0,1,,26\r\n",
        f"{CONCAT_PDUS[0]}\r\n".encode(),
        b"+CMGL: 1,1,,26\r\n",
        f"{CONCAT_PDUS[1]}\r\n".encode(),
        b"OK\r\n",
    ])
    manager.writer = FakeWriter()

    result = await manager._drain_stored_sms()

    # Delivered once, whole.
    assert len(received) == 1
    assert len(received[0]) == CONCAT_MESSAGE_LENGTH
    assert (result.entries, result.delivered, result.undecodable) == (2, 2, 0)
    assert result.complete is True
    # Nothing left holding a partial copy.
    assert manager.concat_sms_cache == {}


async def test_part_expiry_resumes_once_the_drain_is_over():
    """The suspension is scoped to the pass, not switched off. Parts arriving
    live still have to be given up on, or a half message waits for ever."""
    manager = _make()
    assert manager._draining is False

    manager.reader = FakeReader([b"OK\r\n"])
    await manager._drain_stored_sms()
    assert manager._draining is False

    await manager._handle_concat_sms_part(
        NUMBER, datetime(2024, 11, 2, 13, 15, 51), "AAAA", 7, 2, 1
    )
    manager.concat_sms_cache[(NUMBER, 7)].first_received -= (
        DeviceManager.CONCAT_SMS_TIMEOUT + 1
    )
    await manager._cleanup_expired_concat_cache()
    assert manager.concat_sms_cache == {}


async def test_the_drain_flag_is_cleared_even_when_a_forward_raises():
    """A pass that ends badly must not leave expiry suspended for the life of
    the process."""
    async def explode(sender, timestamp, content):
        raise RuntimeError("downstream unavailable")

    manager = DeviceManager(explode, port="/hostdev/ttyUSB2")
    manager.reader = FakeReader([
        b"+CMGL: 0,1,,26\r\n",
        f"{STORED_PDU}\r\n".encode(),
        b"OK\r\n",
    ])
    manager.writer = FakeWriter()

    await manager._drain_stored_sms()
    assert manager._draining is False


async def test_the_two_drain_failures_are_reported_apart():
    """An operator seeing repeats has to be able to tell which happened. A link
    that was down explains the repeats and stops on its own; a corrupt entry
    never will, and is the one that may need clearing by hand."""
    from module import device_manager as dm_module

    async def refuse(sender, timestamp, content):
        return False

    manager = DeviceManager(refuse, port="/hostdev/ttyUSB2")
    manager.reader = FakeReader([
        b"+CMGL: 0,1,,26\r\n",
        CORRUPT_PDU + b"\r\n",
        b"+CMGL: 1,1,,26\r\n",
        f"{STORED_PDU}\r\n".encode(),
        b"OK\r\n",
    ])
    manager.writer = FakeWriter()

    with _LogCapture(dm_module) as captured:
        result = await manager._drain_stored_sms()

    assert (result.entries, result.delivered, result.undecodable) == (2, 0, 1)
    errors = [
        record.getMessage() for record in captured.records
        if record.levelno >= logging.ERROR
    ]
    # Two separate records, each naming its own cause and its own count. One
    # record covering both would leave the operator unable to tell which of the
    # two happened, which is the whole point of reporting them apart.
    decode_errors = [message for message in errors if "decoded" in message]
    delivery_errors = [message for message in errors if "not delivered" in message]
    assert len(decode_errors) == 1, errors
    assert len(delivery_errors) == 1, errors
    assert decode_errors[0] != delivery_errors[0], errors
    # One of the two listed entries could not be decoded; of the one that
    # remained readable, one was not delivered.
    assert "1 of 2" in decode_errors[0], decode_errors
    assert "1 of 1" in delivery_errors[0], delivery_errors
    # Counts only: neither the raw entry nor any decoded fragment of it.
    assert CORRUPT_PDU.decode() not in captured.text
    assert STORED_PDU not in captured.text
    assert STORED_PDU[:16] not in captured.text


async def test_a_skipped_erase_says_why_the_messages_will_arrive_again():
    """An operator seeing every stored message a second time has to be able to
    find out from the log that the store was deliberately left intact."""
    from module import device_manager as dm_module

    async def refuse(sender, timestamp, content):
        return False

    manager, _sent = _recording_manager(refuse, _listing_responder(GOOD_PDU))
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

    @property
    def formatted(self):
        """Everything a real handler would print, including any traceback.

        getMessage() renders the interpolated message and nothing else, so a
        record carrying exc_info can keep an exception's text out of `text`
        while a real handler prints it in full - a formatted traceback ends
        with the exception's own str(). Anything asserting that a body cannot
        reach the log has to look here as well.
        """
        formatter = logging.Formatter("%(levelname)s %(message)s")
        return " ".join(formatter.format(record) for record in self.records)


async def test_device_manager_is_a_managed_service():
    manager = _make()
    assert manager.name == "device"
    for method in ("connect_once", "run", "teardown"):
        assert callable(getattr(manager, method))


async def test_probe_settings_come_from_config(monkeypatch):
    """Each setting is given a value that is not its default, so a field wired
    to a literal that happens to equal the default cannot pass this."""
    monkeypatch.setattr(config, "MODEM_PROBE_INTERVAL", 11.0)
    monkeypatch.setattr(config, "MODEM_PROBE_TIMEOUT", 13.0)
    monkeypatch.setattr(config, "MODEM_PROBE_FAILURES", 7)
    monkeypatch.setattr(config, "MODEM_REGISTRATION_FAILURES", 9)
    monkeypatch.setattr(config, "MODEM_REGISTRATION_CHECK", False)

    manager = _make()
    assert manager.probe_interval == 11.0
    assert manager.probe_timeout == 13.0
    assert manager.probe_failures == 7
    assert manager.registration_failures == 9
    assert manager.registration_check is False


async def test_one_reading_can_never_be_enough_to_drop_the_connection(monkeypatch):
    """Registration dips for a moment during a handover, so a threshold of one
    would restart a working session for an event that fixes itself, and zero
    would switch the check off while still looking like a setting."""
    import importlib

    original = config.MODEM_REGISTRATION_FAILURES
    try:
        for value in ("0", "1"):
            monkeypatch.setenv("MODEM_REGISTRATION_FAILURES", value)
            assert importlib.reload(config).MODEM_REGISTRATION_FAILURES >= 2
    finally:
        monkeypatch.undo()
        importlib.reload(config)

    assert config.MODEM_REGISTRATION_FAILURES == original


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


async def test_only_a_hexadecimal_line_can_be_pdu_data():
    """What separates PDU data from a URC that interrupted it, tested directly.
    Hoisting one branch at a time only protects the URCs someone thought of."""
    from module.device_manager import _is_pdu_line

    assert _is_pdu_line(DELIVER_PDU.encode()) is True
    # The standard fixes the encoding, not the case.
    assert _is_pdu_line(DELIVER_PDU.lower().encode()) is True
    assert _is_pdu_line(b"") is False
    assert _is_pdu_line(b'+CREG: 2,"2AF3","0123ABC",7') is False
    assert _is_pdu_line(b"+CSQ: 21,99") is False
    assert _is_pdu_line(b"+QIND: SMS DONE") is False
    assert _is_pdu_line(b"RING") is False


async def test_a_registration_urc_is_not_mistaken_for_pdu_data():
    """+CREG sat behind the pending-PDU check. AT+CREG=2 is part of setup, so
    registration reports arrive precisely while the radio is reattaching, which
    is the same moment a store of waiting messages is being drained. Appended
    to a pending PDU one of them destroys the message that was arriving, and is
    itself never routed."""
    received = []

    async def collect(sender, timestamp, content):
        received.append(timestamp)
        return True

    manager = DeviceManager(collect, port="/hostdev/ttyUSB2")
    manager.reader = FakeReader()
    manager.writer = FakeWriter()

    await manager.process_message(b"+CMT: ,26\r\n")
    await manager.process_message(b'+CREG: 2,"2AF3","0123ABC",7\r\n')
    # The PDU is still waiting for its own line, unaltered.
    assert manager.pending_sms["pdu"] == b""

    await manager.process_message(f"{DELIVER_PDU}\r\n".encode())
    assert len(received) == 1
    assert manager.pending_sms["pdu"] is None


async def test_every_registration_shape_the_manual_gives_is_read_correctly():
    """The two shapes differ by one leading field, and telling them apart is
    the whole of this parse.

    The vendor manual (AT+CREG) gives the answer to AT+CREG? as
    `+CREG: <n>,<stat>[,<lac>,<ci>[,<AcT>]]` and the modem's own report as
    `+CREG: <stat>[,<lac>,<ci>[,<AcT>]]`. Both are routed through here,
    because AT+CREG=2 is part of setup and the heartbeat now asks the question
    as well. Reading the second field of a report yields the location area
    instead of the registration state, which would count a registered modem as
    absent from the network and restart a session that was working.

    Two further facts from the same section separate them without guessing:
    <n> is one of 0, 1 and 2, and location information accompanies a state
    only when the modem is registered, so a report that is not registered is a
    single field and a report that leads with 1 and carries location cannot be
    a reply.
    """
    shapes = {
        # Replies to AT+CREG?, which always lead with <n>.
        b"+CREG: 2,0": 0,
        b"+CREG: 2,2": 2,
        b"+CREG: 0,3": 3,
        b"+CREG: 2,6": 6,
        b'+CREG: 2,1,"D509","80D413D",7': 1,
        b'+CREG: 2,5,"D509","80D413D",7': 5,
        b'+CREG: 2,7,"D509","80D413D",7': 7,
        # The modem's own reports, which lead with <stat>.
        b"+CREG: 0": 0,
        b"+CREG: 1": 1,
        b"+CREG: 3": 3,
        b"+CREG: 4": 4,
        b'+CREG: 1,"D509","80D413D",7': 1,
        b'+CREG: 5,"2AF3","0123ABC",7': 5,
        b'+CREG: 6,"2AF3","0123ABC",7': 6,
    }

    for line, expected in shapes.items():
        health = HealthState(["device", "telegram"])
        manager = DeviceManager(_noop_callback, health=health, port="/hostdev/ttyUSB2")
        await manager.process_message(line + b"\r\n")
        assert health.snapshot()["registration"] == expected, line


async def test_an_unreadable_registration_line_is_not_taken_for_an_answer():
    """The opposite conclusion to the liveness probe's, on purpose. A +CSQ
    that will not parse still proves the modem answered, which is all that
    probe asks. This probe asks what the modem said, so a line it cannot read
    tells it nothing and must not end the wait: treating it as an answer would
    leave the loop reading whatever the previous round happened to record."""
    health = HealthState(["device", "telegram"])
    manager = DeviceManager(_noop_callback, health=health, port="/hostdev/ttyUSB2")
    await manager.process_message(b"+CREG: garbage\r\n")
    assert health.snapshot()["registration"] is None
    assert manager._registration_event.is_set() is False


async def test_an_unhandled_urc_is_not_mistaken_for_pdu_data():
    """The guard has to hold for lines this code has never seen, which is why
    it tests what a PDU is made of instead of naming the intruders."""
    manager = _make()

    await manager.process_message(b"+CMT: ,26\r\n")
    await manager.process_message(b"+QIND: SMS DONE\r\n")
    assert manager.pending_sms["pdu"] == b""

    await manager.process_message(b"RING\r\n")
    assert manager.pending_sms["pdu"] == b""


async def test_a_hexadecimal_line_still_reaches_the_pending_pdu():
    """The guard must not cost us the PDU itself. A PDU is hexadecimal and can
    arrive in more than one read, so a partial one has to accumulate."""
    manager = _make()

    await manager.process_message(b"+CMT: ,26\r\n")
    await manager.process_message(DELIVER_PDU[:20].encode() + b"\r\n")
    assert manager.pending_sms["pdu"] == DELIVER_PDU[:20].encode()


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
    # The whole round, both questions, as an operator who enabled the check
    # gets it: the liveness count has to behave the same either way.
    manager.registration_check = True
    answers = iter([False, True, False, False])
    probes = []

    async def fake_probe(command):
        if command == "AT+CREG?":
            # On the network throughout: this test is about the liveness path,
            # so the registration path must not be what ends the loop.
            await manager.process_message(b"+CREG: 2,1")
            return
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
    manager.registration_check = True  # the full round, as an operator who enabled it
    probes = []
    settled = asyncio.Event()

    async def fake_probe(command):
        if command == "AT+CREG?":
            # On the network throughout: only the liveness path is under test.
            await manager.process_message(b"+CREG: 2,1")
            return
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


class _RecordingHealth(HealthState):
    """The real HealthState, with a note of every progress report it is given."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress = []

    def record_progress(self, name):
        self.progress.append(name)
        super().record_progress(name)


async def test_the_heartbeat_reports_progress_even_with_nothing_to_refresh(tmp_path):
    """The stamp has to be written by this loop, and refresh_file() cannot be
    the route.

    The snapshot is written only while every component is up, so it falls
    silent for reasons that have nothing to do with this loop - here, a
    Telegram component that has not connected yet. A stamp that travelled
    through it would go silent with it and the loop would look stopped. It
    would be no better the other way round either: both component loops refresh
    the same file, so one that is still running keeps it fresh on behalf of one
    that has stopped.
    """
    health = _RecordingHealth(
        ["device", "telegram"], health_file=str(tmp_path / "healthy")
    )
    health.mark_up("device")  # Telegram deliberately left down.
    manager = DeviceManager(_noop_callback, health=health, port="/hostdev/ttyUSB2")
    manager.reader = FakeReader()
    manager.writer = FakeWriter()
    manager.probe_timeout = 0.01
    manager._sleep = _no_delay
    # The longer of the two rounds, so the report being asserted is the one a
    # round that asks both questions makes.
    manager.registration_check = True
    rounds = []
    settled = asyncio.Event()

    async def fake_probe(command):
        if command == "AT+CREG?":
            await manager.process_message(b"+CREG: 2,1")
            return
        rounds.append(command)
        manager._probe_event.set()
        if len(rounds) >= 2:
            settled.set()

    manager.send_at_command_async = fake_probe

    task = asyncio.create_task(manager.heartbeat_loop())
    try:
        await asyncio.wait_for(settled.wait(), timeout=2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert health.progress[:2] == ["device", "device"]
    # Not one refresh landed in all that time, which is the point.
    assert not os.path.exists(str(tmp_path / "healthy"))


async def _heartbeat_must_give_up(manager, blocked_on: str) -> str:
    """Run one heartbeat loop until it reports a failure, and never longer.

    The outer deadline is what protects the suite rather than the component:
    without it a regression here does not fail, it hangs, and a hung suite
    says nothing about which test hung it. Reaching that deadline is turned
    into this test's own failure, naming what the loop was still waiting on.

    :param blocked_on: what a regression would still be waiting for, quoted
        back in the failure so the report says which bound was lost.
    :return: the message the loop raised.
    """
    try:
        await asyncio.wait_for(manager.heartbeat_loop(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            f"the heartbeat never reached a deadline of its own: it was still "
            f"blocked on {blocked_on}"
        )
    except RuntimeError as exc:
        return str(exc)
    pytest.fail("the heartbeat loop ended without reporting a failure")


async def _probe_must_finish(manager, command, answered, blocked_on: str):
    """Run one probe with a deadline of the test's own around it.

    The same protection _heartbeat_must_give_up gives the loop, for the tests
    that drive a single probe directly. Without it a regression does not fail
    these either: the probe blocks, and with nothing above it to give up, the
    suite stops here rather than reporting anything.

    :param blocked_on: what a regression would still be waiting for.
    :return: what the probe reported.
    """
    try:
        return await asyncio.wait_for(
            manager._probe_once(command, answered), timeout=2.0
        )
    except asyncio.TimeoutError:
        pytest.fail(
            f"the probe never reached a deadline of its own: it was still "
            f"blocked on {blocked_on}"
        )


async def test_a_blocked_write_is_a_missed_heartbeat_not_a_hang():
    """Serial flow control has no deadline of its own, and no exception. A
    modem that has stopped accepting bytes blocks the write, so a probe that
    times only the reply never reaches the point where it would give up: the
    check built to notice a silent modem is itself what stops running, and
    nothing raises, so nothing downstream ever hears about it."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 1
    manager.writer = FlowControlledWriter(blocked="AT+CSQ")

    message = await _heartbeat_must_give_up(manager, "the write of AT+CSQ")

    assert "heartbeat" in message
    # The command was handed over: what never completed is the flush.
    assert manager.writer.written == [b"AT+CSQ\r\n"]


async def test_a_held_lock_is_a_missed_heartbeat_not_a_hang():
    """The compounding case, and the worse one. A send whose write blocks is
    holding the AT lock across both of its writes, so a probe that waits for
    that lock outside its own deadline is waiting on a transaction that will
    never finish - and the modem never has to go quiet for it to happen."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 1
    await manager._at_lock.acquire()  # a send stuck mid-transaction

    try:
        message = await _heartbeat_must_give_up(manager, "the AT lock")
    finally:
        manager._at_lock.release()

    assert "heartbeat" in message
    # Nothing was written: the probe gave up before it ever took the port,
    # which is what keeps it out of the middle of somebody else's transaction.
    assert manager.writer.written == []


async def test_a_blocked_write_on_the_registration_probe_is_a_miss_not_a_hang():
    """The second question the heartbeat asks needs the same bound as the
    first. It is written the same way and under the same lock, so a modem
    that stops accepting bytes between the two probes blocks this one
    instead - and a bound on the first alone would leave the loop hanging one
    step further along."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99  # the liveness path must not be what raises
    manager.registration_failures = 2
    manager.registration_check = True  # the question this test is about

    async def answer(command):
        if command == b"AT+CSQ\r\n":
            manager._probe_event.set()

    manager.writer = FlowControlledWriter(blocked="AT+CREG?", answer=answer)

    message = await _heartbeat_must_give_up(manager, "the write of AT+CREG?")

    assert "registered" in message


async def test_a_probe_cancelled_while_writing_leaves_the_lock_free():
    """The deadline is only half of it. A probe that gave up while still
    holding the port would hand the next probe, and every send after it, a
    lock nothing will ever release: a bounded probe that deadlocks the whole
    component instead of merely hanging itself."""
    manager = _make()
    manager.probe_timeout = 0.01
    manager.writer = FlowControlledWriter(blocked="AT+CSQ")

    gave_up = await _probe_must_finish(
        manager, "AT+CSQ", manager._probe_event, "the write of AT+CSQ"
    )
    assert gave_up is False
    assert manager._at_lock.locked() is False

    # Not merely unlocked but usable: the next probe takes the port and is
    # answered through it.
    async def answer(command):
        manager._probe_event.set()

    manager.writer = FlowControlledWriter(answer=answer)
    answered = await _probe_must_finish(
        manager, "AT+CSQ", manager._probe_event, "the lock the last probe held"
    )
    assert answered is True


async def test_a_probe_cancelled_while_waiting_for_the_lock_leaves_it_to_its_holder():
    """Giving up on the lock must take nothing away from whoever holds it. A
    probe that released a lock it never acquired would drop the next writer
    into the middle of a send, where every byte written becomes part of the
    outgoing message."""
    manager = _make()
    manager.probe_timeout = 0.01
    await manager._at_lock.acquire()

    gave_up = await _probe_must_finish(
        manager, "AT+CSQ", manager._probe_event, "the AT lock"
    )
    assert gave_up is False
    assert manager._at_lock.locked() is True
    assert manager.writer.written == []

    manager._at_lock.release()
    assert manager._at_lock.locked() is False


async def test_an_answered_probe_still_takes_the_port_and_gives_it_back():
    """The bound must cost the ordinary case nothing. The command still goes
    out under the lock, the reply still ends the wait, and the port is handed
    back as soon as the write is done rather than kept for the whole deadline:
    a probe holding it until its reply arrives would put every send behind
    every probe, and a send that lost that race would wait out a deadline of
    its own for a message the modem never saw."""
    manager = _make()
    manager.probe_timeout = 5.0
    held_while_writing = []
    held_while_waiting = []

    async def reply():
        # Runs once the probe is waiting for its answer, which is where the
        # port has to be free again.
        held_while_waiting.append(manager._at_lock.locked())
        await manager.process_message(b"+CSQ: 21,99\r\n")

    async def answer(command):
        held_while_writing.append(manager._at_lock.locked())
        asyncio.create_task(reply())

    manager.writer = FlowControlledWriter(answer=answer)

    answered = await _probe_must_finish(
        manager, "AT+CSQ", manager._probe_event, "a reply that had already arrived"
    )
    assert answered is True
    assert manager.writer.written == [b"AT+CSQ\r\n"]
    assert held_while_writing == [True]
    assert held_while_waiting == [False]
    assert manager._at_lock.locked() is False


# How long a wait that is meant never to finish waits for. Nothing reaches it:
# whatever is parked on it is cancelled by the test that parked it. It exists
# so a test ends on a condition the test controls rather than on a deadline
# elapsing, which no assertion could then be sure of having beaten.
_PARKED = 3600


def _answering_modem(manager, replies):
    """Answer the heartbeat's probes, one registration reading at a time.

    The registration check is switched on here, because it ships off and every
    test using this helper is a test of what the check does once an operator
    has turned it on. Turning the default off must not quietly delete that
    coverage: with this line removed, every test below would stop asking the
    question and the assertions counting AT+CREG? would fail rather than pass
    vacuously.

    Every AT+CSQ is answered, so the liveness path can never be what decides
    the outcome of a test about registration. Each AT+CREG? is answered with
    the next entry of `replies`, fed through the real routing path so it is
    parsed exactly as a modem's line would be; an entry of None is a probe the
    modem does not answer at all.

    Once the entries run out the returned event is set and the probe parks
    rather than answering, so a test that has seen every round it scripted
    ends the loop itself instead of racing a deadline. Parking now needs the
    probe's deadline lifted first, because that deadline covers the whole
    probe: left at the value the test chose to make an unanswered probe give
    up quickly, it would end the parked probe too and count a miss no test
    wrote. The deadline is lifted a step early, at the liveness probe of the
    unscripted round, because _probe_once reads it before this responder is
    called.

    :return: the commands as they are sent, and the event announcing that the
        scripted rounds are over.
    """
    manager.registration_check = True
    remaining = list(replies)
    sent = []
    exhausted = asyncio.Event()

    async def respond(command):
        sent.append(command)
        if command == "AT+CSQ":
            if not remaining:
                manager.probe_timeout = _PARKED
            manager._probe_event.set()
            return
        if not remaining:
            exhausted.set()
            await asyncio.sleep(_PARKED)
            return
        line = remaining.pop(0)
        if line is not None:
            await manager.process_message(line)

    manager.send_at_command_async = respond
    return sent, exhausted


async def test_the_heartbeat_gives_up_on_a_modem_that_is_not_on_the_network():
    """AT+CSQ proves the modem answers; it does not prove a message can reach
    it. A SIM the carrier has detached keeps answering exactly as before while
    nothing arrives, so the liveness probe is answered throughout this test and
    only the registration state can decide it."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99  # the liveness path must not be what raises
    manager.registration_failures = 3
    # Not registered, searching for an operator.
    sent, _ = _answering_modem(manager, [b"+CREG: 2,2"] * 10)

    with pytest.raises(RuntimeError, match="registered"):
        await asyncio.wait_for(manager.heartbeat_loop(), timeout=2.0)

    # Given up on the third consecutive reading: not before, and not later.
    assert sent.count("AT+CREG?") == 3


async def test_a_registered_reply_clears_the_deregistration_count():
    """Registration dips as a modem moves between cells and comes back on its
    own. Only a run of readings means the SIM is no longer attached, so one
    reply saying registered has to put the count back to zero."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    manager.registration_failures = 3
    # Searching, searching, registered, searching, searching: five readings
    # that are never three in a row.
    sent, exhausted = _answering_modem(manager, [
        b"+CREG: 2,2", b"+CREG: 2,2", b"+CREG: 2,1", b"+CREG: 2,2", b"+CREG: 2,2",
    ])

    task = asyncio.create_task(manager.heartbeat_loop())
    try:
        await asyncio.wait_for(exhausted.wait(), timeout=2.0)
        assert task.done() is False
        # The two after the registered reply, not the four before it.
        assert manager.registration_misses == 2
        assert sent.count("AT+CREG?") == 6
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_roaming_counts_as_being_on_the_network():
    """5 is a modem registered on an operator that is not its own, and it
    receives messages exactly as the home network does. Counting it as a
    failure would restart the session every few minutes on a travelling SIM."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    manager.registration_failures = 2
    sent, exhausted = _answering_modem(manager, [b"+CREG: 2,5"] * 3)

    task = asyncio.create_task(manager.heartbeat_loop())
    try:
        await asyncio.wait_for(exhausted.wait(), timeout=2.0)
        assert task.done() is False
        assert manager.registration_misses == 0
        assert sent.count("AT+CREG?") == 4
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_an_unanswered_registration_probe_is_not_read_as_registered():
    """Silence is not an answer, and the reply that did arrive belongs to the
    round it arrived in. A modem that stops answering AT+CREG? while still
    answering AT+CSQ is precisely the failure this probe exists to catch, so
    reading the last known state again would keep the count at zero for ever
    and hide it."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    manager.registration_failures = 2
    sent, _ = _answering_modem(manager, [b"+CREG: 2,1", None, None])

    with pytest.raises(RuntimeError, match="registered"):
        await asyncio.wait_for(manager.heartbeat_loop(), timeout=2.0)

    assert sent.count("AT+CREG?") == 3


async def test_the_deregistration_count_does_not_cross_a_session():
    """This loop runs once per session, and the count belongs to the session.
    Readings taken through a connection that has since been torn down say
    nothing about the one that replaced it: carried over, they would give a
    modem that has just reconnected a single probe to prove itself before the
    new session was dropped too."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    manager.registration_failures = 2
    manager.registration_misses = 1  # left behind by a session that has ended

    sent, exhausted = _answering_modem(manager, [b"+CREG: 2,0", b"+CREG: 2,1"])
    task = asyncio.create_task(manager.heartbeat_loop())
    try:
        await asyncio.wait_for(exhausted.wait(), timeout=2.0)
        # The first reading of this session is its first miss, not its second.
        assert task.done() is False
        assert manager.registration_misses == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_timed_out_probe_clears_the_published_registration_state():
    """The snapshot is where the state is read from outside the process, and
    it is read precisely while the count is climbing. Leaving the last reading
    in it would show a modem on the network for the whole time the loop spends
    giving up on one that has gone silent, which hides the failure the
    snapshot exists to expose."""
    health = HealthState(["device", "telegram"])
    manager = DeviceManager(_noop_callback, health=health, port="/hostdev/ttyUSB2")
    manager.writer = FakeWriter()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    manager.registration_failures = 2
    _answering_modem(manager, [b"+CREG: 2,1", None, None])

    assert health.snapshot()["registration"] is None
    with pytest.raises(RuntimeError, match="registered"):
        await asyncio.wait_for(manager.heartbeat_loop(), timeout=2.0)

    assert health.snapshot()["registration"] is None


async def test_registration_for_messages_only_counts_as_being_on_the_network():
    """6 and 7 are a network that granted the modem an association for
    messages and nothing else, which is the most registered it can be for a
    service that only carries messages. Counting them as failures would cycle
    the radio for ever on a network that attaches this way, in both the shape
    that leads with <n> and the shape that leads with the state."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    manager.registration_failures = 2
    sent, exhausted = _answering_modem(manager, [
        b"+CREG: 2,6",                     # reply: messages only, home
        b'+CREG: 6,"2AF3","0123ABC",7',    # report: the same state
        b"+CREG: 2,7",                     # reply: messages only, roaming
    ])

    task = asyncio.create_task(manager.heartbeat_loop())
    try:
        await asyncio.wait_for(exhausted.wait(), timeout=2.0)
        assert task.done() is False
        assert manager.registration_misses == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_an_emergency_only_attach_does_not_count_as_being_on_the_network():
    """The boundary on the other side of 6 and 7. 8 is a modem attached for
    emergency bearer services alone: it is on a network and no ordinary
    message can reach it, which is exactly the condition this probe exists to
    report."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    manager.registration_failures = 2
    sent, _ = _answering_modem(manager, [b"+CREG: 2,8"] * 5)

    with pytest.raises(RuntimeError, match="registered"):
        await asyncio.wait_for(manager.heartbeat_loop(), timeout=2.0)

    assert sent.count("AT+CREG?") == 2


async def test_the_off_network_warning_names_one_state_and_one_count():
    """The line an operator greps for, and the only place the reason appears.
    A description carrying a comma of its own inside a parenthesised list
    reads as two fields plus a count as a third, so nothing in the line may
    use the separator that divides its parts."""
    from module import device_manager as dm_module

    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    manager.registration_failures = 2
    _answering_modem(manager, [b"+CREG: 2,2"] * 5)

    with _LogCapture(dm_module) as captured:
        with pytest.raises(RuntimeError, match="registered"):
            await asyncio.wait_for(manager.heartbeat_loop(), timeout=2.0)

    warnings = [
        record.getMessage() for record in captured.records
        if record.levelno >= logging.WARNING and "not on the network" in record.getMessage()
    ]
    assert len(warnings) == 2, warnings
    # Both halves are there: why, and for how long.
    assert "searching" in warnings[0], warnings
    assert "1 in a row" in warnings[0], warnings
    assert "2 in a row" in warnings[1], warnings
    # And neither half can be mistaken for the other, or for a third field.
    assert "," not in warnings[0], warnings


async def test_a_stock_start_asks_the_liveness_question_and_nothing_else():
    """What an unconfigured deployment does, asserted through the real config
    default rather than a value the test chose.

    The check ships off, so the heartbeat asks AT+CSQ and stops there. Nothing
    about the modem's registration is asked, no reading is counted, and a
    network that cannot answer the question truthfully cannot end a session
    over it.
    """
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    # Straight from config, untouched: this is the assertion that fails if the
    # default is flipped back without the reasoning that flipped it off.
    assert manager.registration_check is False

    probes = []
    settled = asyncio.Event()

    async def fake_probe(command):
        probes.append(command)
        manager._probe_event.set()
        if len(probes) >= 5:
            settled.set()

    manager.send_at_command_async = fake_probe

    task = asyncio.create_task(manager.heartbeat_loop())
    try:
        await asyncio.wait_for(settled.wait(), timeout=2.0)
        assert task.done() is False
        assert probes == ["AT+CSQ"] * 5
        assert manager.registration_misses == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_the_registration_check_can_be_switched_off():
    """A network that attaches the modem for data alone, with messages
    arriving over a path this question does not describe, has no true answer
    to give it. An operator who enabled the check and then saw that must be
    able to stop asking without waiting for a release, and without the radio
    being reinitialised every few minutes in the meantime."""
    manager = _make()
    manager._sleep = _no_delay
    manager.probe_timeout = 0.01
    manager.probe_failures = 99
    manager.registration_failures = 2
    manager.registration_check = False

    probes = []
    settled = asyncio.Event()

    async def fake_probe(command):
        probes.append(command)
        manager._probe_event.set()
        if len(probes) >= 5:
            settled.set()

    manager.send_at_command_async = fake_probe

    task = asyncio.create_task(manager.heartbeat_loop())
    try:
        await asyncio.wait_for(settled.wait(), timeout=2.0)
        # Five rounds is more than the threshold, and the modem answered
        # nothing about registration in any of them.
        assert task.done() is False
        assert probes == ["AT+CSQ"] * 5
        assert manager.registration_misses == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_the_registration_check_is_switched_off_separately_from_its_threshold(
    monkeypatch,
):
    """A switch and a count are deliberately two settings. Folding the switch
    into the count would let an operator raising the threshold disable the
    guard by accident, which is why the count keeps a floor it cannot be
    tuned below.

    The switch defaults to off, and an unset environment must read the same as
    an explicit no. What the check asks cannot be answered truthfully on every
    network, and where it cannot the misreading ends a session on a timescale
    longer than the one a session takes to count as recovered - so each cycle
    resets what the watchdog measures and the loop registers nowhere. Enabling
    it is a decision to take per SIM, once the state that modem reports is
    known.
    """
    import importlib

    original = config.MODEM_REGISTRATION_CHECK
    try:
        monkeypatch.delenv("MODEM_REGISTRATION_CHECK", raising=False)
        assert importlib.reload(config).MODEM_REGISTRATION_CHECK is False

        for value in ("0", "false", "no", "off"):
            monkeypatch.setenv("MODEM_REGISTRATION_CHECK", value)
            reloaded = importlib.reload(config)
            assert reloaded.MODEM_REGISTRATION_CHECK is False, value
            # Switching the check off does not lower the floor on the count.
            assert reloaded.MODEM_REGISTRATION_FAILURES >= 2

        # And the opt-in still works, from every spelling an operator is
        # likely to reach for: a default of off would be worth nothing if
        # turning it on were unreachable.
        for value in ("1", "true", "yes", "on"):
            monkeypatch.setenv("MODEM_REGISTRATION_CHECK", value)
            assert importlib.reload(config).MODEM_REGISTRATION_CHECK is True, value
    finally:
        monkeypatch.undo()
        importlib.reload(config)

    assert config.MODEM_REGISTRATION_CHECK == original


async def test_run_parks_while_the_modem_keeps_answering():
    """The supervisor treats a run body that returns as a failed session, and a
    session shorter than SERVICE_STABLE_SECONDS as flapping. A healthy run must
    therefore never end, while its heartbeat keeps going underneath it."""
    manager = _make([])
    manager._sleep = _no_delay
    manager.registration_check = True  # both questions, as the longer round
    probes = []
    settled = asyncio.Event()

    async def fake_probe(command):
        if command == "AT+CREG?":
            # Answering both probes is what a healthy modem does; a run body
            # that ends because one of them went unanswered would prove
            # nothing about the case this test is making.
            await manager.process_message(b"+CREG: 2,1")
            return
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
    behind a message being sent rather than writing into the middle of it.

    Queueing is bounded now: a probe that does not reach the port within its
    deadline gives up and asks again next round rather than waiting for ever,
    because a send whose own write has blocked never gives the port back. What
    is unchanged, and is what this asserts, is that nothing is written while
    somebody else holds it.
    """
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


class WedgedPort:
    """The port behind a transport that has stopped accepting bytes.

    Two of its calls matter here and they are opposites. flush() waits for the
    kernel's own output queue to empty and carries no deadline; on a queue that
    is not moving it never returns, and it is called on whichever thread asked
    for it. reset_output_buffer() discards that queue and returns at once.
    """

    def __init__(self, calls):
        # Bytes the file descriptor refused, which is the only reason the
        # transport still holds any - and so the only reason its close could
        # not complete in the first place.
        self.queued = True
        self.discarded = False
        self.parked = False
        self.closed = False
        self.calls = calls

    def reset_output_buffer(self):
        self.calls.append("discard")
        self.queued = False
        self.discarded = True

    def flush(self):
        if self.queued:
            # Recorded rather than reproduced. Really parking here would park
            # the thread running this test, and with it the event loop and the
            # rest of the suite; what a caller can observe is the same either
            # way, which is that nothing after this point ever runs.
            self.parked = True

    def close(self):
        self.closed = True


class UnflushableWriter(FakeWriter):
    """A port whose close can never complete, and the transport behind it.

    A transport finishes a close only once its own write buffer has drained,
    and leaves it to the write path to report that it has. A port that has
    stopped accepting bytes never reports it and raises nothing: the bytes are
    merely queued behind flow control.

    abort() is the way out, but it only forces the *rest* of the close to be
    scheduled - and that remainder begins by flushing the port, which is the
    call that waits for the very queue that is not moving. So an abort issued
    without discarding the queue first hands the wait to the event loop
    instead of to one task.
    """

    def __init__(self):
        super().__init__()
        self.aborted = False
        self.connection_lost = False
        self.calls = []
        self.serial = WedgedPort(self.calls)
        # A StreamWriter exposes the transport this reaches for; here the two
        # are the same object.
        self.transport = self

    async def wait_closed(self):
        await asyncio.Event().wait()

    def abort(self):
        self.aborted = True
        self.calls.append("abort")
        # The real one schedules the remainder for the next loop iteration.
        asyncio.get_running_loop().call_soon(self._finish_closing)

    def _finish_closing(self):
        self.serial.flush()
        if self.serial.parked:
            return  # The real transport does not reach its next line either.
        self.connection_lost = True
        self.serial.close()


async def _teardown_must_return(manager) -> None:
    """Tear the connection down, and fail rather than hang if it will not end.

    The deadline protects the suite rather than the component: without it a
    regression here does not fail, it stops the run, and a stuck run says
    nothing about which test stuck it.
    """
    try:
        await asyncio.wait_for(manager.teardown(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "teardown never returned: closing a port that has stopped "
            "accepting bytes is unbounded"
        )


async def test_teardown_gives_up_on_a_port_that_will_not_close(monkeypatch):
    """The heartbeat detects a wedged modem in a minute or two and ends the
    session, and then the teardown that follows waits for a close that can
    never complete. Nothing raises, so the guard around it never fires, and by
    then the component is marked down - which is the one state the stall
    criterion deliberately keeps off. Detection would lead to a process parked
    in teardown until the down tolerance ran out, an hour later.

    The bound belongs here rather than in the supervisor, which cannot tell a
    slow teardown from a slow session body and holds no transport to abort.
    """
    monkeypatch.setattr(config, "SERIAL_CLOSE_TIMEOUT", 0.01)
    manager = _make()
    writer = UnflushableWriter()
    manager.writer = writer
    # What makes _flushed() false at close in the first place: the probe that
    # noticed the modem had gone quiet left its command in the buffer.
    writer.written.append(b"AT+CSQ\r\n")

    await _teardown_must_return(manager)

    assert writer.aborted is True
    assert manager.writer is None


async def test_the_abort_leaves_the_transport_nothing_to_wait_for(monkeypatch):
    """Forcing the close is not the whole of it. What the abort schedules
    begins by flushing the port, and flushing waits for the kernel's output
    queue - the queue that is not moving, which is why the close needed forcing
    at all. That wait runs on the event loop rather than on one task, so it
    parks the watchdog, the reconnect and the signal handler together, and a
    process that cannot exit is not restarted by any policy outside it.

    Discarding the queue first is what leaves the flush nothing to wait for.
    """
    monkeypatch.setattr(config, "SERIAL_CLOSE_TIMEOUT", 0.01)
    manager = _make()
    writer = UnflushableWriter()
    manager.writer = writer

    await _teardown_must_return(manager)
    # The transport completes the close on the next iteration, as the real one
    # does; nothing before this point could have run it.
    await asyncio.sleep(0)

    assert writer.serial.parked is False, (
        "the flush inside the abort waited for a queue that is not moving, "
        "which parks the event loop rather than one task"
    )
    assert writer.connection_lost is True
    assert writer.serial.closed is True
    # Ordered, not merely both present. The abort schedules the flush rather
    # than performing it, so today the two happen to work either way round;
    # pinning the order is what keeps that true if anything is ever awaited
    # between them, which would let the flush run first.
    assert writer.calls == ["discard", "abort"]


async def test_the_port_can_be_reopened_after_a_close_that_never_completed(monkeypatch):
    """Returning is only half of it. The supervisor reconnects straight after a
    teardown, so the point of aborting is that the close really does run to the
    end and the next attempt finds the port free rather than still held by a
    transport that is waiting to flush it.
    """
    from module import device_manager as dm_module

    monkeypatch.setattr(config, "SERIAL_CLOSE_TIMEOUT", 0.01)
    manager = _make()
    writer = UnflushableWriter()
    manager.writer = writer
    await _teardown_must_return(manager)
    await asyncio.sleep(0)

    # The port was actually given back, rather than teardown merely returning.
    assert writer.connection_lost is True
    assert writer.serial.closed is True

    reopened = FakeWriter()

    async def fake_open(url, baudrate):
        return FakeReader([b"OK\r\n"]), reopened

    monkeypatch.setattr(
        dm_module.serial_asyncio, "open_serial_connection", fake_open
    )
    manager._port_exists = lambda path: True
    manager.setup_sms = _noop_callback

    await asyncio.wait_for(manager.connect_once(), timeout=2.0)
    assert manager.writer is reopened


async def test_a_port_that_closes_normally_is_not_aborted():
    """Aborting discards whatever the transport still holds, so it must be the
    escalation and never the ordinary path."""
    manager = _make()
    writer = UnflushableWriter()

    async def closes_cleanly():
        return None

    writer.wait_closed = closes_cleanly
    manager.writer = writer

    await _teardown_must_return(manager)
    assert writer.closed is True
    assert writer.aborted is False


def test_the_serial_close_deadline_has_a_floor(monkeypatch):
    """Zero would abort every ordinary disconnect before the transport had a
    chance to flush, which is the opposite of what the deadline is for."""
    monkeypatch.setenv("SERIAL_CLOSE_TIMEOUT", "0")
    try:
        assert importlib.reload(config).SERIAL_CLOSE_TIMEOUT >= 1.0
    finally:
        monkeypatch.undo()
        importlib.reload(config)


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


def test_the_write_cannot_start_swallowing_cancellations():
    """The property in send_at_command_async that a widening edit could undo in
    silence, guarded on the shape of the code rather than on behaviour.

    The heartbeat's deadline works by cancelling the probe, and a cancelled
    probe has to leave through this method rather than be caught in it: a
    handler that swallowed the cancellation would turn that bound back into the
    hang it was written to remove. The guard here is narrow only because
    CancelledError derives from BaseException while the handler below catches
    Exception - a distinction one word wide, and one that no behavioural test
    can object to until the day it is edited away. Handling the cancellation
    explicitly is what makes widening the handler visibly wrong.
    """
    from module import device_manager as dm_module

    tree = ast.parse(pathlib.Path(dm_module.__file__).read_text())
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "send_at_command_async"
    )
    # Anchored to the try that guards the drain, not to whichever try comes
    # first in the method. Taking the first would let a second, unrelated try
    # satisfy this while the drain's own handler went on swallowing the
    # cancellation - the guard would pass on exactly the code it exists to
    # reject. Only the body is searched, so a handler mentioning drain cannot
    # nominate its own try.
    guarded = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Try)
        and any(
            isinstance(inner, ast.Call)
            and (dotted_name(inner.func) or "").endswith(".drain")
            for statement in node.body
            for inner in ast.walk(statement)
        )
    ]

    assert len(guarded) == 1, (
        "the drain is not guarded by exactly one try, so which handlers "
        "protect it can no longer be read off"
    )
    handlers = guarded[0].handlers
    assert handlers, "the write around which this property exists is gone"
    first = handlers[0]
    assert dotted_name(first.type) in ("asyncio.CancelledError", "CancelledError"), (
        "a cancellation must be handled before anything broader can catch it"
    )
    assert len(first.body) == 1 and isinstance(first.body[0], ast.Raise), (
        "the cancellation handler must do nothing but re-raise"
    )
    assert first.body[0].exc is None, (
        "the cancellation must be re-raised as it arrived"
    )


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
        await manager._handle_concat_sms_part(NUMBER, when, marker[:4], 7, 2, 1)
        await manager._handle_concat_sms_part(NUMBER, when, marker[4:], 7, 2, 2)

    assert marker not in captured.text
    assert marker[:4] not in captured.text
    assert marker[4:] not in captured.text
    # The sender is disclosure of the same kind: correlated with the timestamp
    # on the same line it records who messages this SIM and when.
    assert NUMBER not in captured.text
    assert NUMBER[:-4] not in captured.text
    # The diagnostics that replaced them must still be there.
    assert NUMBER[-4:] in captured.text
    assert "ref=7" in captured.text
    assert "2/2 received" in captured.text
    assert f"{len(marker)} character(s)" in captured.text


async def test_an_expired_concat_buffer_is_logged_without_its_payload():
    from module import device_manager as dm_module

    marker = "QQZZWWXX"
    manager = _make()
    await manager._handle_concat_sms_part(
        NUMBER, datetime(2024, 11, 2, 13, 15, 51), marker, 7, 2, 1
    )
    buffer = manager.concat_sms_cache[(NUMBER, 7)]
    buffer.first_received -= DeviceManager.CONCAT_SMS_TIMEOUT + 1

    with _LogCapture(dm_module) as captured:
        await manager._cleanup_expired_concat_cache()

    assert manager.concat_sms_cache == {}
    assert marker not in captured.text
    assert NUMBER not in captured.text
    assert NUMBER[:-4] not in captured.text


async def test_a_decoded_message_is_logged_without_its_sender():
    """The sender of a decoded message reaches the log at INFO on every single
    message. Only the suffix is needed to tell one conversation from another."""
    from module import device_manager as dm_module

    seen = {}

    async def collect(sender, timestamp, content):
        seen["sender"] = sender
        return True

    manager = DeviceManager(collect, port="/hostdev/ttyUSB2")
    with _LogCapture(dm_module) as captured:
        assert await manager._forward_pdu(DELIVER_PDU) is ForwardOutcome.DELIVERED

    sender = seen["sender"]
    assert len(sender) > 6
    assert sender not in captured.text
    assert sender[:-4] not in captured.text
    assert sender[-4:] in captured.text


async def test_the_send_path_logs_no_full_destination_number():
    from module import device_manager as dm_module

    manager = _make()
    manager._sleep = _no_delay

    async def confirming_send(command):
        manager.sms_sent_event.set()

    manager.send_at_command_async = confirming_send

    with _LogCapture(dm_module) as captured:
        assert await manager.handle_send_sms(NUMBER, "TESTMSG") is True

    assert NUMBER not in captured.text
    assert NUMBER[:-4] not in captured.text
    assert NUMBER[-4:] in captured.text


async def test_a_failed_send_logs_no_full_destination_number():
    from module import device_manager as dm_module

    manager = _make()
    manager._sleep = _no_delay

    async def failing_send(command):
        raise RuntimeError("serial write refused")

    manager.send_at_command_async = failing_send

    with _LogCapture(dm_module) as captured:
        assert await manager.handle_send_sms(NUMBER, "TESTMSG") is False

    assert NUMBER not in captured.text
    assert NUMBER[:-4] not in captured.text


async def test_a_raising_send_logs_neither_the_message_nor_the_number(monkeypatch):
    """The send path is the one place holding an outbound body, so its failure
    handler may not interpolate the exception it caught.

    The failure is injected rather than provoked, deliberately. The bundled
    encoder happens to fall back to UCS2 instead of raising on a character it
    cannot fit into GSM-7, so today it does not leak - but two raise sites in
    that module do name the offending character, no version is pinned, and the
    writes further down this block carry the encoded message too. The rule this
    asserts cannot be allowed to rest on which of those happens to fire.
    """
    from module import device_manager as dm_module

    marker = "QQZZWWXX"

    def refuse(number, text, **kwargs):
        # Shaped like the library's own message, which quotes the character it
        # rejected, and carrying the whole body and number for good measure.
        raise ValueError(
            f'Cannot encode char "{text[0]}" using GSM-7 encoding: '
            f'{text} to {number}'
        )

    monkeypatch.setattr(dm_module, "encodeSmsSubmitPdu", refuse)

    manager = _make()
    manager._sleep = _no_delay

    with _LogCapture(dm_module) as captured:
        assert await manager.handle_send_sms(NUMBER, marker) is False

    # Both views: `formatted` also renders a traceback, so reattaching exc_info
    # cannot smuggle the exception text back past this test.
    for rendered in (captured.text, captured.formatted):
        assert marker not in rendered
        assert marker[0] not in rendered
        assert NUMBER not in rendered
        assert NUMBER[:-4] not in rendered

    # Still diagnosable: the kind of failure, the masked destination, the length.
    assert "ValueError" in captured.text
    assert NUMBER[-4:] in captured.text
    assert f"{len(marker)} character(s)" in captured.text


async def test_a_timed_out_send_logs_no_full_destination_number():
    from module import device_manager as dm_module

    class _NeverConfirms:
        """Stands in for the confirmation event so the send deadline is reached
        without spending it."""

        def clear(self):
            pass

        async def wait(self):
            raise asyncio.TimeoutError

    manager = _make()
    manager._sleep = _no_delay
    manager.sms_sent_event = _NeverConfirms()

    async def silent_send(command):
        pass

    manager.send_at_command_async = silent_send

    with _LogCapture(dm_module) as captured:
        assert await manager.handle_send_sms(NUMBER, "TESTMSG") is False

    assert NUMBER not in captured.text
    assert NUMBER[:-4] not in captured.text
