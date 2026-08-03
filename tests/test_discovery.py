import asyncio
import os

import pytest

from module.discovery import candidate_ports, discover_port, probe_port


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


def test_by_id_entries_come_first(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "ttyUSB0"))
    _touch(os.path.join(root, "serial/by-id/usb-Vendor_Modem-if04-port0"))
    ports = candidate_ports(root)
    assert ports[0].endswith("usb-Vendor_Modem-if04-port0")


def test_duplicates_are_removed_by_realpath(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "ttyUSB0"))
    os.makedirs(os.path.join(root, "serial/by-id"), exist_ok=True)
    os.symlink("../../ttyUSB0", os.path.join(root, "serial/by-id/usb-Vendor-if04"))
    ports = candidate_ports(root)
    assert len(ports) == 1


def test_builtin_serial_ports_are_never_probed(tmp_path):
    """ttyS* may be the kernel console; writing AT to it is not acceptable."""
    root = str(tmp_path)
    _touch(os.path.join(root, "ttyS0"))
    _touch(os.path.join(root, "ttyUSB0"))
    ports = candidate_ports(root)
    assert not any(p.endswith("ttyS0") for p in ports)
    assert any(p.endswith("ttyUSB0") for p in ports)


def test_acm_devices_are_included(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "ttyACM0"))
    assert any(p.endswith("ttyACM0") for p in candidate_ports(root))


def test_empty_root_yields_no_candidates(tmp_path):
    assert candidate_ports(str(tmp_path)) == []


class _FakeReader:
    def __init__(self, lines):
        self.lines = list(lines)

    async def readline(self):
        if not self.lines:
            await asyncio.sleep(3600)
        return self.lines.pop(0)


class _FakeWriter:
    def __init__(self):
        self.written = []
        self.closed = False
        self.wait_closed_called = False

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.wait_closed_called = True


def _opener_for(mapping):
    """Build an opener whose response depends on the port path.

    Records every call's kwargs and the writer it handed back, so tests can
    assert on how the port was opened (e.g. was `exclusive` requested) and
    whether the writer was later closed.
    """
    calls = []
    writers = {}

    async def opener(url, baudrate, **kwargs):
        calls.append({"url": url, "baudrate": baudrate, **kwargs})
        if url not in mapping:
            raise OSError(f"cannot open {url}")
        writer = _FakeWriter()
        writers[url] = writer
        return _FakeReader(mapping[url]), writer

    opener.calls = calls
    opener.writers = writers
    return opener


async def test_probe_accepts_a_port_that_answers_ok():
    opener = _opener_for({"/dev/ttyUSB2": [b"AT\r\n", b"OK\r\n"]})
    assert await probe_port("/dev/ttyUSB2", 115200, 1.0, opener=opener) is True


async def test_probe_rejects_a_silent_port():
    opener = _opener_for({"/dev/ttyUSB0": []})
    assert await probe_port("/dev/ttyUSB0", 115200, 0.05, opener=opener) is False


async def test_probe_rejects_a_port_that_cannot_be_opened():
    opener = _opener_for({})
    assert await probe_port("/dev/ttyUSB9", 115200, 0.05, opener=opener) is False


async def test_probe_closes_the_port_after_a_successful_answer():
    """A leaked handle here would lock the real device out of the connection
    that immediately follows discovery, so the OK path must still close."""
    opener = _opener_for({"/dev/ttyUSB2": [b"AT\r\n", b"OK\r\n"]})
    assert await probe_port("/dev/ttyUSB2", 115200, 1.0, opener=opener) is True
    writer = opener.writers["/dev/ttyUSB2"]
    assert writer.closed is True
    assert writer.wait_closed_called is True


async def test_probe_closes_the_port_after_a_silent_timeout():
    """The failure path is the one that matters: a candidate that never
    answers must still release the port before discovery moves on."""
    opener = _opener_for({"/dev/ttyUSB0": []})
    assert await probe_port("/dev/ttyUSB0", 115200, 0.05, opener=opener) is False
    writer = opener.writers["/dev/ttyUSB0"]
    assert writer.closed is True
    assert writer.wait_closed_called is True


async def test_probe_closes_the_port_when_no_answer_matches():
    """Several non-matching replies followed by silence must still end in
    the port being released, not just a pure-silence timeout."""
    opener = _opener_for({"/dev/ttyUSB1": [b"ERROR\r\n", b"RING\r\n"]})
    assert await probe_port("/dev/ttyUSB1", 115200, 0.05, opener=opener) is False
    writer = opener.writers["/dev/ttyUSB1"]
    assert writer.closed is True
    assert writer.wait_closed_called is True


async def test_probe_has_nothing_to_close_when_open_fails():
    """When the port cannot even be opened, no writer exists, so there is
    nothing to leak; this is the third distinct exit path from probe_port."""
    opener = _opener_for({})
    assert await probe_port("/dev/ttyUSB9", 115200, 0.05, opener=opener) is False
    assert opener.writers == {}


async def test_probe_requests_exclusive_open_on_posix(monkeypatch):
    """exclusive=True stops discovery from stealing a port another process
    already holds; pyserial only supports the flag on POSIX."""
    monkeypatch.setattr(os, "name", "posix")
    opener = _opener_for({"/dev/ttyUSB2": [b"OK\r\n"]})
    await probe_port("/dev/ttyUSB2", 115200, 1.0, opener=opener)
    assert opener.calls[-1].get("exclusive") is True


async def test_probe_omits_exclusive_flag_off_posix(monkeypatch):
    """On a platform where pyserial does not support the flag, it must not
    be forwarded at all."""
    monkeypatch.setattr(os, "name", "nt")
    opener = _opener_for({"/dev/ttyUSB2": [b"OK\r\n"]})
    await probe_port("/dev/ttyUSB2", 115200, 1.0, opener=opener)
    assert "exclusive" not in opener.calls[-1]


async def test_discover_returns_the_first_port_that_answers(tmp_path):
    """A module exposes several serial ports and only one speaks AT."""
    root = str(tmp_path)
    for name in ("ttyUSB0", "ttyUSB1", "ttyUSB2"):
        _touch(os.path.join(root, name))
    opener = _opener_for({os.path.join(root, "ttyUSB2"): [b"OK\r\n"]})
    found = await discover_port(root, 115200, 0.05, opener=opener)
    assert found == os.path.join(root, "ttyUSB2")


async def test_discover_returns_none_when_nothing_answers(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "ttyUSB0"))
    assert await discover_port(root, 115200, 0.05, opener=_opener_for({})) is None
