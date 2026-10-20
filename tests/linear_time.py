"""Linear-time checks that compare two input sizes instead of timing one run against a wall-clock budget.

A fixed budget flakes under machine load and coverage tracing, which slow a run by a factor that has nothing to do
with the code under test. Both runs of a ratio slow down together, so the ratio stays put.
"""
import time

# Below this a run is too short to time: the ratio would compare scheduler noise.
FLOOR = 0.0005


def timed(call):
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


def best_of(call, repeats=5):
    """The fastest of several runs: a spike from the scheduler or a garbage collection only ever adds time."""
    return min(timed(call) for _ in range(repeats))


def grows_linearly(scan, make, small=20_000, factor=4):
    """True if `scan` over `make(small * factor)` costs well under `factor` squared times `scan` over `make(small)`.

    Linear work gives a ratio near `factor`, quadratic near `factor` squared; the bound is twice `factor`.
    `make(size)` builds an input of about `size` units."""
    little, big = make(small), make(small * factor)
    scan(little)   # warm caches and lazy imports before timing
    scan(big)
    return best_of(lambda: scan(big)) < max(best_of(lambda: scan(little)), FLOOR) * factor * 2
