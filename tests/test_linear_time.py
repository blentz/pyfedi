"""The linear-time helper: a single noisy measurement doesn't fail a linear scan, and a quadratic one still fails."""
from tests import linear_time


def _fake_per_call(monkeypatch, costs):
    """per_call returns these (big, little) cost pairs in turn, one pair per attempt."""
    readings = iter([cost for pair in costs for cost in pair])
    monkeypatch.setattr(linear_time, 'per_call', lambda call: next(readings))


def test_one_noisy_reading_of_a_linear_scan_does_not_fail_it(monkeypatch):
    _fake_per_call(monkeypatch, [(3.0, 0.1), (0.8, 0.1)])   # a ratio of 30 once (over the bound of 24), then 8
    assert linear_time.grows_linearly(lambda s: None, lambda n: '')


def test_a_quadratic_scan_fails_on_every_attempt(monkeypatch):
    _fake_per_call(monkeypatch, [(6.4, 0.1)] * linear_time.ATTEMPTS)   # a ratio of 64, factor 8 squared, every time
    assert not linear_time.grows_linearly(lambda s: None, lambda n: '')


def test_a_quadratic_scan_is_well_over_the_bound(monkeypatch):
    _fake_per_call(monkeypatch, [(4.0, 0.1)] * linear_time.ATTEMPTS)   # 40: well under 64, still over 24
    assert not linear_time.grows_linearly(lambda s: None, lambda n: '')
