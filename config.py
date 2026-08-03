import os

# Settings whose value the operator asked for was overridden by a floor or a
# ceiling, because a value outside that range would either disable the guard
# the setting exists for or reinstate the exact failure the guard exists to
# prevent. Recorded here rather than logged immediately: this module cannot
# import the logger, since logger.py reads its level from here and importing
# it back would be circular. main.py logs each of these once the logger
# exists.
CLAMP_NOTICES: list = []


def _clamped(name: str, requested, low=None, high=None):
    """Apply a floor and/or a ceiling to an operator-supplied setting.

    Returns the value actually in force. When a bound moves the value away
    from what was requested, appends a notice to CLAMP_NOTICES naming the
    setting, what was asked for, and what is running instead - an operator
    who tunes a setting outside its allowed range gets told, rather than
    left believing it took effect.

    Only ever called below with a numeric setting and a literal name typed
    at the call site, never with an arbitrary key - so it has no way to be
    pointed at BOT_TOKEN or CHAT_ID, which are credentials and have no floor
    or ceiling of their own regardless.
    """
    applied = requested
    if low is not None:
        applied = max(low, applied)
    if high is not None:
        applied = min(high, applied)
    if applied != requested:
        if low is not None and applied == low:
            reason = f"floor is {low:g}"
        else:
            reason = f"ceiling is {high:g}"
        CLAMP_NOTICES.append(
            f"{name} was requested as {requested:g} but is running as "
            f"{applied:g} ({reason})"
        )
    return applied


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

# The timings the Telegram polling loop is built from.
#
# Deliberately not settable from the environment, unlike almost everything else
# here. WATCHDOG_STALL_FLOOR below is derived from them, and that floor is only
# correct while it describes the loop it is measuring: lengthening a deadline
# here without moving the floor would put the stall threshold underneath the
# time one working iteration can legitimately take, and the watchdog would
# restart a process that is doing its job. Keeping them together is what makes
# that impossible to do by halves.
#
# Deadline for one whole HTTP request to the API.
TELEGRAM_REQUEST_TIMEOUT = 60.0
# How long one getUpdates call parks on the server waiting for an update. Must
# stay below the request deadline, which has to cover this wait plus the round
# trip, or every poll expires on its own deadline before the API answers.
TELEGRAM_LONG_POLL_SECONDS = 50
# Attempts one outgoing message gets, and the wait between them.
TELEGRAM_SEND_ATTEMPTS = 3
TELEGRAM_SEND_RETRY_DELAY = 5.0

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
HEALTH_STALE_SECONDS = _clamped(
    "HEALTH_STALE_SECONDS", int(os.getenv("HEALTH_STALE_SECONDS", "120")), low=2
)
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
SERVICE_STABLE_SECONDS = _clamped(
    "SERVICE_STABLE_SECONDS",
    float(os.getenv("SERVICE_STABLE_SECONDS", "60.0")),
    low=5.0,
)
# How often the watchdog inspects component health, in seconds. Floored because
# a value of zero would turn the watchdog into a busy loop.
WATCHDOG_CHECK_INTERVAL = _clamped(
    "WATCHDOG_CHECK_INTERVAL",
    float(os.getenv("WATCHDOG_CHECK_INTERVAL", "30.0")),
    low=1.0,
)
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
NOTIFY_TIMEOUT = _clamped(
    "NOTIFY_TIMEOUT",
    float(os.getenv("NOTIFY_TIMEOUT", "5.0")),
    low=1.0,
    high=NOTIFY_TIMEOUT_CEILING,
)

# Root of the device tree to scan. Inside a container this points at the
# bind-mounted host /dev; running directly on a host it is just /dev.
SMS_DEV_ROOT = os.getenv("SMS_DEV_ROOT", "/dev")
# How long a candidate port has to answer AT during discovery, in seconds.
PORT_PROBE_TIMEOUT = float(os.getenv("PORT_PROBE_TIMEOUT", "3.0"))

# Timeout for a single AT command to produce a terminating response, in seconds.
AT_COMMAND_TIMEOUT = float(os.getenv("AT_COMMAND_TIMEOUT", "3.0"))
# Longer timeout for commands the modem processes slowly (AT&F, AT+CFUN, AT&W).
AT_SLOW_COMMAND_TIMEOUT = float(os.getenv("AT_SLOW_COMMAND_TIMEOUT", "10.0"))

# How long the serial transport gets to flush what it is still holding when the
# connection is released, in seconds.
#
# A close completes only once that buffer has drained, and the transport leaves
# it to the write path to say when it has. A port that has stopped accepting
# bytes never says it, and nothing raises: the bytes are simply queued behind
# flow control. The probe that noticed the modem had gone quiet is exactly what
# leaves bytes in the buffer, so the close that follows such a detection is the
# one most likely never to end - and an unbounded wait there parks the process
# in teardown, still holding the port, until the watchdog's down tolerance runs
# out an hour later.
#
# Past the deadline the transport is aborted instead, which discards the buffer
# and forces the close through. Floored because zero would abort every ordinary
# disconnect before the transport had a chance to flush, and an abort throws
# away whatever was still queued.
SERIAL_CLOSE_TIMEOUT = _clamped(
    "SERIAL_CLOSE_TIMEOUT", float(os.getenv("SERIAL_CLOSE_TIMEOUT", "5.0")), low=1.0
)

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
MODEM_PROBE_INTERVAL = _clamped(
    "MODEM_PROBE_INTERVAL",
    float(os.getenv("MODEM_PROBE_INTERVAL", "30.0")),
    low=1.0,
    high=HEALTH_STALE_SECONDS / 2,
)
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
MODEM_REGISTRATION_FAILURES = _clamped(
    "MODEM_REGISTRATION_FAILURES",
    int(os.getenv("MODEM_REGISTRATION_FAILURES", "3")),
    low=2,
)

# Whether to ask the registration question at all.
#
# Deliberately a separate setting from the count above rather than a zero it
# could take. Turning the check off and tuning how patient it is are different
# decisions, and folding them into one number means an operator adjusting the
# patience can switch the guard off by accident - so the count keeps a floor it
# cannot be tuned below, and switching off is spelt out here instead.
#
# Off by default, which is an opt-in and not an oversight. The question can
# have no true answer: the registration state read by this check describes the
# circuit-switched domain, and a network that attaches a module for packet
# service alone, delivering messages over a path that state does not describe,
# reports "not registered" while every message arrives. See the known
# limitation recorded beside _REGISTERED_STATES in module/device_manager.py for
# what a complete answer would require.
#
# On such a network the check does more than report nothing useful, and this is
# what decides the default rather than the noise alone. It raises only after
# MODEM_REGISTRATION_FAILURES misses spaced MODEM_PROBE_INTERVAL apart, which
# at the values above is longer than SERVICE_STABLE_SECONDS - so every cycle
# reaches the point where the session counts as recovered before the point
# where it fails. Each recovery re-stamps the health record, which is what both
# watchdog criteria measure from, so neither the down clock nor the stall clock
# accumulates across cycles and the loop is invisible to both. The snapshot
# goes stale for only the width of one teardown, so the container healthcheck
# stays green through it too. Meanwhile every cycle reinitialises the radio and
# rewrites the modem's stored profile. A guard whose false positive cannot be
# seen by anything watching is not one to ship enabled.
#
# Turning it on is a decision to take once the answer is known for the SIM and
# the network in use, and costs nothing to defer: setup asks the modem to
# report registration changes unasked, so the state is still parsed, still
# published in the health snapshot and still logged with this off. What is
# deferred is only whether a run of unregistered readings ends the session.
# Read the registration field of the snapshot over a period that includes
# ordinary message traffic; if it settles on 1 or 5 - or on 6 or 7, registered
# for messages alone - then the question has a true answer on that network and
# MODEM_REGISTRATION_CHECK=1 buys the detection it was built for. If it sits at
# 0 or 2 while messages keep arriving, leave it off: that is the network this
# check cannot describe.
MODEM_REGISTRATION_CHECK = os.getenv(
    "MODEM_REGISTRATION_CHECK", "0"
).strip().lower() not in ("0", "false", "no", "off")

# The gap the heartbeat can legitimately leave between two reports of progress,
# written as the rounds that make it up.
#
# The device loop reports its own progress and refreshes the health snapshot at
# the same point, so this one figure bounds both gaps.
#
# Only a round whose liveness probe went unanswered skips that point, and such
# a round costs the interval before it plus the one deadline it spent waiting.
# MODEM_PROBE_FAILURES - 1 of them can pass before the next one gives up and
# raises. The round that does refresh costs an interval and two deadlines rather
# than one, because an answered liveness probe is followed by the registration
# probe, which waits the same deadline for its own answer:
#
#     max(0, F - 1) x (I + T)  +  (I + 2T)
#
# which is 110 seconds at the defaults. For all of that time the component is
# working and has simply not refreshed the file, so anything treating a gap this
# short as a failure is reading a slow modem as a dead one.
#
# This is an exact bound rather than an estimate, and it only became one when the
# probe's own lock acquisition and write were brought inside its deadline. While
# the deadline covered the reply alone there was no arithmetic to write: a modem
# that stops accepting bytes blocks the write under serial flow control with no
# bound and no exception, and a send stuck mid-transaction holds the AT lock the
# probe needs, so the worst gap was unbounded rather than large.
#
# The margin no floor here protects is the one against HEALTH_STALE_SECONDS,
# which the container healthcheck reads: 120 seconds against a worst gap of 110.
# At the default interval and retry count the gap is 90 + 4T, so it passes the
# window once MODEM_PROBE_TIMEOUT exceeds seven and a half seconds - and that
# setting is neither capped nor clamped against the window, so a generous reply
# deadline can fail the healthcheck on a process that is working.
# MODEM_PROBE_INTERVAL is clamped for exactly this reason; the deadline is not.
WATCHDOG_REFRESH_BUDGET = (
    max(0, MODEM_PROBE_FAILURES - 1) * (MODEM_PROBE_INTERVAL + MODEM_PROBE_TIMEOUT)
    + MODEM_PROBE_INTERVAL
    + 2 * MODEM_PROBE_TIMEOUT
)
# The gap the Telegram polling loop can legitimately leave between two reports
# of progress.
#
# It reports one when a poll returns and one after each update it handles, so a
# single gap holds at most one of two things: a poll, bounded by the request
# deadline; or the handling of one update. The slowest thing handling an update
# can do is send a message, which is attempted TELEGRAM_SEND_ATTEMPTS times
# behind that same deadline with a wait between attempts. The two are added
# rather than maximised because two handlers really do both: answering a button
# press and then replying to it reaches this figure exactly.
#
# This has to be in the floor below, not merely in the margin. Before progress
# was tracked per component the Telegram loop's own pace did not matter, because
# the device heartbeat refreshed the shared snapshot every half-minute whatever
# Telegram was doing. Now that the loop is measured on its own, a threshold
# under this figure would restart the process for taking a long poll.
TELEGRAM_PROGRESS_BUDGET = (
    TELEGRAM_REQUEST_TIMEOUT
    + TELEGRAM_SEND_ATTEMPTS * TELEGRAM_REQUEST_TIMEOUT
    + max(0, TELEGRAM_SEND_ATTEMPTS - 1) * TELEGRAM_SEND_RETRY_DELAY
)

# The modem term is doubled for margin, because the probe is not the only thing
# that loop does between reports; its second form covers a configuration that
# has cut the reply deadline and the retry count to almost nothing, where twice
# the budget would come to barely more than a single round.
#
# The Telegram term is not doubled - it is already a whole iteration's worst
# case rather than a per-round figure - but it cannot be used bare either. It is
# a figure two real handlers reach exactly, and the watchdog compares with >=,
# so a threshold equal to it makes the worst legitimate gap a tripping gap. One
# further request deadline is added: the unit that gap is built from, and enough
# for a handler that makes one more call than today's slowest.
WATCHDOG_STALL_FLOOR = max(
    2 * WATCHDOG_REFRESH_BUDGET,
    4 * MODEM_PROBE_INTERVAL,
    TELEGRAM_PROGRESS_BUDGET + TELEGRAM_REQUEST_TIMEOUT,
)

# How long a component loop may go without advancing, or the health snapshot
# without being written, before the watchdog treats the process as stalled. A
# component that blocks without raising never reaches mark_down, so this is the
# only signal that catches it.
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
#
# Handled separately from _clamped above, rather than reused, because both
# bounds here are derived rather than fixed, and the fallback used when the
# operator sets nothing - twice HEALTH_STALE_SECONDS - already sits under
# WATCHDOG_STALL_FLOOR at the defaults this file ships (240 against 310).
# Reporting that plain arithmetic fact on every unmodified start would be
# exactly the noise an operator learns to stop reading, so a notice is only
# recorded for what the operator actually touched: an explicit
# WATCHDOG_STALL_SECONDS that the bounds move away from, or a
# WATCHDOG_DOWN_SECONDS low enough that the floor now exceeds it - which is
# the documented invariant above failing, quietly, because the floor is
# applied last on purpose.
_watchdog_stall_env = os.environ.get("WATCHDOG_STALL_SECONDS")
_watchdog_stall_requested = (
    float(_watchdog_stall_env)
    if _watchdog_stall_env is not None
    else 2 * HEALTH_STALE_SECONDS
)
WATCHDOG_STALL_SECONDS = max(
    WATCHDOG_STALL_FLOOR,
    min(float(WATCHDOG_DOWN_SECONDS), _watchdog_stall_requested),
)

if (
    _watchdog_stall_env is not None
    and WATCHDOG_STALL_SECONDS != _watchdog_stall_requested
):
    _reason = (
        f"floor is {WATCHDOG_STALL_FLOOR:g}"
        if WATCHDOG_STALL_SECONDS == WATCHDOG_STALL_FLOOR
        else f"ceiling is WATCHDOG_DOWN_SECONDS={WATCHDOG_DOWN_SECONDS:g}"
    )
    CLAMP_NOTICES.append(
        f"WATCHDOG_STALL_SECONDS was requested as {_watchdog_stall_requested:g} "
        f"but is running as {WATCHDOG_STALL_SECONDS:g} ({_reason})"
    )
if WATCHDOG_STALL_FLOOR > WATCHDOG_DOWN_SECONDS:
    CLAMP_NOTICES.append(
        f"WATCHDOG_STALL_SECONDS floor ({WATCHDOG_STALL_FLOOR:g}) exceeds "
        f"WATCHDOG_DOWN_SECONDS ({WATCHDOG_DOWN_SECONDS:g}): a stall can now be "
        "tolerated longer than the down timeout it is documented to stay under. "
        "This is intentional - delaying a restart beats restarting a component "
        "that is still working - but the documented invariant no longer holds "
        "for this configuration."
    )
