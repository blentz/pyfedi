"""Conformance gate: diff the suites' results against the committed baseline of expected non-passes."""
import json
import re
import sys
from pathlib import Path
from typing import Literal

Outcome = Literal['passed', 'failed', 'skipped', 'error', 'inapplicable', 'cantTell']   # the last two are EARL's own, for aptest

BASELINE = Path(__file__).with_name('baseline.json')
EARL_OUTCOMES: dict[str, Outcome] = {
    'passed': 'passed', 'failed': 'failed', 'inapplicable': 'inapplicable', 'cantTell': 'cantTell',
}
TESTS_DIR = '/opt/feditest-tests-fediverse/tests/'   # the pinned feditest-tests-fediverse checkout in the runner image
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


def feditest_test_results(transcript: dict, prefix: str):
    """Yield (id, result) for each test of each session, the result being what worst_result picks."""
    tests = transcript['plan']['session_template']['tests']
    for session in transcript['sessions']:
        for run_test in session['run_tests']:
            name = transcript['test_meta'][tests[run_test['plan_test_index']]['name']]['name']
            yield f'{prefix}/{name}', worst_result(run_test)


def parse_feditest_json(text: str, prefix: str) -> dict[str, Outcome]:
    """Read feditest's --json transcript. Its TAP writer reports skips as ok, so the gate reads this instead."""
    transcript = json.loads(text)
    results: dict[str, Outcome] = {}
    if transcript.get('result'):
        results[f'{prefix}/__run__'] = 'error'
    for number, session in enumerate(transcript['sessions']):
        if session.get('result'):
            results[f'{prefix}/__session_{number}__'] = 'error'
    for test_id, result in feditest_test_results(transcript, prefix):
        results[test_id] = feditest_outcome(result)
    return results


def parse_feditest_locations(text: str, prefix: str) -> dict[str, str]:
    """Where each failed or errored test failed: its innermost stacktrace frame in the tests dir as `<file>.py:<line>`,
    or `unknown` when none is there. A baselined test that fails at another line is another failure."""
    locations: dict[str, str] = {}
    for test_id, result in feditest_test_results(json.loads(text), prefix):
        if feditest_outcome(result) in ('failed', 'error'):
            frames = [(file, line) for file, line in result.get('stacktrace') or [] if file.startswith(TESTS_DIR)]
            locations[test_id] = f'{Path(frames[-1][0]).name}:{frames[-1][1]}' if frames else 'unknown'
    return locations


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


def compare(results: dict[str, Outcome], baseline: dict[str, dict], locations: dict[str, str] | None = None) -> list[str]:
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
        elif test_id in (locations or {}) and not entry.get('at'):
            problems.append(f'{test_id}: baseline entry has no at')
        elif test_id in (locations or {}) and locations[test_id] != entry['at']:
            problems.append(f'{test_id}: baselined as {entry["outcome"]} at {entry["at"]} but fails elsewhere, at {locations[test_id]}')
    for test_id, outcome in sorted(results.items()):
        if outcome != 'passed' and test_id not in baseline:
            problems.append(f'{test_id}: {outcome} and not in the baseline')
    return problems


def collect(results_dir: Path) -> tuple[dict[str, Outcome], dict[str, str]]:
    results: dict[str, Outcome] = {}
    locations: dict[str, str] = {}
    for path in sorted(results_dir.glob('*.json')):
        if not path.name.startswith('aptest-'):
            results |= parse_feditest_json(path.read_text(), path.stem)
            locations |= parse_feditest_locations(path.read_text(), path.stem)
    for path in sorted(results_dir.glob('aptest-*.ndjson')):
        results |= parse_aptest_ndjson(path.read_text(), path.stem.removeprefix('aptest-'))
    return results, locations


def main(argv: list[str]) -> int:
    results, locations = collect(Path(argv[0]))
    problems = compare(results, json.loads(BASELINE.read_text()), locations)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
