"""The linear-time helper: a single noisy measurement doesn't fail a linear scan, and a quadratic one still fails."""
from tests import linear_time


def _fake_per_call(monkeypatch, costs):
    """per_call returns these (big, little) cost pairs in turn, one pair per attempt."""
    readings = iter([cost for pair in costs for cost in pair])
    monkeypatch.setattr(linear_time, 'per_call', lambda call: next(readings))


def test_one_noisy_reading_of_a_linear_scan_does_not_fail_it(monkeypatch):
    _fake_per_call(monkeypatch, [(1.0, 0.1), (0.4, 0.1)])   # a ratio of 10 once, then 4
    assert linear_time.grows_linearly(lambda s: None, lambda n: '')


def test_a_quadratic_scan_fails_on_every_attempt(monkeypatch):
    _fake_per_call(monkeypatch, [(1.6, 0.1)] * linear_time.ATTEMPTS)   # a ratio of 16 every time
    assert not linear_time.grows_linearly(lambda s: None, lambda n: '')
