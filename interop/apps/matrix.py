"""Client-app gate: what each third-party app did against PieFed, diffed against what matrix.json says it should do.

Each app is run through the same three steps, `login`, `view` and `post` (STEPS). A test records an outcome per step
with `record()`, into results/<app>.json, with the app version it ran. This module's `main` then compares every app
matrix.json names against its result and exits 1, naming the app and step, when

- an app has no result, or a result names an app matrix.json does not know,
- a step's outcome differs from its expectation (a step with no expectation must pass), including an expected
  non-pass that now passes, so the matrix is updated when PieFed gains an API or an app gains PieFed support,
- an expectation is not a non-pass outcome or has no reason.

It also writes results/summary.md, the table of apps, versions and outcomes the run produced.
"""
import json
import sys
from pathlib import Path
from typing import Literal

Outcome = Literal['passed', 'failed', 'unsupported', 'blocked', 'inapplicable']
# unsupported: the app cannot do this against PieFed because PieFed lacks the API it speaks, an expected, documented
# gap rather than a bug. blocked: not tried, because an earlier step did not pass. inapplicable: the app has no such
# step at all (a podcast player neither logs in nor posts).
OUTCOMES = ('passed', 'failed', 'unsupported', 'blocked', 'inapplicable')
NON_PASS = ('failed', 'unsupported', 'blocked', 'inapplicable')
STEPS = ('login', 'view', 'post')

HERE = Path(__file__).parent
MATRIX = HERE / 'matrix.json'
RESULTS = HERE / 'results'


def load_matrix(path: Path = MATRIX) -> dict[str, dict]:
    return json.loads(path.read_text())['apps']


def record(app: str, step: str, outcome: Outcome, detail: str = '', version: str | None = None,
           results_dir: Path = RESULTS) -> None:
    """Merge one step's outcome into results/<app>.json. `version` is set once known (the first step that knows it)."""
    if step not in STEPS:
        raise ValueError(f'unknown step {step!r}')
    if outcome not in OUTCOMES:
        raise ValueError(f'unknown outcome {outcome!r}')
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f'{app}.json'
    result = json.loads(path.read_text()) if path.exists() else {'app': app, 'version': None, 'steps': {}}
    if version:
        result['version'] = version
    result['steps'][step] = {'outcome': outcome, 'detail': detail[:2000]}
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')


def collect(results_dir: Path = RESULTS) -> tuple[dict[str, dict], list[str]]:
    results, problems = {}, []
    for path in sorted(results_dir.glob('*.json')) if results_dir.is_dir() else []:
        try:
            result = json.loads(path.read_text())
            results[result['app']] = result
        except (ValueError, KeyError, TypeError):
            problems.append(f'{path.name}: not a result file')
    return results, problems


def expectation(app: dict, step: str) -> dict:
    return app.get('expect', {}).get(step) or {'outcome': 'passed'}


def inapplicable(app: dict) -> set[str]:
    """The steps the app has no such thing as (matrix.json says why): every driver records them so, untried."""
    return {step for step in STEPS if expectation(app, step)['outcome'] == 'inapplicable'}


def compare(matrix: dict[str, dict], results: dict[str, dict]) -> list[str]:
    problems = []
    for name, app in matrix.items():
        for step, expected in app.get('expect', {}).items():
            if step not in STEPS:
                problems.append(f'{name}/{step}: not a step ({", ".join(STEPS)})')
            elif expected.get('outcome') not in NON_PASS:
                problems.append(f'{name}/{step}: an expectation must be one of {", ".join(NON_PASS)}')
            elif not expected.get('reason'):
                problems.append(f'{name}/{step}: the expectation has no reason')
        result = results.get(name)
        if result is None:
            problems.append(f'{name}: no result (the app did not run)')
            continue
        for step in STEPS:
            want = expectation(app, step)['outcome']
            got = result['steps'].get(step, {}).get('outcome', 'missing')
            if got == want:
                continue
            detail = result['steps'].get(step, {}).get('detail', '')
            if want != 'passed' and got == 'passed':
                problems.append(f'{name}/{step}: now passes, expected {want}: remove the expectation from matrix.json')
            else:
                problems.append(f'{name}/{step}: {got}, expected {want}' + (f': {detail}' if detail else ''))
    for name in sorted(set(results) - set(matrix)):
        problems.append(f'{name}: a result for an app matrix.json does not name')
    return problems


def summary(matrix: dict[str, dict], results: dict[str, dict]) -> str:
    lines = ['| App | Platform | Surface | Version | ' + ' | '.join(STEPS) + ' |',
             '|---|---|---|---|' + '---|' * len(STEPS)]
    for name, app in sorted(matrix.items(), key=lambda item: (item[1].get('platform', ''), item[0])):
        result = results.get(name, {'version': None, 'steps': {}})
        cells = []
        for step in STEPS:
            got = result['steps'].get(step, {}).get('outcome', 'missing')
            want = expectation(app, step)['outcome']
            cells.append(got if got == want else f'**{got}** (expected {want})')
        lines.append(f'| {name} | {app.get("platform", "")} | {app.get("surface", "")} | {result.get("version") or "?"} | '
                     + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


def main(argv: list[str]) -> int:
    results_dir = Path(argv[0]) if argv else RESULTS
    matrix = load_matrix()
    results, problems = collect(results_dir)
    problems += compare(matrix, results)
    if results_dir.is_dir():
        (results_dir / 'summary.md').write_text(summary(matrix, results))
    for problem in problems:
        print(problem)
    print(f'apps: {len(matrix)} in the matrix, {len(results)} ran, {len(problems)} problem(s)')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
