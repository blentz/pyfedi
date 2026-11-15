"""Running an app's steps in order and recording each (matrix.record): a step that raises is `failed` with its error,
and every later step is `blocked` by it. A driver is three callables, login, view and post, each raising
AssertionError (or a timeout) when the app did not do it."""
import json
from collections.abc import Callable
from pathlib import Path

from interop.apps.matrix import RESULTS, STEPS, expectation, inapplicable, record


class Unsupported(Exception):
    """Raised by a driver whose step needs an API PieFed does not serve yet: recorded `unsupported`, with the reason,
    and blocking no later step."""


def run(name: str, app: dict, drivers: dict[str, Callable[[], str | None]], version: str | None = None,
        on_failure: Callable[[str], None] | None = None, results_dir: Path = RESULTS) -> bool:
    """`drivers[step]()` returns an optional detail on success. `on_failure(step)` captures evidence (a screenshot).
    True unless a step failed that the matrix expects to pass."""
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
        except Unsupported as missing:
            record(name, step, 'unsupported', str(missing), version=version, results_dir=results_dir)
            continue
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
    return blocked_by is None or expectation(app, blocked_by)['outcome'] != 'passed'


def run_with_retry(name: str, app: dict, attempt: Callable[[bool], tuple[dict, Callable[[str], None] | None]],
                   version: str | None = None, results_dir: Path = RESULTS) -> None:
    """`run`, and once more from fresh state if a step failed that should pass: under a loaded host an app's own
    screens can miss a wait, which a second go from scratch rules out (owner ruling 2026-11-15). `attempt(retry)` sets
    up the app (fresh, when `retry`) and returns its drivers and evidence capture. The first try's evidence is kept as
    `<name>-try1-*`, and the result says which steps needed the second go and how they first failed (`retried`), so
    the gate shows flakiness without failing on it."""
    drivers, on_failure = attempt(False)
    if run(name, app, drivers, version=version, on_failure=on_failure, results_dir=results_dir):
        return
    path = results_dir / f'{name}.json'
    first = json.loads(path.read_text())
    failed = {step: result['detail'] for step, result in first['steps'].items() if result['outcome'] == 'failed'}
    for evidence in results_dir.glob(f'{name}-*'):
        if not evidence.name.startswith(f'{name}-try1-'):
            evidence.rename(results_dir / evidence.name.replace(f'{name}-', f'{name}-try1-', 1))
    path.unlink()
    drivers, on_failure = attempt(True)
    run(name, app, drivers, version=version or first.get('version'), on_failure=on_failure, results_dir=results_dir)
    result = json.loads(path.read_text())
    result['retried'] = failed
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
