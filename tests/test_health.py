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


def test_file_written_when_all_up(tmp_path):
    health, _, path = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.record_rssi(21)
    health.refresh_file()
    with open(path) as handle:
        data = json.load(handle)
    assert data == {"services": {"device": True, "telegram": True}, "rssi": 21}


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


def test_a_successful_refresh_resets_the_stall_age(tmp_path):
    health, clock, _ = _make(tmp_path)
    health.mark_up("device")
    health.mark_up("telegram")
    health.refresh_file()
    clock.advance(40.0)
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
    health.refresh_file()
    # The write failed, so the healthcheck still sees the old file. Pretending
    # it was refreshed would hide exactly that.
    assert health.stall_duration() == 10.0
