import json
import os

from healthcheck import check


def _write(path, payload, mtime):
    with open(path, "w") as handle:
        json.dump(payload, handle)
    os.utime(path, (mtime, mtime))


def test_fresh_and_all_up_is_healthy(tmp_path):
    path = str(tmp_path / "healthy")
    _write(path, {"services": {"device": True, "telegram": True}, "rssi": 21}, mtime=1000.0)
    assert check(path, stale_seconds=120, now=1010.0) == 0


def test_stale_file_is_unhealthy(tmp_path):
    path = str(tmp_path / "healthy")
    _write(path, {"services": {"device": True, "telegram": True}, "rssi": 21}, mtime=1000.0)
    assert check(path, stale_seconds=120, now=1200.0) == 1


def test_component_down_is_unhealthy(tmp_path):
    path = str(tmp_path / "healthy")
    _write(path, {"services": {"device": True, "telegram": False}, "rssi": None}, mtime=1000.0)
    assert check(path, stale_seconds=120, now=1010.0) == 1


def test_missing_file_is_unhealthy(tmp_path):
    assert check(str(tmp_path / "absent"), stale_seconds=120, now=1010.0) == 1


def test_corrupt_file_is_unhealthy(tmp_path):
    path = str(tmp_path / "healthy")
    with open(path, "w") as handle:
        handle.write("not json at all")
    os.utime(path, (1000.0, 1000.0))
    assert check(path, stale_seconds=120, now=1010.0) == 1


def test_missing_services_key_is_unhealthy(tmp_path):
    path = str(tmp_path / "healthy")
    _write(path, {"rssi": 21}, mtime=1000.0)
    assert check(path, stale_seconds=120, now=1010.0) == 1


def test_future_mtime_is_still_fresh(tmp_path):
    """A backwards clock step leaves mtime in the future; the file is still new."""
    path = str(tmp_path / "healthy")
    _write(path, {"services": {"device": True, "telegram": True}, "rssi": 21}, mtime=2000.0)
    assert check(path, stale_seconds=120, now=1000.0) == 0


def test_non_object_json_is_unhealthy(tmp_path):
    """Syntactically valid JSON that is not an object (e.g. a bare number)
    must not raise -- .get() only exists on dicts."""
    path = str(tmp_path / "healthy")
    _write(path, 42, mtime=1000.0)
    assert check(path, stale_seconds=120, now=1010.0) == 1
