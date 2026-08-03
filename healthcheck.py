"""Container healthcheck.

Two conditions, both required:
  1. The health file is fresh, proving the main loop is still running.
  2. Every component in it is up, proving the process can actually reach the
     modem and deliver messages.

Checking only that the file exists would report healthy forever once the
process wedged, because the file would still be sitting there.
"""

import json
import os
import sys
import time


def check(path: str, stale_seconds: float, now: float) -> int:
    """Return a process exit code: 0 healthy, 1 unhealthy."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return 1

    # Only staleness is checked, never a future mtime: a backwards clock step
    # leaves mtime ahead of now while the contents are still current. A large
    # forward step (NTP correcting a wrong boot clock) makes the file look
    # ancient and costs one false unhealthy, recovered on the next refresh.
    # The bias is deliberate: never report healthy when in doubt.
    if now - mtime > stale_seconds:
        return 1

    try:
        with open(path) as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return 1

    if not isinstance(payload, dict):
        return 1

    services = payload.get("services")
    if not isinstance(services, dict) or not services:
        return 1
    if not all(services.values()):
        return 1
    return 0


if __name__ == "__main__":
    import config

    sys.exit(check(config.HEALTH_FILE, config.HEALTH_STALE_SECONDS, time.time()))
