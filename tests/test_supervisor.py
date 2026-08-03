import pytest

from module.supervisor import Backoff, FatalConfigError


def _no_jitter():
    """An rng returning 0.5 makes the jitter factor exactly 1.0."""
    return 0.5


def test_backoff_doubles_and_caps():
    backoff = Backoff(minimum=1.0, maximum=30.0, rng=_no_jitter)
    delays = [backoff.next_delay() for _ in range(7)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_backoff_reset_returns_to_minimum():
    backoff = Backoff(minimum=1.0, maximum=30.0, rng=_no_jitter)
    backoff.next_delay()
    backoff.next_delay()
    backoff.reset()
    assert backoff.next_delay() == 1.0


def test_backoff_jitter_stays_within_bounds():
    low = Backoff(minimum=10.0, maximum=10.0, jitter=0.2, rng=lambda: 0.0)
    high = Backoff(minimum=10.0, maximum=10.0, jitter=0.2, rng=lambda: 1.0)
    assert low.next_delay() == pytest.approx(8.0)
    assert high.next_delay() == pytest.approx(12.0)


def test_fatal_config_error_is_an_exception():
    assert issubclass(FatalConfigError, Exception)
