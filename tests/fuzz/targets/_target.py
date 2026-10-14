"""The record every fuzz target is registered as.

In its own module, not in tests/fuzz/targets/__init__.py, because the surface
modules import it and __init__ imports the surface modules: defining it in
__init__ would make every surface module depend on __init__ having got exactly
far enough before importing them. The leading underscore also keeps it out of
the registry gate's "every module in this package contributes TARGETS" sweep.
"""

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
