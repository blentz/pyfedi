"""Conformance gate: diff the suites' results against the committed baseline of expected non-passes."""
import json
import re
import sys
from pathlib import Path
from typing import Literal

Outcome = Literal['passed', 'failed', 'skipped', 'error']

BASELINE = Path(__file__).with_name('baseline.json')
EARL_OUTCOMES: dict[str, Outcome] = {
    'passed': 'passed', 'failed': 'failed', 'inapplicable': 'skipped', 'cantTell': 'skipped',
}
TAP_LINE = re.compile(r'^(not ok|ok)\b\s*(?:\d+)?\s*(?:-\s*)?(.*)$')
TAP_SKIP = re.compile(r'\s+#\s*SKIP\b.*$', re.IGNORECASE)


def parse_tap(text: str, prefix: str) -> dict[str, Outcome]:
    results: dict[str, Outcome] = {}
    for line in text.splitlines():
        if line.startswith('Bail out!'):
            results[f'{prefix}/__bail__'] = 'error'
            continue
        match = TAP_LINE.match(line)   # indented YAML diagnostics and '#' comments do not match
        if not match:
            continue
        name = match.group(2)
        skipped = TAP_SKIP.search(name) is not None
        name = TAP_SKIP.sub('', name).strip()
        if match.group(1) == 'not ok':
            outcome: Outcome = 'failed'
        else:
            outcome = 'skipped' if skipped else 'passed'
        results[f'{prefix}/{name}'] = outcome
    return results


def feditest_outcome(result: dict | None) -> Outcome:
    """Classify a transcript result the way feditest's TestRunTranscriptSummary.add_test_result does."""
    if result is None:
        return 'passed'
    kind = result['type']
    if kind.endswith('AssertionFailure'):
        return 'failed'
    if kind.endswith(('SkipTestException', 'NotImplementedByNodeError', 'NotImplementedByNodeDriverError')):
        return 'skipped'
    return 'error'


def worst_result(run_test: dict) -> dict | None:
    """A test's result as feditest's TestRunTestTranscript.worst_result: one that is set wins, else the steps': the
    first, which a later one replaces unless it is only a soft or degrade failure."""
    if run_test['result']:
        return run_test['result']
    result = None
    for step in run_test.get('run_steps') or []:
        if result is None or (step['result'] and step['result']['type'] not in ('SoftAssertionFailure', 'DegradeAssertionFailure')):
            result = step['result']
    return result


def parse_feditest_json(text: str, prefix: str) -> dict[str, Outcome]:
    """Read feditest's --json transcript. Its TAP writer reports skips as ok, so the gate reads this instead."""
    transcript = json.loads(text)
    tests = transcript['plan']['session_template']['tests']
    results: dict[str, Outcome] = {}
    if transcript.get('result'):
        results[f'{prefix}/__run__'] = 'error'
    for number, session in enumerate(transcript['sessions']):
        if session.get('result'):
            results[f'{prefix}/__session_{number}__'] = 'error'
        for run_test in session['run_tests']:
            name = transcript['test_meta'][tests[run_test['plan_test_index']]['name']]['name']
            result = worst_result(run_test)
            results[f'{prefix}/{name}'] = feditest_outcome(result)
    return results


def parse_aptest_ndjson(text: str, actor: str) -> dict[str, Outcome]:
    results: dict[str, Outcome] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        assertion = json.loads(line)
        test = assertion['test']
        slug = test.get('slug') or test['url'].rstrip('/').rsplit('/', 1)[-1]   # 0.16.1 writes slug; the EARL test url ends in it too
        outcome = re.split(r'[#/]', assertion['result']['outcome'])[-1]   # a URI such as earl:passed, or the short name
        results[f'aptest/{actor}/{slug}'] = EARL_OUTCOMES.get(outcome, 'error')
    return results


def compare(results: dict[str, Outcome], baseline: dict[str, dict]) -> list[str]:
    if not results:
        return ['no results']
    problems = []
    for test_id, entry in sorted(baseline.items()):
        if not entry.get('reason'):
            problems.append(f'{test_id}: baseline entry has no reason')
        if test_id not in results:
            problems.append(f'{test_id}: baselined but did not run')
        elif results[test_id] == 'passed':
            problems.append(f'{test_id}: now passes, remove it from the baseline')
        elif results[test_id] != entry.get('outcome'):
            problems.append(f'{test_id}: baselined as {entry.get("outcome")} but is now {results[test_id]}')
    for test_id, outcome in sorted(results.items()):
        if outcome != 'passed' and test_id not in baseline:
            problems.append(f'{test_id}: {outcome} and not in the baseline')
    return problems


def collect(results_dir: Path) -> dict[str, Outcome]:
    results: dict[str, Outcome] = {}
    for path in sorted(results_dir.glob('*.json')):
        if not path.name.startswith('aptest-'):
            results |= parse_feditest_json(path.read_text(), path.stem)
    for path in sorted(results_dir.glob('aptest-*.ndjson')):
        results |= parse_aptest_ndjson(path.read_text(), path.stem.removeprefix('aptest-'))
    return results


def main(argv: list[str]) -> int:
    problems = compare(collect(Path(argv[0])), json.loads(BASELINE.read_text()))
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
