import os

# Settings whose value the operator asked for was overridden by a floor or a
# ceiling, because a value outside that range would either disable the guard
# the setting exists for or reinstate the exact failure the guard exists to
# prevent. Recorded here rather than logged immediately: this module cannot
# import the logger, since logger.py reads its level from here and importing
# it back would be circular. main.py logs each of these once the logger
# exists.
CLAMP_NOTICES: list = []


def _clamped(
    name: str, requested, low=None, high=None, low_label=None, high_label=None
):
    """Apply a floor and/or a ceiling to an operator-supplied setting.

    Returns the value actually in force. When a bound moves the value away
    from what was requested, appends a notice to CLAMP_NOTICES naming the
    setting, what was asked for, and what is running instead - an operator
    who tunes a setting outside its allowed range gets told, rather than
    left believing it took effect.

    Which bound gets blamed is decided from what was requested, not from
    what came out: if the request was above the ceiling, the ceiling is what
    cut it, otherwise the floor did. That stays correct even where the two
    bounds happen to coincide - a derived ceiling that lands exactly on the
    floor is still the ceiling, and a request above it is still a request
    above the ceiling, not a floor violation.

    low_label / high_label let a derived bound name where it came from,
    instead of the bare number it works out to - e.g. a ceiling built from
    another setting can say so and give that setting's value, which is what
    an operator actually needs to act on the notice. Left unset, the bound's
    own value is reported, which is already correct for a bound that is
    just a fixed number. Like `name`, a label is a literal built at the call
    site out of other settings' names and values - never out of arbitrary
    operator-controlled text - so a notice still cannot be made to carry a
    credential.

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
        if high is not None and requested > high:
            bound = high_label if high_label is not None else f"{high:g}"
            reason = f"ceiling is {bound}"
        else:
            bound = low_label if low_label is not None else f"{low:g}"
            reason = f"floor is {bound}"
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
# Floored at RECONNECT_BACKOFF_MIN: a maximum below the minimum would make
# each retry's backoff shrink instead of grow, the opposite of what an
# exponential backoff is for.
RECONNECT_BACKOFF_MAX = _clamped(
    "RECONNECT_BACKOFF_MAX",
    float(os.getenv("RECONNECT_BACKOFF_MAX", "30.0")),
    low=RECONNECT_BACKOFF_MIN,
    low_label=f"RECONNECT_BACKOFF_MIN={RECONNECT_BACKOFF_MIN:g}",
)
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
# The whole shutdown path's budget, in seconds.
#
# There is nothing in this process to measure it against: the stop grace period
# is chosen by whatever starts the container and is not visible from in here.
# Ten seconds is the shortest one in common use, so anything on the way out that
# could outlast that is something that could outlast the whole stop.
#
# It bounds the path rather than any one deadline on it, because the device
# teardown awaits two of them one after the other - the serial close and the
# outward notification - and two deadlines each inside the budget can still add
# up to more than it.
STOP_BUDGET_SECONDS = 10.0
NOTIFY_TIMEOUT = _clamped(
    "NOTIFY_TIMEOUT",
    float(os.getenv("NOTIFY_TIMEOUT", "5.0")),
    low=1.0,
    high=STOP_BUDGET_SECONDS,
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
# and forces the close through.
#
# Floored because zero would abort every ordinary disconnect before the
# transport had a chance to flush, and an abort throws away whatever was still
# queued. Capped by whatever the notification deadline leaves of the shutdown
# budget, because both are awaited on the same way out: a close alone that
# outlives the grace period turns a clean stop into a kill, and the snapshot
# then keeps claiming this process is healthy until it ages out.
SERIAL_CLOSE_TIMEOUT_FLOOR = 1.0
_serial_close_ceiling = STOP_BUDGET_SECONDS - NOTIFY_TIMEOUT
SERIAL_CLOSE_TIMEOUT = _clamped(
    "SERIAL_CLOSE_TIMEOUT",
    float(os.getenv("SERIAL_CLOSE_TIMEOUT", "5.0")),
    low=SERIAL_CLOSE_TIMEOUT_FLOOR,
    # Where the derived ceiling and the floor collide, max() picks the floor
    # value, and the collision notice below says so. Zero here would abort
    # every disconnect outright, which is a worse answer to a tight budget
    # than overrunning it.
    high=max(SERIAL_CLOSE_TIMEOUT_FLOOR, _serial_close_ceiling),
    high_label=(
        f"STOP_BUDGET_SECONDS={STOP_BUDGET_SECONDS:g} minus "
        f"NOTIFY_TIMEOUT={NOTIFY_TIMEOUT:g}"
    ),
)
if _serial_close_ceiling < SERIAL_CLOSE_TIMEOUT_FLOOR:
    CLAMP_NOTICES.append(
        f"SERIAL_CLOSE_TIMEOUT floor ({SERIAL_CLOSE_TIMEOUT_FLOOR:g}) leaves "
        f"NOTIFY_TIMEOUT ({NOTIFY_TIMEOUT:g}) and the close together above "
        f"STOP_BUDGET_SECONDS ({STOP_BUDGET_SECONDS:g}): a teardown can now "
        "outlast the shortest stop grace period in common use, and a teardown "
        "that does is killed rather than finished."
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
# Consecutive misses that trigger a reconnect. Floored at one: the loop raises
# once the count reaches this figure, so zero behaves exactly as one while
# reading like an off switch, and a number that looks like a switch and is not
# one is what MODEM_REGISTRATION_CHECK exists separately to avoid.
MODEM_PROBE_FAILURES = _clamped(
    "MODEM_PROBE_FAILURES", int(os.getenv("MODEM_PROBE_FAILURES", "3")), low=1
)
# Also governs the AT handshake performed right after the port opens: both ask
# the same question, which is how long the modem gets to answer a probe.
#
# Capped against the staleness window the container healthcheck reads. The
# longest gap this loop can legitimately leave between two refreshes is
# WATCHDOG_REFRESH_BUDGET below, which in closed form is
#
#     F x I + (F + 1) x T
#
# so keeping that inside HEALTH_STALE_SECONDS means
#
#     T <= (HEALTH_STALE_SECONDS - F x I) / (F + 1)
#
# which is seven and a half seconds at the values this file ships. Raising the
# reply deadline is exactly what one does for a modem that answers slowly, and
# past this bound it fails the healthcheck on a process that is working.
# MODEM_PROBE_INTERVAL is clamped for the same reason: both terms make up the
# same closed-form gap, and bounding only one of them would leave the window
# guarantee holding only for settings nobody has tuned.
#
# Floored because a deadline of zero abandons every probe before the modem
# could answer it, which fails the connection on the first round for ever.
MODEM_PROBE_TIMEOUT_FLOOR = 1.0
_probe_timeout_ceiling = (
    HEALTH_STALE_SECONDS - MODEM_PROBE_FAILURES * MODEM_PROBE_INTERVAL
) / (MODEM_PROBE_FAILURES + 1)
MODEM_PROBE_TIMEOUT = _clamped(
    "MODEM_PROBE_TIMEOUT",
    float(os.getenv("MODEM_PROBE_TIMEOUT", "5.0")),
    low=MODEM_PROBE_TIMEOUT_FLOOR,
    high=max(MODEM_PROBE_TIMEOUT_FLOOR, _probe_timeout_ceiling),
    high_label=(
        f"HEALTH_STALE_SECONDS={HEALTH_STALE_SECONDS:g}, "
        f"MODEM_PROBE_INTERVAL={MODEM_PROBE_INTERVAL:g}, "
        f"MODEM_PROBE_FAILURES={MODEM_PROBE_FAILURES:g}"
    ),
)
if _probe_timeout_ceiling < MODEM_PROBE_TIMEOUT_FLOOR:
    CLAMP_NOTICES.append(
        f"MODEM_PROBE_TIMEOUT floor ({MODEM_PROBE_TIMEOUT_FLOOR:g}) leaves the "
        f"worst refresh gap above HEALTH_STALE_SECONDS "
        f"({HEALTH_STALE_SECONDS:g}): {MODEM_PROBE_FAILURES:g} probes "
        f"{MODEM_PROBE_INTERVAL:g}s apart already fill that window, so the "
        "container healthcheck can fail a process that is working. Lower "
        "MODEM_PROBE_INTERVAL or MODEM_PROBE_FAILURES, or raise "
        "HEALTH_STALE_SECONDS."
    )

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
# probe, which waits the same deadline for its own answer. That second deadline
# is counted whether or not MODEM_REGISTRATION_CHECK is on: a threshold derived
# from this figure has to hold for every configuration, and the round that asks
# one question is the shorter of the two, so the longer one is the bound:
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
# The margin this protects is the one against HEALTH_STALE_SECONDS, which the
# container healthcheck reads: 120 seconds against a worst gap of 110 at the
# values this file ships. Both terms it is built from are now bounded so that
# the gap cannot leave that window silently - MODEM_PROBE_INTERVAL against half
# of it, MODEM_PROBE_TIMEOUT against what the interval and the retry count
# leave of it - and where those bounds cannot both hold, a notice above says so.
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
