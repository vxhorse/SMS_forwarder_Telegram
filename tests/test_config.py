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
    not be mistaken for tuning it outside."""
    monkeypatch.setenv("HEALTH_STALE_SECONDS", "120")
    monkeypatch.setenv("SERVICE_STABLE_SECONDS", "90")
    monkeypatch.setenv("WATCHDOG_CHECK_INTERVAL", "15")
    monkeypatch.setenv("NOTIFY_TIMEOUT", "8")
    monkeypatch.setenv("SERIAL_CLOSE_TIMEOUT", "3")
    monkeypatch.setenv("MODEM_PROBE_INTERVAL", "40")
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
    assert reloaded.NOTIFY_TIMEOUT == reloaded.NOTIFY_TIMEOUT_CEILING
    matches = _notices_for(reloaded, "NOTIFY_TIMEOUT")
    assert len(matches) == 1
    assert "600" in matches[0]
    assert f"{reloaded.NOTIFY_TIMEOUT_CEILING:g}" in matches[0]


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
