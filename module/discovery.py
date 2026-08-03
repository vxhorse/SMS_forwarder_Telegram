"""Automatic discovery of the modem's AT command port.

Modules of this class typically enumerate several serial ports, only one of
which accepts AT commands. Asking the user to work out which one is the first
thing that goes wrong on a fresh install, and hard-coding a path makes the
configuration specific to one machine. Discovery probes the plausible
candidates and keeps the first that answers.
"""

import asyncio
import glob
import os
import time
from typing import Callable, List, Optional

import serial_asyncio

from logger import setup_logger

logger = setup_logger(__name__)

# Order matters. by-id entries are stable across re-enumeration and are always
# USB devices, so they are tried first. Built-in serial ports (ttyS*) are
# deliberately absent: on many boards ttyS0 is the kernel console, and writing
# AT to a console is not an acceptable side effect of starting up.
_CANDIDATE_PATTERNS = (
    "serial/by-id/*",
    "ttyUSB*",
    "ttyACM*",
)


def candidate_ports(dev_root: str) -> List[str]:
    """List plausible modem ports under dev_root, most specific first."""
    seen = set()
    found: List[str] = []
    for pattern in _CANDIDATE_PATTERNS:
        for path in sorted(glob.glob(os.path.join(dev_root, pattern))):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            found.append(path)
    return found


async def probe_port(
    path: str,
    baudrate: int,
    timeout: float,
    opener: Callable = serial_asyncio.open_serial_connection,
) -> bool:
    """Return True if the port answers a bare AT with OK within the timeout."""
    reader = writer = None
    open_kwargs = {"url": path, "baudrate": baudrate}
    if os.name == "posix":
        # Do not steal a port another process already holds. pyserial only
        # supports this flag on POSIX.
        open_kwargs["exclusive"] = True
    try:
        reader, writer = await opener(**open_kwargs)
    except Exception as exc:
        logger.debug(f"Cannot open {path}: {exc}")
        return False

    try:
        writer.write(b"AT\r\n")
        await writer.drain()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
            if line.strip() == b"OK":
                return True
    except Exception as exc:
        logger.debug(f"Probe of {path} failed: {exc}")
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def discover_port(
    dev_root: str,
    baudrate: int,
    timeout: float,
    opener: Callable = serial_asyncio.open_serial_connection,
) -> Optional[str]:
    """Probe candidates in order and return the first that speaks AT."""
    candidates = candidate_ports(dev_root)
    if not candidates:
        logger.warning(f"No candidate serial ports under {dev_root}")
        return None

    logger.info(f"Probing {len(candidates)} candidate port(s) under {dev_root}")
    for path in candidates:
        if await probe_port(path, baudrate, timeout, opener=opener):
            logger.info(f"Discovered modem AT port: {path}")
            return path
        logger.debug(f"No AT response from {path}")

    logger.warning("No candidate port answered AT")
    return None
