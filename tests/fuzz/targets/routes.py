"""Every external-facing route, one atheris target per family (tests/fuzz/routes.py, spec 2026-11-13-fuzz-every-route).

Each check is tests.fuzz.routes.check for its family: the input decoded into one request, made against the seeded
world, and held to the oracle (FR1). The targets need the app and the test database; their corpora are replayed by
tests/test_fuzz_routes_corpus.py, which seeds the world once per family.
"""
from tests.fuzz import routes
from tests.fuzz.targets._target import Target, hold_to_budget


def _target(family: str) -> Target:
    def check(data: bytes) -> None:
        label, elapsed_ms = routes.check(family, data)
        if elapsed_ms > routes.REQUEST_BUDGET_MS:   # a slow request is a finding (FR1), held to the scaled budget
            hold_to_budget(label, elapsed_ms, routes.REQUEST_BUDGET_MS)
    check.__name__ = f'check_routes_{family}'
    return Target(name=f'routes_{family}', check=check, surface='routes', budget_ms=None, needs_app=True,
                  own_replay=True)


TARGETS = [_target(family) for family in sorted(routes.FAMILIES)]
