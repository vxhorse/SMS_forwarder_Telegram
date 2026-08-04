"""Tests for configuration clamps and the notices they leave behind.

config.py cannot import the logger - logger.py reads its level from config,
so importing the logger back would be circular - so a setting whose value
was overridden by a floor or a ceiling is recorded in CLAMP_NOTICES instead
of being logged directly. tests/test_main.py covers the other half of this:
that main.py actually logs whatever ends up in that list once the logger
exists.

This file covers config.py's half: that every floor and ceiling actually
gets recorded, that a legitimate configuration - including the one nothing
overrides at all - stays silent, and that no notice can carry a credential.

Reloading config mutates a module every other test in the suite relies on,
so every test here restores it in a fixture teardown - the same pattern
already used by test_the_notify_deadline_is_clamped_at_both_ends in
tests/test_main.py.
"""

import importlib

import pytest

import config as config_module


@pytest.fixture(autouse=True)
def _restore_config():
    """Undo whatever env-driven reload a test performs, so later tests see
    the real defaults regardless of what this one asked for."""
    yield
    importlib.reload(config_module)


def _notices_for(reloaded, name):
    return [notice for notice in reloaded.CLAMP_NOTICES if notice.startswith(name)]


# --- Silence when nothing needs saying --------------------------------------


def test_stock_configuration_has_no_notices():
    """Nothing set, nothing clamped, nothing to say. An operator who sees a
    notice on every unmodified start learns to stop reading them."""
    reloaded = importlib.reload(config_module)
    assert reloaded.CLAMP_NOTICES == []


def test_a_fully_specified_but_legal_configuration_has_no_notices(monkeypatch):
    """Tuning every clamped setting to a value inside its allowed range must
    not be mistaken for tuning it outside.

    The values here have to satisfy the derived bounds as well as the fixed
    ones: the two deadlines on the shutdown path have to fit inside
    STOP_BUDGET_SECONDS together, and the probe schedule has to leave the worst
    refresh gap inside HEALTH_STALE_SECONDS.
    """
    monkeypatch.setenv("HEALTH_STALE_SECONDS", "120")
    monkeypatch.setenv("SERVICE_STABLE_SECONDS", "90")
    monkeypatch.setenv("WATCHDOG_CHECK_INTERVAL", "15")
    monkeypatch.setenv("NOTIFY_TIMEOUT", "8")
    monkeypatch.setenv("SERIAL_CLOSE_TIMEOUT", "2")
    monkeypatch.setenv("MODEM_PROBE_INTERVAL", "25")
    monkeypatch.setenv("MODEM_REGISTRATION_FAILURES", "4")
    monkeypatch.setenv("WATCHDOG_DOWN_SECONDS", "3600")
    monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "1200")
    reloaded = importlib.reload(config_module)
    assert reloaded.CLAMP_NOTICES == []


# --- The floors -------------------------------------------------------------


@pytest.mark.parametrize(
    "env_name, low_value, floor",
    [
        ("HEALTH_STALE_SECONDS", "0", 2),
        ("SERVICE_STABLE_SECONDS", "0", 5.0),
        ("WATCHDOG_CHECK_INTERVAL", "0", 1.0),
        ("NOTIFY_TIMEOUT", "0", 1.0),
        ("SERIAL_CLOSE_TIMEOUT", "0", 1.0),
        ("MODEM_PROBE_INTERVAL", "0", 1.0),
        ("MODEM_REGISTRATION_FAILURES", "0", 2),
        ("MODEM_PROBE_TIMEOUT", "0", 1.0),
        ("MODEM_PROBE_FAILURES", "0", 1),
    ],
)
def test_a_floored_setting_is_reported(monkeypatch, env_name, low_value, floor):
    monkeypatch.setenv(env_name, low_value)
    reloaded = importlib.reload(config_module)
    matches = _notices_for(reloaded, env_name)
    assert len(matches) == 1, reloaded.CLAMP_NOTICES
    notice = matches[0]
    assert low_value in notice
    assert f"{floor:g}" in notice
    assert getattr(reloaded, env_name) == floor


# --- The ceilings ------------------------------------------------------------


def test_notify_timeout_ceiling_is_reported(monkeypatch):
    monkeypatch.setenv("NOTIFY_TIMEOUT", "600")
    reloaded = importlib.reload(config_module)
    assert reloaded.NOTIFY_TIMEOUT == reloaded.STOP_BUDGET_SECONDS
    matches = _notices_for(reloaded, "NOTIFY_TIMEOUT")
    assert len(matches) == 1
    assert "600" in matches[0]
    assert f"{reloaded.STOP_BUDGET_SECONDS:g}" in matches[0]


def test_modem_probe_interval_ceiling_tracks_health_stale_seconds(monkeypatch):
    """The ceiling here is half of HEALTH_STALE_SECONDS, not a fixed number,
    so shrinking that window is enough to trip it without touching
    MODEM_PROBE_INTERVAL's own setting at all."""
    monkeypatch.setenv("HEALTH_STALE_SECONDS", "20")
    monkeypatch.setenv("MODEM_PROBE_INTERVAL", "100")
    reloaded = importlib.reload(config_module)
    assert reloaded.MODEM_PROBE_INTERVAL == reloaded.HEALTH_STALE_SECONDS / 2
    matches = _notices_for(reloaded, "MODEM_PROBE_INTERVAL")
    assert len(matches) == 1
    assert "100" in matches[0]


def test_the_shutdown_path_fits_inside_one_stop_grace_period(monkeypatch):
    """NOTIFY_TIMEOUT and SERIAL_CLOSE_TIMEOUT are both awaited on the way out,
    one after the other. Bounding them separately lets their sum outlast the
    grace period either of them was bounded against, and a teardown that
    outlives the grace period is killed rather than finished - so the process
    never reaches the point where it stops claiming to be healthy."""
    monkeypatch.setenv("NOTIFY_TIMEOUT", "6")
    monkeypatch.setenv("SERIAL_CLOSE_TIMEOUT", "60")
    reloaded = importlib.reload(config_module)
    assert (
        reloaded.NOTIFY_TIMEOUT + reloaded.SERIAL_CLOSE_TIMEOUT
        <= reloaded.STOP_BUDGET_SECONDS
    )
    matches = _notices_for(reloaded, "SERIAL_CLOSE_TIMEOUT")
    assert len(matches) == 1, reloaded.CLAMP_NOTICES
    assert "60" in matches[0]


def test_a_notify_deadline_that_eats_the_whole_budget_is_reported(monkeypatch):
    """With nothing left for the close, the floor wins - aborting every
    disconnect before the transport could flush would be worse than overrunning
    the budget. The invariant no longer holds for that configuration, and
    saying so is the only thing that keeps it from being a silent one.

    SERIAL_CLOSE_TIMEOUT is pinned at its own floor here rather than left at
    the default: requested then equals applied, so _clamped's own notice
    never fires, and the only thing left that can catch the broken budget is
    the dedicated collision notice this test is named after."""
    monkeypatch.setenv("NOTIFY_TIMEOUT", "10")
    monkeypatch.setenv("SERIAL_CLOSE_TIMEOUT", "1")
    reloaded = importlib.reload(config_module)
    assert reloaded.SERIAL_CLOSE_TIMEOUT == reloaded.SERIAL_CLOSE_TIMEOUT_FLOOR
    matches = _notices_for(reloaded, "SERIAL_CLOSE_TIMEOUT")
    assert len(matches) == 1, reloaded.CLAMP_NOTICES
    assert "NOTIFY_TIMEOUT" in matches[0]
    assert "STOP_BUDGET_SECONDS" in matches[0]


def test_the_probe_deadline_is_capped_against_the_staleness_window(monkeypatch):
    """The worst legitimate gap between two refreshes is F*I + (F+1)*T, and
    raising T is exactly what one does for a slow modem. Past the window the
    healthcheck fails a process that is working perfectly."""
    monkeypatch.setenv("MODEM_PROBE_TIMEOUT", "30")
    reloaded = importlib.reload(config_module)
    assert reloaded.WATCHDOG_REFRESH_BUDGET <= reloaded.HEALTH_STALE_SECONDS
    matches = _notices_for(reloaded, "MODEM_PROBE_TIMEOUT")
    assert len(matches) == 1, reloaded.CLAMP_NOTICES
    assert "30" in matches[0]
    # The documented figure at the shipped defaults.
    assert reloaded.MODEM_PROBE_TIMEOUT == 7.5


def test_a_probe_schedule_with_no_room_left_for_a_deadline_is_reported(monkeypatch):
    """F * I alone can fill the window, leaving no positive T at all. The floor
    wins, because a deadline of zero abandons every probe before it is
    attempted, and the notice says the window no longer holds.

    MODEM_PROBE_TIMEOUT is pinned at its own floor here rather than left at
    the default: requested then equals applied, so _clamped's own notice
    never fires, and the only thing left that can catch the broken window is
    the dedicated collision notice this test is named after."""
    monkeypatch.setenv("MODEM_PROBE_INTERVAL", "40")
    monkeypatch.setenv("MODEM_PROBE_TIMEOUT", "1")
    reloaded = importlib.reload(config_module)
    assert reloaded.MODEM_PROBE_TIMEOUT == reloaded.MODEM_PROBE_TIMEOUT_FLOOR
    matches = _notices_for(reloaded, "MODEM_PROBE_TIMEOUT")
    assert len(matches) == 1, reloaded.CLAMP_NOTICES
    assert "worst refresh gap" in matches[0]
    assert "HEALTH_STALE_SECONDS" in matches[0]


# --- A clamp notice names the setting a derived bound came from, not just --
# --- the number it works out to ----------------------------------------------


def test_a_ceiling_that_coincides_with_the_floor_still_blames_the_ceiling(
    monkeypatch,
):
    """NOTIFY_TIMEOUT=9 puts SERIAL_CLOSE_TIMEOUT's derived ceiling exactly on
    its fixed floor (10 - 9 == 1). The invariant still holds at 9 + 1 == 10, so
    there is no floor/ceiling collision to report - but the operator asked for
    more than the ceiling allowed, and the notice has to blame the ceiling
    (and name NOTIFY_TIMEOUT as where it came from), not the floor that only
    happens to sit at the same value."""
    monkeypatch.setenv("NOTIFY_TIMEOUT", "9")
    monkeypatch.setenv("SERIAL_CLOSE_TIMEOUT", "9")
    reloaded = importlib.reload(config_module)
    assert reloaded.SERIAL_CLOSE_TIMEOUT == 1.0
    matches = _notices_for(reloaded, "SERIAL_CLOSE_TIMEOUT")
    assert len(matches) == 1, reloaded.CLAMP_NOTICES
    assert "ceiling" in matches[0]
    assert "NOTIFY_TIMEOUT=9" in matches[0]


def test_the_plain_probe_ceiling_notice_names_its_origin(monkeypatch):
    """The common case - an operator raising MODEM_PROBE_TIMEOUT for a slow
    modem, with no floor collision anywhere in sight - must still say where
    the resulting ceiling comes from. A bare 'ceiling is 7.5' gives the
    operator nothing to act on."""
    monkeypatch.setenv("MODEM_PROBE_TIMEOUT", "15")
    reloaded = importlib.reload(config_module)
    assert reloaded.MODEM_PROBE_TIMEOUT == 7.5
    matches = _notices_for(reloaded, "MODEM_PROBE_TIMEOUT")
    assert len(matches) == 1, reloaded.CLAMP_NOTICES
    assert "HEALTH_STALE_SECONDS" in matches[0]


def test_the_backoff_floor_notice_names_reconnect_backoff_min(monkeypatch):
    """RECONNECT_BACKOFF_MAX's floor is RECONNECT_BACKOFF_MIN, not a fixed
    number - the notice has to say so, or an operator who set a ceiling below
    the floor has nothing telling them which other setting to look at."""
    monkeypatch.setenv("RECONNECT_BACKOFF_MIN", "10")
    monkeypatch.setenv("RECONNECT_BACKOFF_MAX", "5")
    reloaded = importlib.reload(config_module)
    assert reloaded.RECONNECT_BACKOFF_MAX == reloaded.RECONNECT_BACKOFF_MIN
    matches = _notices_for(reloaded, "RECONNECT_BACKOFF_MAX")
    assert len(matches) == 1, reloaded.CLAMP_NOTICES
    assert "RECONNECT_BACKOFF_MIN=10" in matches[0]


# --- WATCHDOG_STALL_SECONDS: two derived bounds instead of two fixed ones ----


def test_watchdog_stall_seconds_floor_overrides_a_low_operator_value(monkeypatch):
    """The concrete regression: an operator setting WATCHDOG_STALL_SECONDS
    anywhere under the derived floor used to see no effect and no
    explanation."""
    monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "50")
    reloaded = importlib.reload(config_module)
    assert reloaded.WATCHDOG_STALL_SECONDS == reloaded.WATCHDOG_STALL_FLOOR
    matches = _notices_for(reloaded, "WATCHDOG_STALL_SECONDS")
    assert len(matches) == 1
    assert "50" in matches[0]
    assert f"{reloaded.WATCHDOG_STALL_FLOOR:g}" in matches[0]


def test_watchdog_stall_seconds_ceiling_is_reported(monkeypatch):
    monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "999999")
    reloaded = importlib.reload(config_module)
    assert reloaded.WATCHDOG_STALL_SECONDS == float(reloaded.WATCHDOG_DOWN_SECONDS)
    matches = _notices_for(reloaded, "WATCHDOG_STALL_SECONDS")
    assert len(matches) == 1
    assert "999999" in matches[0]


def test_a_legitimate_stall_override_is_not_reported(monkeypatch):
    monkeypatch.setenv("WATCHDOG_STALL_SECONDS", "1000")
    reloaded = importlib.reload(config_module)
    assert reloaded.WATCHDOG_STALL_SECONDS == 1000.0
    assert reloaded.CLAMP_NOTICES == []


def test_watchdog_down_seconds_below_the_stall_floor_is_reported(monkeypatch):
    """config.py documents that a stall must not outlive WATCHDOG_DOWN_SECONDS,
    but the floor is applied last and wins on purpose - delaying a restart
    beats restarting a component that is still working. That precedence is
    correct, but silently breaking the documented invariant is not: this
    must show up even though WATCHDOG_STALL_SECONDS's own setting was never
    touched.
    """
    monkeypatch.setenv("WATCHDOG_DOWN_SECONDS", "100")
    reloaded = importlib.reload(config_module)
    assert reloaded.WATCHDOG_STALL_FLOOR > reloaded.WATCHDOG_DOWN_SECONDS
    assert reloaded.WATCHDOG_STALL_SECONDS == reloaded.WATCHDOG_STALL_FLOOR
    assert any(
        "WATCHDOG_DOWN_SECONDS" in notice and "WATCHDOG_STALL" in notice
        for notice in reloaded.CLAMP_NOTICES
    )


def test_watchdog_down_seconds_comfortably_above_the_floor_is_silent(monkeypatch):
    monkeypatch.setenv("WATCHDOG_DOWN_SECONDS", "10000")
    reloaded = importlib.reload(config_module)
    assert reloaded.CLAMP_NOTICES == []


def test_a_down_tolerance_under_the_first_recovery_is_reported(monkeypatch):
    """Every clock in this file measures from a recovery, and a component
    cannot reach its first one until it has connected and then held for
    SERVICE_STABLE_SECONDS. Until then it is marked down, and has been since
    the process started.

    So a down tolerance under that window plus one inspection interval exits
    before any component could possibly be marked up: a permanent restart loop
    on hardware that is working, reported by a line about a component being
    down for an hour's worth of reasons that do not apply. Nothing can clamp
    this - the connect time is a property of the hardware and unknowable from
    here - so the part that is knowable is reported instead.
    """
    monkeypatch.setenv("WATCHDOG_DOWN_SECONDS", "60")
    reloaded = importlib.reload(config_module)
    matches = [
        notice
        for notice in reloaded.CLAMP_NOTICES
        if "SERVICE_STABLE_SECONDS" in notice and "WATCHDOG_DOWN_SECONDS" in notice
    ]
    assert len(matches) == 1, reloaded.CLAMP_NOTICES
    assert "WATCHDOG_CHECK_INTERVAL" in matches[0]


def test_a_down_tolerance_that_clears_the_first_recovery_is_silent(monkeypatch):
    """The bound is only the part that can be computed, so it has to stay quiet
    for a configuration that satisfies it - including one tuned right up to the
    edge, or every operator who shortens the tolerance deliberately learns to
    ignore the line that matters."""
    monkeypatch.setenv("WATCHDOG_DOWN_SECONDS", "3000")
    monkeypatch.setenv("SERVICE_STABLE_SECONDS", "120")
    monkeypatch.setenv("WATCHDOG_CHECK_INTERVAL", "10")
    reloaded = importlib.reload(config_module)
    assert reloaded.CLAMP_NOTICES == []


# --- WATCHDOG_CHURN_*: a fixed floor, and a window with a derived one --------


def test_the_churn_threshold_has_a_floor(monkeypatch):
    """One reconnect is not a pattern. At zero or one, a single transient
    failure ends the process."""
    monkeypatch.setenv("WATCHDOG_CHURN_SESSIONS", "1")
    reloaded = importlib.reload(config_module)
    assert reloaded.WATCHDOG_CHURN_SESSIONS == 2
    assert len(_notices_for(reloaded, "WATCHDOG_CHURN_SESSIONS")) == 1


def test_the_churn_window_must_hold_that_many_worst_case_cycles(monkeypatch):
    """A window shorter than the threshold's worth of slowest cycles can never
    reach the threshold, so the criterion would simply never fire."""
    monkeypatch.setenv("WATCHDOG_CHURN_WINDOW", "60")
    reloaded = importlib.reload(config_module)
    assert reloaded.WATCHDOG_CHURN_WINDOW == reloaded.WATCHDOG_CHURN_WINDOW_FLOOR
    matches = _notices_for(reloaded, "WATCHDOG_CHURN_WINDOW")
    assert len(matches) == 1
    assert "60" in matches[0]


@pytest.mark.parametrize("failures", ["2", "3", "8"])
def test_the_churn_window_floor_holds_a_registration_driven_cycle(
    monkeypatch, failures
):
    """The registration check ends a session after a run of readings, not after
    one, so the slowest cycle the window has to hold is multiplied by
    MODEM_REGISTRATION_FAILURES - a setting that appears in no other bound.

    Left out of the floor, a patient count makes the window too short to hold
    the threshold's worth of cycles, the count can never reach the threshold,
    and the criterion is switched off while the settings table still says it is
    on. That is a false negative, which for a criterion whose whole purpose is
    seeing what nothing else sees is the dangerous direction.
    """
    monkeypatch.setenv("MODEM_REGISTRATION_CHECK", "1")
    monkeypatch.setenv("MODEM_REGISTRATION_FAILURES", failures)
    reloaded = importlib.reload(config_module)
    cycle = reloaded.RECONNECT_BACKOFF_MAX + _worst_session_seconds(reloaded)
    assert (
        reloaded.WATCHDOG_CHURN_WINDOW >= reloaded.WATCHDOG_CHURN_SESSIONS * cycle
    ), reloaded.WATCHDOG_CHURN_WINDOW_FLOOR


def test_the_churn_window_floor_ignores_the_registration_count_when_off(monkeypatch):
    """The multiplier belongs to the check, not to the count. With the check
    off the count decides nothing at all, and a floor that grew with it anyway
    would widen the window - and with it the criterion's appetite - for a
    setting that is not in force."""
    monkeypatch.setenv("MODEM_REGISTRATION_FAILURES", "8")
    reloaded = importlib.reload(config_module)
    assert reloaded.WATCHDOG_CHURN_WINDOW_FLOOR == 1400.0
    assert reloaded.CLAMP_NOTICES == []


def test_the_shipped_churn_window_clears_its_floor_without_a_notice():
    """The default has to sit above the derived floor on its own, or every
    unmodified start prints a notice and an operator learns to stop reading
    them."""
    reloaded = importlib.reload(config_module)
    assert reloaded.WATCHDOG_CHURN_WINDOW > reloaded.WATCHDOG_CHURN_WINDOW_FLOOR
    assert reloaded.CLAMP_NOTICES == []


# --- The meta-rule: enforced or reported, never neither ----------------------


def _worst_session_seconds(cfg):
    """The longest a connected session can take to end in a failure.

    Derived here from the shape of module/device_manager.py's heartbeat_loop
    rather than from config.py's own figure: a derivation checked against
    itself would pass however wrong it was, and the whole point of the floor
    below is that it models the loop it has to hold.

    One round costs the interval plus what the probes in it wait for. A round
    whose liveness probe goes unanswered spends one deadline and starts again;
    a round that is answered goes on to ask about registration, which waits the
    same deadline for its own answer.
    """
    unanswered = cfg.MODEM_PROBE_INTERVAL + cfg.MODEM_PROBE_TIMEOUT
    answered = cfg.MODEM_PROBE_INTERVAL + 2 * cfg.MODEM_PROBE_TIMEOUT
    # The liveness path: consecutive unanswered rounds until the count is
    # reached, which is the whole session.
    liveness = cfg.MODEM_PROBE_FAILURES * unanswered
    if not cfg.MODEM_REGISTRATION_CHECK:
        return liveness
    # The registration path: only an answered round can find the modem off the
    # network, so it takes that many answered rounds - and one answered probe
    # clears the liveness count, so up to MODEM_PROBE_FAILURES - 1 unanswered
    # rounds fit in front of each of them without ending the session first.
    registration = cfg.MODEM_REGISTRATION_FAILURES * (
        max(0, cfg.MODEM_PROBE_FAILURES - 1) * unanswered + answered
    )
    return max(liveness, registration)


def _invariants(cfg):
    """Every relationship between two operator-settable settings that has to
    hold, as (name, holds, the names a single notice about it must mention
    together).

    A relationship that is only true by accident of the defaults is one that
    will be broken by the first operator who tunes either side of it. Each one
    here is either kept true by a clamp or, where two bounds genuinely
    conflict, reported - and this is the list that says which relationships
    there are among the settings an operator can actually reach from the
    environment, so a new one of those cannot quietly join without one.

    This is not the list of every relationship between two settings in this
    file. TELEGRAM_LONG_POLL_SECONDS < TELEGRAM_REQUEST_TIMEOUT, for instance,
    is just as real, but config.py deliberately hardcodes both (see the
    comment there for why) - with no operator-settable value on either side,
    there is nothing for a clamp to guard or a notice to report.
    """
    return [
        (
            "the shutdown path fits inside one stop grace period",
            cfg.NOTIFY_TIMEOUT + cfg.SERIAL_CLOSE_TIMEOUT <= cfg.STOP_BUDGET_SECONDS,
            ("SERIAL_CLOSE_TIMEOUT",),
        ),
        (
            "the worst refresh gap stays inside the staleness window",
            cfg.WATCHDOG_REFRESH_BUDGET <= cfg.HEALTH_STALE_SECONDS,
            ("MODEM_PROBE_TIMEOUT", "HEALTH_STALE_SECONDS"),
        ),
        (
            "the probe cannot refresh more slowly than half the window",
            cfg.MODEM_PROBE_INTERVAL <= cfg.HEALTH_STALE_SECONDS / 2,
            ("MODEM_PROBE_INTERVAL",),
        ),
        (
            "the first recovery can be reached before the down clock runs out",
            cfg.SERVICE_STABLE_SECONDS + cfg.WATCHDOG_CHECK_INTERVAL
            < float(cfg.WATCHDOG_DOWN_SECONDS),
            ("SERVICE_STABLE_SECONDS", "WATCHDOG_DOWN_SECONDS"),
        ),
        (
            "a stall is not tolerated longer than an outright loss",
            cfg.WATCHDOG_STALL_SECONDS <= float(cfg.WATCHDOG_DOWN_SECONDS),
            ("WATCHDOG_STALL", "WATCHDOG_DOWN_SECONDS"),
        ),
        (
            "the stall threshold clears the longest legitimate gap",
            cfg.WATCHDOG_STALL_SECONDS >= cfg.WATCHDOG_STALL_FLOOR,
            ("WATCHDOG_STALL",),
        ),
        (
            "the backoff ceiling never sits below its own floor",
            cfg.RECONNECT_BACKOFF_MAX >= cfg.RECONNECT_BACKOFF_MIN,
            ("RECONNECT_BACKOFF_MAX", "RECONNECT_BACKOFF_MIN"),
        ),
        (
            "the churn window can hold the threshold's worth of worst cycles",
            cfg.WATCHDOG_CHURN_WINDOW
            >= cfg.WATCHDOG_CHURN_SESSIONS
            * (cfg.RECONNECT_BACKOFF_MAX + _worst_session_seconds(cfg)),
            ("WATCHDOG_CHURN_WINDOW",),
        ),
    ]


# Values an operator might plausibly reach for, including several that break
# one invariant or another. Every one of them has to end up either satisfied
# or explained.
_TUNINGS = [
    {},
    {"NOTIFY_TIMEOUT": "10"},
    {"NOTIFY_TIMEOUT": "9", "SERIAL_CLOSE_TIMEOUT": "9"},
    {"SERIAL_CLOSE_TIMEOUT": "60"},
    {"MODEM_PROBE_TIMEOUT": "30"},
    {"MODEM_PROBE_INTERVAL": "40"},
    {"MODEM_PROBE_INTERVAL": "55", "MODEM_PROBE_FAILURES": "5"},
    {"HEALTH_STALE_SECONDS": "10"},
    {"HEALTH_STALE_SECONDS": "600", "MODEM_PROBE_TIMEOUT": "60"},
    {"WATCHDOG_DOWN_SECONDS": "100"},
    # Short enough that the watchdog would exit before any component could
    # reach its first recovery - a permanent restart loop on hardware that is
    # working perfectly.
    {"WATCHDOG_DOWN_SECONDS": "60"},
    {"WATCHDOG_STALL_SECONDS": "30"},
    {"MODEM_PROBE_FAILURES": "0", "MODEM_PROBE_TIMEOUT": "0"},
    {"RECONNECT_BACKOFF_MIN": "10", "RECONNECT_BACKOFF_MAX": "5"},
    # Requesting the probe deadline at exactly its own floor leaves _clamped
    # silent - applied equals requested, so there is nothing for it to
    # report - even though the schedule this pairs with still overflows the
    # staleness window. Only the dedicated collision notice below catches
    # this one; nothing else in CLAMP_NOTICES mentions the setting at all.
    {"MODEM_PROBE_INTERVAL": "40", "MODEM_PROBE_TIMEOUT": "1"},
    # The same shape on the shutdown path: requesting the close deadline at
    # exactly its own floor leaves _clamped silent there too, even though
    # NOTIFY_TIMEOUT alone already consumes the whole budget. Only the
    # dedicated collision notice below catches this one.
    {"NOTIFY_TIMEOUT": "10", "SERIAL_CLOSE_TIMEOUT": "1"},
    # The churn window's floor is derived from three other settings, so it
    # moves without being touched: a longer backoff or a slower probe schedule
    # widens the slowest cycle it has to hold, and raising the session count
    # multiplies it.
    {"WATCHDOG_CHURN_WINDOW": "60"},
    {"WATCHDOG_CHURN_SESSIONS": "20", "RECONNECT_BACKOFF_MAX": "300"},
    # The registration check makes a session end after a run of readings rather
    # than after one, so it multiplies the slowest cycle the window has to
    # hold - and the count is a setting of its own that appears in no other
    # bound. The first of these is the shipped count with the check switched
    # on; the second is an operator asking the check for more patience.
    {"MODEM_REGISTRATION_CHECK": "1"},
    {"MODEM_REGISTRATION_CHECK": "1", "MODEM_REGISTRATION_FAILURES": "8"},
]


@pytest.mark.parametrize("tuning", _TUNINGS, ids=lambda t: "+".join(t) or "stock")
def test_every_invariant_is_either_enforced_or_reported(monkeypatch, tuning):
    for name, value in tuning.items():
        monkeypatch.setenv(name, value)
    reloaded = importlib.reload(config_module)
    for description, holds, mentions in _invariants(reloaded):
        if holds:
            continue
        assert any(
            all(word in notice for word in mentions)
            for notice in reloaded.CLAMP_NOTICES
        ), (
            f"{description!r} does not hold and no single notice says so: "
            f"{reloaded.CLAMP_NOTICES}"
        )


# --- Credentials can never ride along ---------------------------------------


def test_no_notice_can_reference_the_bot_token_or_chat_id(monkeypatch):
    """BOT_TOKEN and CHAT_ID are credentials. Nothing about the clamp
    mechanism should be able to carry either regardless of what else in the
    file gets clamped at the same time."""
    monkeypatch.setenv("BOT_TOKEN", "sentinel-bot-token-value")
    monkeypatch.setenv("CHAT_ID", "sentinel-chat-id-value")
    monkeypatch.setenv("SERVICE_STABLE_SECONDS", "0")
    reloaded = importlib.reload(config_module)
    joined = " ".join(reloaded.CLAMP_NOTICES)
    assert "sentinel-bot-token-value" not in joined
    assert "sentinel-chat-id-value" not in joined
    assert reloaded.CLAMP_NOTICES  # sanity: the other clamp still fired
