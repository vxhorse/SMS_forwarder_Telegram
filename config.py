import os

# Log level, taken from the environment. INFO unless overridden.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Modem settings.
# Leave empty to auto-discover the modem's AT port.
SMS_PORT = os.getenv("SMS_PORT", "").strip()
SMS_BAUDRATE = int(os.getenv("SMS_BAUDRATE", "115200"))

# Telegram bot settings.
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_telegram_bot_token")
CHAT_ID = os.getenv("CHAT_ID", "your_telegram_chat_id")
# Outbound proxy for the Telegram API. Unset by default: a proxy is a property
# of one particular network, not a sensible default. Unset and empty both mean
# "connect directly".
PROXY_URL = os.getenv("PROXY_URL") or None

# Health snapshot file written by the process and read by the container healthcheck.
HEALTH_FILE = os.getenv("HEALTH_FILE", "/tmp/healthy")
# How old the health file may be before the healthcheck considers it stale.
#
# Floored, because MODEM_PROBE_INTERVAL below is clamped to half this window
# but never below one second, and that probe is the shortest guaranteed refresh
# interval in the process. A window under two seconds would therefore be
# shorter than the interval at which the file can possibly be rewritten, and
# the healthcheck would fail a process that is working perfectly. Two seconds
# is that bound.
HEALTH_STALE_SECONDS = max(2, int(os.getenv("HEALTH_STALE_SECONDS", "120")))
# Exit the process once any component has been down this long, letting the
# container runtime restart everything as a last resort.
WATCHDOG_DOWN_SECONDS = int(os.getenv("WATCHDOG_DOWN_SECONDS", "3600"))
# Exponential backoff bounds for component reconnection, in seconds.
RECONNECT_BACKOFF_MIN = float(os.getenv("RECONNECT_BACKOFF_MIN", "1.0"))
RECONNECT_BACKOFF_MAX = float(os.getenv("RECONNECT_BACKOFF_MAX", "30.0"))
# How long a connected session must last before it counts as a recovery.
# Supervisor._serve_session states what this buys and why; in short, a component
# that connects and then fails immediately is still broken.
#
# Floored because at zero every connection counts as a recovery the instant it
# is made, which is precisely the behaviour this setting exists to prevent.
SERVICE_STABLE_SECONDS = max(5.0, float(os.getenv("SERVICE_STABLE_SECONDS", "60.0")))
# How often the watchdog inspects component health, in seconds. Floored because
# a value of zero would turn the watchdog into a busy loop.
WATCHDOG_CHECK_INTERVAL = max(1.0, float(os.getenv("WATCHDOG_CHECK_INTERVAL", "30.0")))
# Deadline for one outward component-state notification, in seconds.
#
# Sending a notification retries several times with a delay between attempts, so
# an unreachable messaging API can hold its caller for far longer than the send
# itself would suggest. The device path awaits one when it connects and another
# while it is shutting down: unbounded, the first slows every reconnect cycle
# and the second can outlast the stop grace period the container runtime allows,
# turning a clean stop into a kill. Floored because zero would abandon every
# notification before it was attempted, and capped because an over-large value
# reinstates exactly the stall the deadline exists to prevent.
#
# There is nothing in this process to clamp the cap against: the stop grace
# period is chosen by whatever starts the container and is not visible from in
# here. Ten seconds is the shortest one in common use, so a notification that
# could outlast that is one that could outlast the whole stop.
NOTIFY_TIMEOUT_CEILING = 10.0
NOTIFY_TIMEOUT = max(1.0, min(
    float(os.getenv("NOTIFY_TIMEOUT", "5.0")),
    NOTIFY_TIMEOUT_CEILING,
))

# Root of the device tree to scan. Inside a container this points at the
# bind-mounted host /dev; running directly on a host it is just /dev.
SMS_DEV_ROOT = os.getenv("SMS_DEV_ROOT", "/dev")
# How long a candidate port has to answer AT during discovery, in seconds.
PORT_PROBE_TIMEOUT = float(os.getenv("PORT_PROBE_TIMEOUT", "3.0"))

# Timeout for a single AT command to produce a terminating response, in seconds.
AT_COMMAND_TIMEOUT = float(os.getenv("AT_COMMAND_TIMEOUT", "3.0"))
# Longer timeout for commands the modem processes slowly (AT&F, AT+CFUN, AT&W).
AT_SLOW_COMMAND_TIMEOUT = float(os.getenv("AT_SLOW_COMMAND_TIMEOUT", "10.0"))

# Modem liveness probe (AT+CSQ): interval, response deadline, and how many
# consecutive misses trigger a reconnect.
#
# The probe also refreshes the health snapshot file. Both component loops do
# that, and either one is enough, because the file is only written while every
# component is up. The interval is still clamped to half of
# HEALTH_STALE_SECONDS so that this side alone can keep the file fresh: leaving
# it to the other component's loop would couple the healthcheck to whatever
# period that loop happens to have. Half the window means a single missed probe
# is not enough to trip it, and the floor keeps a value of zero from turning
# the probe into a busy loop.
MODEM_PROBE_INTERVAL = max(1.0, min(
    float(os.getenv("MODEM_PROBE_INTERVAL", "30.0")),
    HEALTH_STALE_SECONDS / 2,
))
# Also governs the AT handshake performed right after the port opens: both ask
# the same question, which is how long the modem gets to answer a probe.
MODEM_PROBE_TIMEOUT = float(os.getenv("MODEM_PROBE_TIMEOUT", "5.0"))
MODEM_PROBE_FAILURES = int(os.getenv("MODEM_PROBE_FAILURES", "3"))

# Consecutive heartbeat readings of "not on the network" before the connection
# counts as failed.
#
# The liveness probe above proves the modem answers, which is not the same as
# proving a message can reach it: a SIM the network has detached answers every
# command exactly as before while nothing arrives. Registration also dips for a
# moment whenever a modem hands over between cells, so one reading is not
# evidence of anything and only a run of them means the radio is not attached.
# Floored at two for that reason, and because zero would switch the check off
# while looking like a setting.
MODEM_REGISTRATION_FAILURES = max(2, int(os.getenv("MODEM_REGISTRATION_FAILURES", "3")))

# The longest gap the heartbeat can legitimately leave between two refreshes of
# the health snapshot. A missed probe costs its whole interval plus the deadline
# it waits for a reply, and MODEM_PROBE_FAILURES of them in a row have to pass
# before the probe gives up and raises. For all of that time the component is
# working and has simply not refreshed the file. Anything that treats a gap this
# short as a failure is reading a slow modem as a dead one.
WATCHDOG_REFRESH_BUDGET = MODEM_PROBE_FAILURES * (
    MODEM_PROBE_INTERVAL + MODEM_PROBE_TIMEOUT
)
# Doubled for margin, because the probe is not the only thing the loop does
# between refreshes. The second term keeps the floor positive if the probe is
# configured never to retry at all.
WATCHDOG_STALL_FLOOR = max(2 * WATCHDOG_REFRESH_BUDGET, 4 * MODEM_PROBE_INTERVAL)

# How long the health snapshot may go unrefreshed before the watchdog treats
# the process as stalled. A component that blocks without raising never reaches
# mark_down, so this is the only signal that catches it.
#
# Bounded at both ends. Below the floor above, the watchdog restarts a process
# that is merely riding out a slow modem. Above WATCHDOG_DOWN_SECONDS, a stall
# outlives the operator's stated tolerance for losing a component outright -
# and a stall is a component lost without saying so, so it must not be given
# more room than one that says so.
#
# The floor is applied last, so it wins if the two ever conflict. Holding the
# threshold above the down tolerance only delays a restart; cutting it below the
# refresh budget restarts a process that is working.
#
# This belongs with the watchdog settings above, and sits here instead because
# it is derived from the modem probe settings, which are themselves derived from
# HEALTH_STALE_SECONDS. Naming any of them earlier in the file would be a
# forward reference, and the module would fail to import.
WATCHDOG_STALL_SECONDS = max(
    WATCHDOG_STALL_FLOOR,
    min(
        float(WATCHDOG_DOWN_SECONDS),
        float(os.getenv("WATCHDOG_STALL_SECONDS", str(2 * HEALTH_STALE_SECONDS))),
    ),
)
