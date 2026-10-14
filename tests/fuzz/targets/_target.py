"""The record every fuzz target is registered as.

In its own module, not in tests/fuzz/targets/__init__.py, because the surface
modules import it and __init__ imports the surface modules: defining it in
__init__ would make every surface module depend on __init__ having got exactly
far enough before importing them. The leading underscore also keeps it out of
the registry gate's "every module in this package contributes TARGETS" sweep.

The load calibration a time budget is held to lives here too (machine_slowdown, hold_to_budget), shared by the
corpus replay's run_case (tests/test_utils_fuzz_corpus.py) and the checks that time a step themselves
(signatures' _check_deep_ld_refused).
"""

import re
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Target:
    """One libFuzzer target and the property check it drives.

    name      -- the corpus directory name under tests/fuzz/corpus/ and the name
                 run_campaign and smoke take on the command line.
    check     -- takes the raw input bytes and raises AssertionError on a
                 property break. Plain function, no atheris: the corpus replay
                 calls it inside the ordinary suite.
    surface   -- key into tests.fuzz.surfaces.SURFACES: which production entry
                 points this target stands for.
    budget_ms -- per-input CPU-time budget the corpus replay enforces, or None
                 for untimed. Set about ten times the slowest committed seed, so
                 CI noise cannot flake it but a ReDoS seed (committed at a size
                 that takes seconds on the vulnerable regex) fails loudly. The
                 campaign turns it into a default libFuzzer -timeout as well.
    needs_app -- the check must run inside a Flask app context. The context is
                 built from the test config and the check may still not touch
                 the database: one that does is a harness bug.
    """
    name: str
    check: Callable[[bytes], None]
    surface: str
    budget_ms: int | None
    needs_app: bool


def reference_work() -> int:
    """A fixed piece of CPU-bound Python -- formatting, sorting, a regex scan --
    whose time says how fast this machine is running Python right now."""
    words = [str(i * 7919 % 100003) for i in range(80_000)]
    return len(re.findall(r'[0-9]+7', ' '.join(sorted(words))))


# reference_work's CPU time in the test container on an idle machine (AMD Ryzen
# 9 7945HX, 2026-10-14), the machine every budget_ms was measured on.
REFERENCE_WORK_MS = 44


def machine_slowdown(clock=time.thread_time) -> float:
    """How many times slower than nominal this machine runs reference_work now,
    and never below 1: a faster machine does not tighten any budget."""
    started = clock()
    reference_work()
    return max(1.0, (clock() - started) * 1000 / REFERENCE_WORK_MS)


def hold_to_budget(what: str, elapsed_ms: float, budget_ms: float, slowdown=machine_slowdown) -> None:
    """Fail when `what` took more CPU time than `budget_ms` times how much slower than nominal the machine runs
    now. The machine is measured only when the budget alone is exceeded, so nothing is spent on a step within it.

    CPU time, not wall-clock, because wall-clock also counts the time the scheduler gives the CPU to other work;
    and scaled, because CPU time still grows when the machine slows: beside other busy processes the cores clock
    down and share their units (SMT). reference_work slows by the same factor, so the scaled budget allows for the
    machine, not for a slower function."""
    if elapsed_ms > budget_ms:
        factor = slowdown()
        assert elapsed_ms <= budget_ms * factor, (
            f'{what} took {elapsed_ms:.0f} ms, over its {budget_ms} ms budget '
            f'({budget_ms * factor:.0f} ms on this machine now, {factor:.2f} times slower than nominal)')
