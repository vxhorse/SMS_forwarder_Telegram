import serial_asyncio
import asyncio
import os
import time
import re
from datetime import datetime
from typing import Optional, Callable, Dict, Any
import config
from config import SMS_PORT, SMS_BAUDRATE
from logger import setup_logger
from module.discovery import discover_port
from module.supervisor import Backoff
from gsmmodem.pdu import encodeSmsSubmitPdu, decodeSmsPdu, Concatenation

logger = setup_logger(__name__)

# What may appear in a log line, and why.
#
# This service carries one-time codes and bank notifications, so no log line
# may reproduce a message. The only part of a serial line that is ever echoed
# is the URC keyword, the "+NAME" ahead of the colon: it is a protocol token
# defined by the AT command set and cannot contain message text. Everything
# else is reduced to a byte count, including whatever follows the colon,
# because a line this code did not recognise may itself be a message.
_URC_KEYWORD = re.compile(rb'^\+[A-Z0-9]{1,12}')

# Bare responses defined by the AT command set. They have no payload at all, so
# quoting them in full costs nothing and says what the modem actually sent,
# which is what makes an unfamiliar module's chatter debuggable.
_SAFE_BARE_RESPONSES = frozenset({
    b'RING', b'BUSY', b'ERROR', b'CONNECT', b'RDY',
    b'NO CARRIER', b'NO ANSWER', b'NO DIALTONE',
})


def _describe_line(message: bytes) -> str:
    """Describe a serial line for the log without reproducing its payload."""
    if message in _SAFE_BARE_RESPONSES:
        return message.decode('ascii')
    match = _URC_KEYWORD.match(message)
    prefix = f"{match.group().decode('ascii')} " if match else ""
    return f"{prefix}[{len(message)} bytes]"


class ConcatSmsBuffer:
    """The parts of one concatenated message, held until they are all here."""

    def __init__(self, sender: str, ref_num: int, max_parts: int, timestamp: datetime):
        self.sender = sender
        self.ref_num = ref_num
        self.max_parts = max_parts
        # The carrier's own timestamp for the message. Message data, not a
        # duration, which is why it stays a wall-clock value.
        self.timestamp = timestamp
        self.parts: Dict[int, str] = {}  # seq_num -> content
        # Monotonic, not wall clock: a machine without a battery-backed RTC can
        # jump years forward once NTP synchronises, which would expire every
        # in-flight part at once.
        self.first_received = time.monotonic()

    def add_part(self, seq_num: int, content: str) -> None:
        """Store one part."""
        self.parts[seq_num] = content

    def is_complete(self) -> bool:
        """Whether every part has arrived."""
        return len(self.parts) == self.max_parts

    def get_merged_content(self) -> str:
        """Join the parts in sequence order."""
        return ''.join(self.parts[i] for i in sorted(self.parts.keys()))

    def is_expired(self, timeout_seconds: int = 60) -> bool:
        """Whether this buffer has been waiting for missing parts too long."""
        return (time.monotonic() - self.first_received) > timeout_seconds


class DeviceManager:
    """The modem component: one serial connection and everything on top of it.

    Supervised through the ManagedService contract (connect_once / run /
    teardown), so reconnection has exactly one owner and lives elsewhere.
    """

    # How long an incomplete concatenated message is held, in seconds.
    CONCAT_SMS_TIMEOUT = 60

    # Placeholder in the setup sequence. It is not an AT command: reaching it
    # runs _drain_stored_sms() instead.
    DRAIN_MARKER = "<DRAIN_STORED_SMS>"

    # The one destructive command in the sequence, named so the code that
    # reasons about it does not repeat the literal.
    ERASE_COMMAND = r'AT+CMGD=1,4'

    # Modem initialisation sequence.
    # One ordering constraint is load-bearing: DRAIN_MARKER must come after
    # AT+CMGF=0 and AT+CPMS (PDU mode and storage area must be selected before
    # the store can be read) and before AT+CMGD=1,4 (which erases it).
    SETUP_COMMANDS = [
        r'AT&F',                    # restore factory defaults
        r'ATE0',                    # disable echo
        r'AT+CFUN=1',               # full functionality
        r'AT+CMGF=0',               # PDU mode
        r'AT+CSCS="UCS2"',          # character set
        r'AT+CSMS=1',               # SMS service phase 2+
        r'AT+CREG=2',               # network registration URCs with location
        r'AT+CTZU=3',               # update clock and time zone from the network
        r'AT+CTZR=0',               # no time zone change reporting
        r'AT+QCFG="urc/cache",0',   # vendor specific (Quectel): no URC caching
        r'AT+QURCCFG="urcport","usbmodem"',  # vendor specific (Quectel): URC port
        r'AT+CPMS="ME","ME","ME"',  # message storage area
        DRAIN_MARKER,               # read out anything already stored, then continue
        ERASE_COMMAND,              # erase all stored messages
        r'AT+CNMI=2,2,0,0,0',       # deliver new messages straight to us
        r'AT+CSMP=17,167,0,8',      # text mode parameters, long message support
        r'AT+CSDH=1',               # verbose message headers
        r'AT+CMMS=2',               # keep the link up between messages
        r'AT&W',                    # persist settings
    ]

    # Commands the modem processes slowly enough to need a longer deadline.
    SLOW_COMMANDS = {r'AT&F', r'AT+CFUN=1', r'AT&W'}

    # Which setup failures are fatal, in full:
    #
    # A command counts as acknowledged only when its response contains OK.
    # _send_and_wait returns an empty list when the modem never answers and a
    # list ending in an error line when it refuses, so both forms of failure
    # are covered by the same test and neither is treated as success. For the
    # commands below that is fatal and setup raises; for every other command
    # it is a warning, because a module may refuse a vendor extension or a
    # convenience setting without a single message being lost.
    #
    # These four have no degraded mode. Without full functionality the radio
    # is off, without PDU mode nothing decodes, without a selected storage
    # area the store cannot be read or erased, and without new-message routing
    # nothing is ever handed to us. A process that finished setup regardless
    # would look healthy while forwarding nothing, which is the failure this
    # policy exists to prevent.
    #
    # There is a second abort path: _drain_stored_sms applies the same rule to
    # its own AT+CMGL=4. It is not listed here because it is not an entry in
    # SETUP_COMMANDS, but an unacknowledged listing is fatal for a sharper
    # reason - the next command in the sequence erases the store, so treating
    # a failed listing as an empty store destroys unread messages.
    REQUIRED_COMMANDS = {
        r'AT+CFUN=1',
        r'AT+CMGF=0',
        r'AT+CPMS="ME","ME","ME"',
        r'AT+CNMI=2,2,0,0,0',
    }

    def __init__(self, receive_sms_callback: Callable, health=None,
                 notify: Optional[Callable] = None, port: Optional[str] = None,
                 baudrate: Optional[int] = None, timeout: int = 2):
        """
        Set up the modem component.

        :param receive_sms_callback: called with each received message
        :param health: HealthState to report signal strength and freshness to
        :param notify: coroutine used to report device state changes outward
        :param port: serial device path; empty means discover it
        :param baudrate: serial speed
        :param timeout: serial read timeout, in seconds
        """

        self.receive_sms_callback = receive_sms_callback
        self.port = port or SMS_PORT
        self.baudrate = baudrate or SMS_BAUDRATE
        self.timeout = timeout

        self.max_retries = 3  # consecutive errors a loop tolerates
        self.retry_delay = 5  # seconds between retries

        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

        self.is_running = False

        self.message_queue: asyncio.Queue = asyncio.Queue()
        self.pending_sms = {"pdu": None, "expected_length": None}

        # Parts of concatenated messages, keyed by (sender, ref_num).
        self.concat_sms_cache: Dict[tuple, ConcatSmsBuffer] = {}

        assert isinstance(self.baudrate, int), "baudrate must be an integer"
        assert isinstance(self.port, str), "port must be a string"

        # Set once the modem confirms a message was sent.
        self.sms_sent_event = asyncio.Event()

        self.name = "device"
        self.health = health
        # Reports device state changes outward. Failures here must never
        # affect the device connection itself.
        self.notify = notify
        # The path actually in use, which may have been discovered rather
        # than configured.
        self.active_port: Optional[str] = None

        self.probe_interval = config.MODEM_PROBE_INTERVAL
        # One deadline covers both probes, because they ask the same question:
        # how long the modem gets to answer. _probe_modem asks it once at
        # connect time, heartbeat_loop asks it repeatedly afterwards.
        self.probe_timeout = config.MODEM_PROBE_TIMEOUT
        self.probe_failures = config.MODEM_PROBE_FAILURES
        # Set whenever a +CSQ reply arrives, which is what proves the modem is
        # still answering rather than merely still connected.
        self._probe_event = asyncio.Event()
        # One AT transaction on the port at a time. The heartbeat and the
        # sending path are independent writers, and AT+CMGS puts the modem into
        # a prompt where every byte written becomes part of the outgoing
        # message: a probe landing in that window would be sent as message
        # data and the message itself would be rejected.
        self._at_lock = asyncio.Lock()

        # Injection points so tests do not have to touch the real filesystem
        # or spend real time.
        self._sleep = asyncio.sleep
        self._port_exists = os.path.exists

    async def send_at_command_async(self, command: str) -> None:
        """
        Send one AT command without waiting for its response.

        :param command: the AT command to send
        """
        if self.writer is None:
            raise ValueError("Serial writer is not initialised")

        try:
            self.writer.write(f"{command}\r\n".encode())
            await self.writer.drain()
        except Exception as e:
            logger.warning(f"Could not write to the serial port: {e}")
        else:
            # The sending path writes a PDU through here too, and that PDU is
            # the encoded message. Only a command is ever named; anything else
            # is reported by size.
            if command.startswith("AT"):
                logger.debug(f"Sent command: {command}")
            else:
                logger.debug(f"Wrote {len(command)} character(s) of payload")

    async def resolve_port(self) -> str:
        """Return the port to use: the configured one, or a discovered one.

        An explicit setting always wins, which keeps multi-modem and unusual
        layouts working. Leaving it empty is the normal case.
        """
        if self.port:
            return self.port

        found = await discover_port(
            config.SMS_DEV_ROOT, self.baudrate, config.PORT_PROBE_TIMEOUT
        )
        if found is None:
            raise RuntimeError(
                f"No modem AT port found under {config.SMS_DEV_ROOT}; "
                f"set SMS_PORT explicitly if the device lives elsewhere"
            )
        return found

    async def _wait_for_port(self, path: str) -> None:
        """Wait for the device node to appear. No deadline, by design.

        A container can be created before its USB device finishes enumerating,
        so waiting is unbounded on purpose: there is no correct timeout for
        "the hardware is not here yet". Visibility comes from the healthcheck
        and from the notification channel instead.
        """
        backoff = Backoff(
            minimum=config.RECONNECT_BACKOFF_MIN,
            maximum=config.RECONNECT_BACKOFF_MAX,
        )
        attempts = 0
        # Every check that comes back negative is followed by a wait, and the
        # loop is the only place the node is tested, so a present node costs
        # nothing and an absent one can never fall out of the loop early.
        while not self._port_exists(path):
            attempts += 1
            delay = backoff.next_delay()
            if attempts <= 5 or attempts % 20 == 0:
                logger.warning(
                    f"Device {path} is not present yet (check {attempts}); "
                    f"retrying in {delay:.1f}s"
                )
            await self._sleep(delay)

        if attempts:
            logger.info(f"Device {path} appeared after {attempts} failed check(s)")

    async def _send_and_wait(self, command: str, timeout: float) -> list:
        """Send one AT command and read until a terminating line or timeout.

        This replaces a fixed sleep after each command, which was wrong in both
        directions: across nineteen setup commands, two seconds each spent
        thirty-eight seconds waiting on modems that had already answered, while
        still being too short for a command that happened to run long.
        """
        if self.writer is None or self.reader is None:
            raise RuntimeError("Serial connection is not open")

        self.writer.write(f"{command}\r\n".encode())
        await self.writer.drain()

        # Monotonic: this runs on boards whose wall clock jumps once the time
        # is synchronised, which would corrupt any wall-clock deadline.
        deadline = time.monotonic() + timeout
        lines: list = []

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(f"{command} timed out after {len(lines)} line(s)")
                return lines
            try:
                raw = await asyncio.wait_for(self.reader.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                logger.warning(f"{command} timed out after {len(lines)} line(s)")
                return lines

            if not raw:
                # An empty read means end of stream: the device is gone. A
                # closed stream yields it again immediately every time, so
                # treating it as a blank line would spin at full speed until
                # the deadline instead of reporting the loss.
                raise RuntimeError(f"Serial connection closed while awaiting {command}")

            line = raw.strip()
            if not line:
                continue
            lines.append(line)
            if line in (b"OK", b"ERROR") or line.startswith((b"+CME ERROR", b"+CMS ERROR")):
                return lines

    async def _probe_modem(self) -> None:
        """Confirm the modem actually responds.

        A device node existing does not mean the modem is ready; enumeration
        completes before its firmware finishes starting, and AT may be silent
        for a while after the node appears.
        """
        lines = await self._send_and_wait("AT", timeout=self.probe_timeout)
        if b"OK" not in lines:
            raise RuntimeError(f"Modem did not answer AT within {self.probe_timeout}s")
        logger.info("Modem handshake succeeded")

    async def connect_once(self) -> None:
        """Establish one connection. Any failure raises so the supervisor
        backs off and starts over from port resolution, which is what makes
        both "device vanished" and "open failed" recover without extra logic.

        Everything after the port is open is reported by raising, not by
        cleaning up here: teardown() runs after every failed attempt and is the
        single place the transport is released.
        """
        path = await self.resolve_port()
        await self._wait_for_port(path)
        self.reader, self.writer = await serial_asyncio.open_serial_connection(
            url=path, baudrate=self.baudrate
        )
        self.active_port = path
        await self._probe_modem()
        await self.setup_sms()
        self.is_running = True
        logger.warning(f"Connected to {path}")
        await self._notify(f"📶 Modem connected: {path}")

    async def run(self) -> None:
        """Long-running body. Any subtask failure propagates to reconnect.

        All three subtasks loop forever, so this parks here for the life of the
        session rather than returning; the supervisor treats a body that
        returns as a failed session.
        """
        tasks = [
            asyncio.create_task(self.read_loop()),
            asyncio.create_task(self.process_loop()),
            asyncio.create_task(self.heartbeat_loop()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
            raise RuntimeError("device subtasks ended unexpectedly")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def teardown(self) -> None:
        """Release the serial connection. Idempotent, never raises."""
        self.is_running = False
        writer, self.writer = self.writer, None
        self.reader = None
        if writer is None:
            # Nothing was ever opened, or this is the second call. Both are
            # normal: teardown runs after every failed attempt, including ones
            # that never reached the port.
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception as exc:
            logger.warning(f"Error closing serial writer: {exc}")
        await self._notify("⚠️ Modem disconnected, reconnecting")

    async def _notify(self, text: str) -> None:
        """Report outward. A dead channel must not affect the device path."""
        if self.notify is None:
            return
        try:
            await self.notify(text)
        except Exception as exc:
            logger.warning(f"Could not send device state notification: {exc}")

    async def heartbeat_loop(self) -> None:
        """Prove periodically that the modem still answers.

        A USB serial port stays open and readable when the modem behind it has
        stopped responding: reads simply never arrive, and nothing else in this
        process can tell that apart from a quiet night. Asking a question the
        modem has to answer is the only way to distinguish them.

        AT+CSQ is used rather than a bare AT because its reply prefix is
        distinctive and cannot be confused with the OK produced by the message
        sending path; the signal strength it returns is useful diagnostically.
        """
        failures = 0
        while True:
            await self._sleep(self.probe_interval)
            self._probe_event.clear()
            async with self._at_lock:
                await self.send_at_command_async("AT+CSQ")
            try:
                await asyncio.wait_for(self._probe_event.wait(), timeout=self.probe_timeout)
            except asyncio.TimeoutError:
                failures += 1
                logger.warning(f"Modem heartbeat missed ({failures} in a row)")
                if failures >= self.probe_failures:
                    raise RuntimeError(f"Modem missed {failures} consecutive heartbeats")
                continue

            # A single answer clears the count: an occasional miss on a busy
            # modem is normal, only a run of them means it has gone quiet.
            failures = 0
            if self.health is not None:
                # Keep the snapshot file fresh so the container healthcheck can
                # tell a live process from a wedged one. Marking the component
                # up is deliberately not done here: that belongs to the
                # supervisor, which waits until a session has proved stable.
                # Doing it from the heartbeat would re-stamp the health
                # timestamp on every flap cycle, and a component that keeps
                # failing just slower than this interval would never let the
                # watchdog reach its threshold.
                self.health.refresh_file()

    async def setup_sms(self) -> None:
        """Run the initialisation sequence, waiting for each response.

        Read ownership matters here: this method owns the reader, and
        read_loop must only be created after it returns. Two readers on the
        same stream would race for the modem's replies.
        """
        for command in self.SETUP_COMMANDS:
            if command == self.DRAIN_MARKER:
                await self._drain_stored_sms()
                continue

            timeout = (
                config.AT_SLOW_COMMAND_TIMEOUT if command in self.SLOW_COMMANDS
                else config.AT_COMMAND_TIMEOUT
            )
            lines = await self._send_and_wait(command, timeout=timeout)
            if b"OK" in lines:
                continue

            # Silence and refusal are both failures here; see REQUIRED_COMMANDS
            # for which ones are fatal and why. Raising hands the decision to
            # the caller, which reconnects and retries.
            if command in self.REQUIRED_COMMANDS:
                raise RuntimeError(f"Modem did not acknowledge {command}")

            if command == self.ERASE_COMMAND:
                # Deliberately not fatal: the messages have already been read
                # out at this point, so the cost is duplication rather than
                # loss. It still needs saying plainly, because a store that
                # was not erased is drained again on the next reconnect and
                # every message in it is forwarded a second time.
                logger.warning(
                    "Modem did not acknowledge the erase; stored messages may "
                    "be forwarded again after the next reconnect"
                )
                continue

            logger.warning(f"{command} was not acknowledged; continuing setup")

    async def read_loop(self) -> None:
        """Read from the serial port. Repeated failures propagate to reconnect."""
        errors = 0
        while True:
            try:
                assert self.reader is not None
                line = await self.reader.readline()
                if line:
                    await self.message_queue.put(line)
                    errors = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                errors += 1
                logger.warning(f"Serial read error: {exc}")
                if errors >= self.max_retries:
                    raise RuntimeError(f"Serial read failed {errors} times: {exc}")
                await self._sleep(self.retry_delay)

        logger.warning("Read loop stopped")

    async def process_loop(self) -> None:
        """Drain the message queue. Reconnection belongs to the supervisor."""
        errors = 0
        while True:
            try:
                message = await asyncio.wait_for(self.message_queue.get(), timeout=5)
                await self.process_message(message)
                errors = 0
            except asyncio.TimeoutError:
                # Idle tick: nudge any partially received PDU along.
                await self.handle_incoming_sms_pdu()
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                errors += 1
                logger.error(f"Message processing error: {exc}")
                if errors >= self.max_retries:
                    raise RuntimeError(f"Message processing failed {errors} times: {exc}")
                await self._sleep(self.retry_delay)

        logger.warning("Process loop stopped")

    async def process_message(self, message: bytes) -> None:
        """Route one line read from the serial port."""

        if message.endswith(b'\r\n'):
            message = message[:-2].strip()

        if message.startswith(b'"') and message.endswith(b'"'):
            message = message[1:-1]

        if message in [b'', b' ', b'OK', b'>']:
            # Nothing to route.
            return
        else:
            logger.debug(f"Processing serial line: {_describe_line(message)}")

        if message.startswith(b'+CMT:'):
            await self.handle_incoming_sms_header(message)
        elif message.startswith(b'+CSQ:'):
            # Checked ahead of the pending-PDU branch on purpose: a heartbeat
            # reply can land between a +CMT header and its PDU, and appending
            # it to the PDU would corrupt the message and lose the heartbeat.
            # A PDU line is hexadecimal, so it can never be mistaken for this.
            self._handle_csq(message)
        elif self.pending_sms["pdu"] is not None:
            await self.handle_incoming_sms_pdu(message)
        elif message.startswith(b'+CMGS:'):
            # +CMGS carries only the message reference number.
            logger.info(f"Message accepted by the modem: {message.decode('utf-8')}")
            self.sms_sent_event.set()
        elif message.startswith(b'+CREG:'):
            # Decoded outside the try so the failure path below can always
            # name the line it could not parse.
            creg_msg = message.decode('utf-8', errors='replace')
            try:
                parts = creg_msg.replace('+CREG:', '').strip().split(',')

                status = parts[0].strip()
                lac = parts[1].strip(' "') if len(parts) > 1 else "Unknown"
                ci = parts[2].strip(' "') if len(parts) > 2 else "Unknown"
                act = parts[3].strip() if len(parts) > 3 else "Unknown"

                status_desc = {
                    "0": "not registered",
                    "1": "registered, home network",
                    "2": "not registered, searching",
                    "3": "registration denied",
                    "4": "unknown",
                    "5": "registered, roaming",
                }.get(status, "unknown state")

                act_desc = {
                    "0": "GSM",
                    "2": "UTRAN",
                    "3": "GSM w/EGPRS",
                    "4": "UTRAN w/HSDPA",
                    "5": "UTRAN w/HSUPA",
                    "6": "UTRAN w/HSDPA and HSUPA",
                    "7": "E-UTRAN",
                }.get(act, "Unknown")

                logger.debug(
                    f"Network registration: {status_desc}, "
                    f"area {lac}, cell {ci}, access technology {act_desc}"
                )
            except Exception as e:
                # A +CREG line reports registration state only; it has no
                # message payload, so quoting it back is safe and useful.
                logger.debug(f"Could not parse a CREG line: {e}, line: {creg_msg!r}")
        else:
            logger.warning(f"Unhandled serial line: {_describe_line(message)}")

    def _handle_csq(self, message: bytes) -> None:
        """Parse +CSQ: <rssi>,<ber>. A malformed reply is logged, not raised."""
        try:
            payload = message.decode('utf-8').replace('+CSQ:', '').strip()
            rssi = int(payload.split(',')[0].strip())
        except (ValueError, IndexError, UnicodeDecodeError) as exc:
            logger.debug(f"Could not parse CSQ: {exc}")
            rssi = None
        if self.health is not None and rssi is not None:
            self.health.record_rssi(rssi)
        # Set regardless of whether the value parsed: the probe asks whether
        # the modem answers, not what it answered.
        self._probe_event.set()

    async def handle_incoming_sms_header(self, bytes_message: bytes) -> None:
        """
        Handle a +CMT header announcing an incoming message.

        :param bytes_message: the header line, as read from the port
        """
        message = bytes_message.decode('utf-8', errors='ignore')

        # The length may appear as "+CMT: <length>" or "+CMT: ,<length>".
        match = re.search(r'\+CMT:\s*(?:,\s*)?(\d+)', message)

        if match:
            pdu_length = int(match.group(1))

            # Start accumulating the PDU that follows this header.
            self.pending_sms = {
                "pdu": b"",
                "expected_length": pdu_length
            }

            logger.debug(f"Expecting a {pdu_length} byte PDU")
        else:
            # Worth reporting, because an unparsable header means a message is
            # about to be dropped. Described rather than quoted: this code did
            # not recognise the line, so it cannot promise it holds no message.
            logger.warning(
                f"Could not read a PDU length from the message header: "
                f"{_describe_line(bytes_message)}"
            )

    async def _forward_pdu(self, pdu_hex: str, force_process: bool = False) -> bool:
        """Decode one PDU and forward it, merging concatenated parts.

        Both the live push path and the startup drain go through here so the
        two cannot drift apart.
        """
        try:
            decoded = decodeSmsPdu(pdu_hex)

            sender = decoded.get('number', 'Unknown')
            # The library exposes the service centre timestamp as 'time'. Using
            # any other key silently falls back to the local clock, which is
            # wrong by seconds in normal operation and wrong by years on a
            # machine that boots without a valid RTC. It would also defeat the
            # point of draining stored messages, since every recovered message
            # would be stamped with the moment it was recovered.
            timestamp = decoded.get('time') or datetime.now()
            timestamp_str = (
                timestamp.strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(timestamp, datetime) else str(timestamp)
            )
            content = decoded.get('text', '')

            concat_info = None
            for header in decoded.get('udh', []):
                if isinstance(header, Concatenation):
                    concat_info = {
                        'ref': header.reference,
                        'max': header.parts,
                        'seq': header.number,
                    }
                    break

            if concat_info:
                logger.debug(
                    f"Concatenated message part ref={concat_info['ref']} "
                    f"seq={concat_info['seq']}/{concat_info['max']}"
                )
                await self._handle_concat_sms_part(
                    sender, timestamp, content,
                    concat_info['ref'], concat_info['max'], concat_info['seq']
                )
            else:
                logger.info(
                    f"Decoded message from {sender} at {timestamp_str}"
                    + (" (forced, may be incomplete)" if force_process else "")
                )
                await self.receive_sms_callback(sender, timestamp_str, content)
            return True

        except Exception as exc:
            # The message body must never reach the log.
            logger.error(f"Could not decode PDU: {exc}")
            return False

    async def _drain_stored_sms(self) -> int:
        """Read messages already in the modem's store and forward them.

        Anything that arrives while the process is not running lands in the
        modem's storage. Erasing the store during startup without reading it
        first means the process silently destroys those messages, which is
        exactly the window a restart is supposed to recover from.
        """
        lines = await self._send_and_wait(
            'AT+CMGL=4', timeout=config.AT_SLOW_COMMAND_TIMEOUT
        )

        if b"OK" not in lines:
            # Only an acknowledged listing proves anything about the store.
            # Silence means the modem never answered; an error line means it
            # answered that it could not list the store, which a modem whose
            # storage is still busy after a reset does routinely. Neither says
            # the store is empty, and the next command in the sequence erases
            # it, so continuing would destroy messages that were never read.
            raise RuntimeError("Modem did not acknowledge AT+CMGL=4; store left unread")

        forwarded = 0
        entries = 0
        index = 0
        while index < len(lines):
            if lines[index].startswith(b'+CMGL:') and index + 1 < len(lines):
                entries += 1
                pdu_hex = lines[index + 1].decode('ascii', errors='ignore').strip()
                # One unreadable entry must not cost us the rest of the store.
                if await self._forward_pdu(pdu_hex):
                    forwarded += 1
                index += 2
            else:
                index += 1

        if forwarded < entries:
            # These are about to be erased and cannot be recovered afterwards.
            logger.error(
                f"{entries - forwarded} of {entries} stored message(s) could "
                f"not be decoded and will not be forwarded"
            )
        if forwarded:
            logger.warning(f"Recovered {forwarded} message(s) from modem storage")
        elif not entries:
            logger.info("Modem listed an empty storage area")
        return forwarded

    async def handle_incoming_sms_pdu(self, pdu_part: bytes = b'', force_process: bool = False) -> None:
        """Accumulate a pushed PDU and forward it once it is complete.

        :param pdu_part: newly received slice of PDU data
        :param force_process: decode what has arrived even if it looks short
        """
        if self.pending_sms["pdu"] is None:
            return

        self.pending_sms["pdu"] += pdu_part

        if len(self.pending_sms["pdu"]) >= self.pending_sms["expected_length"] * 2 or force_process:
            pdu_hex = self.pending_sms["pdu"].decode('ascii', errors='ignore').strip()
            try:
                await self._forward_pdu(pdu_hex, force_process=force_process)
            finally:
                # Always reset, so one bad message cannot wedge the next one.
                self.pending_sms = {"pdu": None, "expected_length": None}
        else:
            logger.debug(
                f"PDU incomplete: {len(self.pending_sms['pdu'])} of "
                f"{self.pending_sms['expected_length'] * 2} bytes received"
            )
    
    async def _handle_concat_sms_part(
        self, sender: str, timestamp: datetime, content: str,
        ref_num: int, max_parts: int, seq_num: int
    ) -> None:
        """
        Handle one part of a concatenated message.

        :param sender: sender number
        :param timestamp: the carrier's timestamp for the message
        :param content: this part's text
        :param ref_num: reference number shared by every part of one message
        :param max_parts: how many parts the message has
        :param seq_num: this part's position, counting from 1
        """
        cache_key = (sender, ref_num)

        logger.debug(
            f"Concatenated part from {sender}: ref={ref_num}, "
            f"part {seq_num}/{max_parts}, {len(content)} character(s)"
        )

        await self._cleanup_expired_concat_cache()

        if cache_key not in self.concat_sms_cache:
            self.concat_sms_cache[cache_key] = ConcatSmsBuffer(
                sender=sender,
                ref_num=ref_num,
                max_parts=max_parts,
                timestamp=timestamp
            )

        buffer = self.concat_sms_cache[cache_key]
        buffer.add_part(seq_num, content)

        logger.info(
            f"Concatenated part held from {sender}: ref={ref_num}, "
            f"{len(buffer.parts)}/{max_parts} received"
        )

        if buffer.is_complete():
            merged_content = buffer.get_merged_content()
            timestamp_str = buffer.timestamp.strftime("%Y-%m-%d %H:%M:%S") if isinstance(buffer.timestamp, datetime) else str(buffer.timestamp)

            logger.info(
                f"Concatenated message complete from {sender} at {timestamp_str}: "
                f"{max_parts} part(s), {len(merged_content)} character(s)"
            )

            await self.receive_sms_callback(sender, timestamp_str, merged_content)

            del self.concat_sms_cache[cache_key]

    async def _cleanup_expired_concat_cache(self) -> None:
        """Drop concatenated messages whose missing parts never arrived."""
        expired_keys = [
            key for key, buffer in self.concat_sms_cache.items()
            if buffer.is_expired(self.CONCAT_SMS_TIMEOUT)
        ]

        for key in expired_keys:
            buffer = self.concat_sms_cache[key]
            # The parts received so far are deliberately not forwarded: half a
            # message is worse than none, because it reads as a whole one.
            logger.warning(
                f"Concatenated message timed out from {buffer.sender}: "
                f"ref={buffer.ref_num}, only {len(buffer.parts)}/"
                f"{buffer.max_parts} part(s) arrived; discarding them"
            )
            del self.concat_sms_cache[key]

    async def handle_send_sms(self, phone_number: str, message: str) -> bool:
        """
        Send one message.

        :param phone_number: destination number
        :param message: text to send
        :return: whether the modem confirmed the send
        """
        logger.debug(f"Sending to {phone_number}, {len(message)} character(s)")

        try:
            self.sms_sent_event.clear()

            if not phone_number.strip():
                logger.warning("Destination number is empty; send cancelled")
                return False

            pdus = encodeSmsSubmitPdu(phone_number, message, requestStatusReport=True)
            logger.debug(f"{len(pdus)} PDU(s) to send")

            for i, pdu in enumerate(pdus, 1):
                pdu_hex = pdu.data.hex().upper()

                smsc_length = int(pdu_hex[:2], 16)
                pdu_length = (len(pdu_hex) - (smsc_length + 1) * 2) // 2

                logger.debug(f"Sending PDU {i}, length {pdu_length}")

                # Held across both writes: everything between the AT+CMGS and
                # the terminating Ctrl+Z is taken by the modem as message data.
                async with self._at_lock:
                    await self.send_at_command_async(f'AT+CMGS={pdu_length}')
                    await self._sleep(1)  # give the modem time to prompt

                    # The PDU is the encoded message, so only its size is
                    # logged. Ctrl+Z is what submits it.
                    logger.debug(f"Writing {len(pdu_hex)} hex character(s) of PDU data")
                    await self.send_at_command_async(pdu_hex + chr(26))

            logger.info(f"Sent to {phone_number}; awaiting the modem's result")
            await asyncio.wait_for(self.sms_sent_event.wait(), timeout=10.0)

            # Reached only once the modem answered +CMGS.
            logger.info(f"Send confirmed: {phone_number}")
            return True

        except asyncio.TimeoutError:
            logger.error(f"Timed out awaiting the send result for {phone_number}")
            return False
        except Exception as e:
            logger.error(f"Send failed for {phone_number}: {e}", exc_info=True)
            return False
        finally:
            self.sms_sent_event.clear()
