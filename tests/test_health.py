import json
import os

from module.health import HealthState


class FakeClock:
    """A monotonic clock the test can advance by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make(tmp_path, clock=None):
    clock = clock or FakeClock()
    path = str(tmp_path / "healthy")
    return HealthState(["device", "telegram"], health_file=path, clock=clock), clock, path


def test_everything_starts_down(tmp_path):
    health, _, _ = _make(tmp_path)
    assert health.all_up() is False


def test_watchdog_counts_from_process_start(tmp_path):
    health, clock, _ = _make(tmp_path)
    clock.advance(120.0)
    assert health.down_duration() == 120.0


def test_down_duration_is_zero_once_all_up(tmp_path):
    health, clock, _ = _make(tmp_path)
    clock.advance(10.0)
    health.mark_up("device")
    health.mark_up("telegram")
    assert health.all_up() is True
    assert health.down_duration() == 0.0


def test_down_duration_measures_from_last_down_transition(tmp_path):
    """A redundant mark_up before the down transition must not leak into the
    down-duration clock: only the moment mark_down actually fires matters.

    (snapshot() exposes only up/down booleans, not per-service timestamps, so
    a stale "since" left behind by a non-idempotent mark_up is not observable
    through the public API once mark_down overwrites it -- this test verifies
    the observable property, not mark_up's idempotency in isolation.)
    """
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    clock.advance(50.0)
    health.mark_up("device")
    health.mark_down("device")
    clock.advance(5.0)
    assert health.down_duration() == 5.0


def test_mark_down_is_idempotent(tmp_path):
    """Repeated mark_down calls on a component that stays down must not
    restart the down-duration timer -- this is what lets the watchdog notice
    a component that has been down since before it started polling, even
    though every poll re-reports the same failure.

    telegram is marked up first so it cannot dominate down_duration()'s max
    over device -- otherwise the assertion would hold regardless of whether
    device's own timer was reset, the same trap the corrected
    test_down_duration_measures_from_last_down_transition avoids.
    """
    health, clock, _ = _make(tmp_path)
    health.mark_up("telegram")
    clock.advance(50.0)
    health.mark_down("device")
    clock.advance(5.0)
    health.mark_down("device")
    clock.advance(10.0)
    assert health.down_duration() == 65.0


def test_down_duration_takes_the_longest(tmp_path):
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.mark_down("device")
    clock.advance(30.0)
    health.mark_down("telegram")
    clock.advance(10.0)
    assert health.down_duration() == 40.0


def test_registration_state_appears_in_the_snapshot(tmp_path):
    """Diagnostic only, and the reason it is worth carrying: a modem that
    answers every probe while sitting off the network looks identical to a
    healthy one from outside the process."""
    health, _, _ = _make(tmp_path)
    assert health.snapshot()["registration"] is None
    health.record_registration(1)
    assert health.snapshot()["registration"] == 1


def test_file_written_when_all_up(tmp_path):
    health, _, path = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.record_rssi(21)
    health.record_registration(5)
    health.refresh_file()
    with open(path) as handle:
        data = json.load(handle)
    assert data == {
        "services": {"device": True, "telegram": True},
        "rssi": 21,
        "registration": 5,
    }


def test_file_not_written_while_any_component_is_down(tmp_path):
    health, _, path = _make(tmp_path)
    health.mark_up("device")
    health.refresh_file()
    assert not os.path.exists(path)


def test_clear_file_is_idempotent(tmp_path):
    health, _, path = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.refresh_file()
    health.clear_file()
    health.clear_file()
    assert not os.path.exists(path)


def test_unknown_service_name_is_ignored(tmp_path):
    health, _, _ = _make(tmp_path)
    health.mark_up("nonexistent")
    assert health.all_up() is False


def test_stall_duration_is_none_before_the_first_healthy_moment(tmp_path):
    health, clock, _ = _make(tmp_path)
    clock.advance(9999.0)
    # Never all-up, so the snapshot has never been written. That is a system
    # still waiting for hardware, not a stalled one.
    assert health.stall_duration() is None


def test_stall_duration_starts_after_the_first_successful_refresh(tmp_path):
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.refresh_file()
    clock.advance(30.0)
    assert health.stall_duration() == 30.0


def test_stall_duration_survives_a_component_going_down(tmp_path):
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.refresh_file()
    health.mark_down("device")
    clock.advance(50.0)
    # refresh_file() is now a no-op, so the age keeps growing. That is the
    # point: a component that is down stops the file being refreshed.
    health.refresh_file()
    assert health.stall_duration() == 50.0


def test_a_completed_cycle_resets_the_stall_age(tmp_path):
    """One cycle of both loops: each reports its own progress and one of them
    rewrites the snapshot. Refreshing the file alone is deliberately not enough
    -- either loop refreshes it for both components, so a refresh says nothing
    about the component that stopped."""
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.refresh_file()
    clock.advance(40.0)
    health.record_progress("device")
    health.record_progress("telegram")
    health.refresh_file()
    assert health.stall_duration() == 0.0


def test_a_failed_write_does_not_count_as_a_refresh(tmp_path, monkeypatch):
    health, clock, path = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.refresh_file()
    clock.advance(10.0)

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("module.health.os.replace", explode)
    # Both loops advanced, so nothing but the unwritable file is behind.
    health.record_progress("device")
    health.record_progress("telegram")
    health.refresh_file()
    # The write failed, so the healthcheck still sees the old file. Pretending
    # it was refreshed would hide exactly that.
    assert health.stall_duration() == 10.0


def test_a_component_that_stops_advancing_is_stalled_while_the_other_advances(tmp_path):
    """The failure a single shared stamp cannot see, which is the whole reason
    progress is tracked per component.

    Both loops refresh the snapshot independently, so the Telegram loop keeps
    it fresh on behalf of a device loop that has stopped advancing, and the
    device stays marked up because a blocked loop raises nothing. Measured
    against the shared stamp alone the process looks perfectly healthy while it
    forwards nothing at all.
    """
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.record_progress("device")
    health.record_progress("telegram")
    health.refresh_file()

    # Ten Telegram cycles. The device loop reports nothing in any of them.
    for _ in range(10):
        clock.advance(30.0)
        health.record_progress("telegram")
        health.refresh_file()

    assert health.stall_duration() == 300.0


def test_marking_a_component_up_restamps_its_progress(tmp_path):
    """A component that has just come back has made no progress under this
    session yet, and the age its previous one left behind measures the outage.
    Carrying that age across the recovery would restart the process for having
    reconnected."""
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.record_progress("device")
    health.record_progress("telegram")
    health.refresh_file()

    health.mark_down("device")
    clock.advance(600.0)
    health.record_progress("telegram")
    health.mark_up("device")
    health.refresh_file()

    clock.advance(5.0)
    assert health.stall_duration() == 5.0


def test_a_component_that_is_down_does_not_count_as_stalled(tmp_path):
    """A component that is down is the down-duration criterion's business, with
    the far longer tolerance chosen for it. Counting its idle loop as a stall
    would put a second, much shorter ceiling on a reconnection."""
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.record_progress("device")
    health.record_progress("telegram")

    clock.advance(600.0)  # The device loop has been silent for the whole outage.
    health.record_progress("telegram")
    health.refresh_file()
    health.mark_down("device")

    clock.advance(10.0)
    health.record_progress("telegram")
    assert health.stall_duration() == 10.0


def test_progress_from_an_unregistered_component_is_ignored(tmp_path):
    health, clock, _ = _make(tmp_path)
    health.record_progress("nonexistent")
    assert health.snapshot()["services"] == {"device": False, "telegram": False}


def _recover_after_an_outage(tmp_path):
    """A device that was down for ten minutes, back and marked up again.

    Nothing has refreshed the snapshot in all that time - it is not written at
    all while a component is down - so the file's age at this moment measures
    the outage. The Telegram loop kept running throughout, which is exactly
    what stops the shared reading from meaning anything on its own.
    """
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.record_progress("device")
    health.record_progress("telegram")
    health.refresh_file()

    health.mark_down("device")
    clock.advance(600.0)
    health.record_progress("telegram")
    health.mark_up("device")
    return health, clock


def test_the_snapshot_age_does_not_outlive_the_outage_that_caused_it(tmp_path):
    """Read as it stands, the age left behind by an outage would restart the
    process for having reconnected.

    Discounted where the reading is taken rather than by whoever reads it: the
    moment the system became whole again is known exactly here, while anything
    downstream can only sample it and has to guess when its baseline stops
    applying.
    """
    health, clock = _recover_after_an_outage(tmp_path)
    assert health.stall_duration() == 0.0
    clock.advance(5.0)
    assert health.stall_duration() == 5.0


def test_a_snapshot_that_stays_unwritten_after_a_recovery_is_still_caught(tmp_path):
    """Discounting the outage delays detection by one threshold measured from
    the recovery. It must not switch it off: a snapshot that is never written
    again is exactly what this reading is for.

    Both loops report progress throughout, so nothing but the file itself is
    behind and the reading can only have come from it.
    """
    health, clock = _recover_after_an_outage(tmp_path)
    for _ in range(10):
        clock.advance(25.0)
        health.record_progress("device")
        health.record_progress("telegram")
    assert health.stall_duration() == 250.0


def test_an_old_outage_does_not_shorten_a_later_stall(tmp_path):
    """The discount lasts until the reading no longer contains the outage, and
    no longer. Subtracting the outage from a stall that starts afterwards would
    leave a blind spot as long as the outage was."""
    health, clock = _recover_after_an_outage(tmp_path)
    clock.advance(20.0)
    health.record_progress("device")
    health.record_progress("telegram")
    health.refresh_file()

    clock.advance(240.0)
    assert health.stall_duration() == 240.0


def test_a_snapshot_that_was_never_written_does_not_hide_a_stall(tmp_path, monkeypatch):
    """A file that cannot be written at all has no timestamp to age, and the
    absence of one used to mean the same thing as never having been healthy.
    Measured from the moment the system became whole, the two are told apart:
    one is a process still waiting for hardware, the other is a process that
    cannot publish its health and whose healthcheck will never pass again."""
    health, clock, _ = _make(tmp_path)

    def explode(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("module.health.os.replace", explode)
    health.mark_up("device")
    health.mark_up("telegram")
    for _ in range(10):
        clock.advance(30.0)
        health.record_progress("device")
        health.record_progress("telegram")
        health.refresh_file()
    assert health.stall_duration() == 300.0


def test_a_progressing_component_does_not_start_the_stall_clock_on_its_own(tmp_path):
    """Cold boot with the modem absent. The Telegram loop is up and advancing,
    the device has never appeared, and stall detection must stay switched off
    for as long as that lasts -- reporting a stall here would be the startup
    deadline this project exists to remove."""
    health, clock, _ = _make(tmp_path)
    health.mark_up("telegram")
    for _ in range(200):
        clock.advance(52.0)
        health.record_progress("telegram")
        health.refresh_file()
    assert health.stall_duration() is None
