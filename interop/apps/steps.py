"""Running an app's steps in order and recording each (matrix.record): a step that raises is `failed` with its error,
and every later step is `blocked` by it. A driver is three callables, login, view and post, each raising
AssertionError (or a timeout) when the app did not do it."""
from collections.abc import Callable
from pathlib import Path

from interop.apps.matrix import RESULTS, STEPS, expectation, inapplicable, record


def run(name: str, app: dict, drivers: dict[str, Callable[[], str | None]], version: str | None = None,
        on_failure: Callable[[str], None] | None = None, results_dir: Path = RESULTS) -> None:
    """`drivers[step]()` returns an optional detail on success. `on_failure(step)` captures evidence (a screenshot)."""
    skipped = inapplicable(app)
    blocked_by = None
    for step in STEPS:
        if step in skipped:
            record(name, step, 'inapplicable', expectation(app, step)['reason'], version=version, results_dir=results_dir)
            continue
        if blocked_by is not None:
            record(name, step, 'blocked', f'{blocked_by} did not pass', version=version, results_dir=results_dir)
            continue
        try:
            detail = drivers[step]() or ''
        except Exception as error:   # any failure of the app's UI is this step's outcome, not the harness's
            if on_failure is not None:
                try:
                    on_failure(step)
                except Exception:   # evidence is best effort: the outcome is recorded either way
                    pass
            record(name, step, 'failed', f'{type(error).__name__}: {error}', version=version, results_dir=results_dir)
            blocked_by = step
            continue
        record(name, step, 'passed', detail, version=version, results_dir=results_dir)
