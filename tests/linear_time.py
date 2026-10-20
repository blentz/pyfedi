"""Linear-time checks that compare two input sizes instead of timing one run against a wall-clock budget.

A fixed budget flakes under machine load and coverage tracing, which slow a run by a factor that has nothing to do
with the code under test. Both runs of a ratio slow down together, and CPU time (not wall time) leaves out the waits
for a core that other processes cause.
"""
import time

# Below this (per call) the ratio would compare clock noise. A run shorter than TARGET is
# repeated within one measurement until the batch reaches it.
FLOOR = 0.00001
TARGET = 0.002
MAX_LOOPS = 256


def timed(call):
    """CPU seconds the running thread spent in `call`. Wall time also counts the seconds the thread waited for a
    core while other processes ran, which has nothing to do with the code under test."""
    started = time.thread_time()
    call()
    return time.thread_time() - started


def per_call(call, repeats=5):
    """CPU seconds of one `call`: the fastest of several runs, which drops a spike from garbage collection or a
    cache eviction. A call that takes under TARGET is run in a batch, so the clock reads a few ticks of it."""
    loops = 1
    while loops < MAX_LOOPS and timed(lambda: [call() for _ in range(loops)]) < TARGET:
        loops *= 2
    return min(timed(lambda: [call() for _ in range(loops)]) for _ in range(repeats)) / loops


def grows_linearly(scan, make, small=20_000, factor=4):
    """True if `scan` over `make(small * factor)` costs well under `factor` squared times `scan` over `make(small)`.

    Linear work gives a ratio near `factor`, quadratic near `factor` squared; the bound is twice `factor`.
    `make(size)` builds an input of about `size` units. Time is the thread's CPU time, so load from other
    processes cannot move the ratio."""
    little, big = make(small), make(small * factor)
    scan(little)   # warm caches and lazy imports before timing
    scan(big)
    return per_call(lambda: scan(big)) < max(per_call(lambda: scan(little)), FLOOR) * factor * 2
