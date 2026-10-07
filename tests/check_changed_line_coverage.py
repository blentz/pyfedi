"""Fail a branch for every line it added that no test executed.

tests/check_coverage_floors.py holds each module to a percentage, which lets a
new untested line hide inside a module that is still above its floor. This gate
looks at the lines themselves: every line the branch added under app/ that
coverage.py measured and did not execute is a failure.

Usage, after a run that wrote coverage.json:

    python tests/check_changed_line_coverage.py coverage.json <base-ref> [--branches] [paths...]

With --branches it also fails every branch coverage.py measured out of an added
line that no test took (`missing_branches`, written when the run used
--cov-branch or `branch = True`). A report with no branch data at all is an
error under --branches, not a pass.

It runs `git diff --unified=0 <base-ref>..HEAD` over the given paths (all of
app/ when none are given), collects each added line, and reports the ones
coverage.json lists in `missing_lines` for that file. A line that is not
executable (a comment, a blank line, a docstring, an excluded line) is in
neither `executed_lines` nor `missing_lines` and is ignored. The report may key
a file as `app/...` or as `/app/app/...`; both are matched to the repository
path.

This script FAILS CLOSED, like check_coverage_floors.py. Each of these is an
error (exit 2), never "no uncovered lines, all clear":

  * a missing, unreadable or non-JSON report, or one that lists no files
    (every added line would then look like a non-statement);
  * a git failure (a base ref that does not exist, git not installed);
  * a path argument that does not exist (git prints an empty diff for it);
  * an added line in a .py file the report has no entry for, because that
    file's lines cannot be judged either way.

It also prints the report's path and modification time on every run, because
coverage.json is gitignored and persists in the working tree: a pytest run that
fails to start leaves the previous run's file behind. Chain this command to the
test run with `&&`.

Exit codes: 0 every added measured line was executed, 1 at least one was not,
2 the check could not be performed.
"""

import datetime
import json
import os
import re
import subprocess
import sys

DEFAULT_PATHS = ['app/']
HUNK = re.compile(r'^@@ -\S+ \+(\d+)(?:,(\d+))? @@')


class GateError(Exception):
    """The gate cannot reach a verdict."""


def run_git(base_ref, paths):
    """The text of `git diff --unified=0 <base_ref>..HEAD -- <paths>`.

    Raises GateError when git is missing or exits non-zero, because an empty
    answer from a failed command would read as "nothing was added".
    """
    command = ['git', 'diff', '--unified=0', '--no-color', '--no-ext-diff', f'{base_ref}..HEAD', '--', *paths]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        raise GateError(f'cannot run git: {exc}') from exc
    if result.returncode != 0:
        raise GateError(f'git diff failed: {result.stderr.strip() or "exit " + str(result.returncode)}')
    return result.stdout


def parse_added_lines(diff_text):
    """Map each file in a zero-context diff to the set of line numbers it added."""
    added = {}
    current = None
    for line in diff_text.splitlines():
        if line.startswith('+++ '):
            target = line[4:].split('\t')[0]
            current = target[2:] if target.startswith('b/') else None
        elif line.startswith('@@'):
            if current is None:
                continue
            match = HUNK.match(line)
            if not match:
                raise GateError(f'cannot read hunk header {line!r}')
            start = int(match.group(1))
            count = 1 if match.group(2) is None else int(match.group(2))
            if count:
                added.setdefault(current, set()).update(range(start, start + count))
    return added


def repo_relative_files(files):
    """The report's `files` keyed by repository path: `/app/app/x.py` becomes `app/x.py`."""
    return {(key[len('/app/'):] if key.startswith('/app/') else key): value for key, value in files.items()}


def uncovered_added_lines(added, coverage_data):
    """Return ({path: sorted uncovered added lines}, [python files with no report entry])."""
    files = repo_relative_files(coverage_data['files'])
    uncovered = {}
    unmeasured = []
    for path in sorted(added):
        if not path.endswith('.py'):
            continue
        entry = files.get(path)
        if entry is None:
            unmeasured.append(path)
            continue
        missing = set(entry.get('missing_lines', []))
        hit = sorted(added[path] & missing)
        if hit:
            uncovered[path] = hit
    return uncovered, unmeasured


def uncovered_added_branches(added, coverage_data):
    """{path: sorted [(line, destination)]} for each untaken branch out of an added line.

    A destination below zero is coverage.py's "exit from the function"."""
    files = repo_relative_files(coverage_data['files'])
    uncovered = {}
    for path in sorted(added):
        entry = files.get(path)
        if entry is None or not path.endswith('.py'):
            continue
        hit = sorted((source, destination) for source, destination in entry.get('missing_branches', [])
                     if source in added[path])
        if hit:
            uncovered[path] = hit
    return uncovered


def has_branch_data(coverage_data):
    return any('missing_branches' in entry for entry in coverage_data['files'].values())


def describe_report(path):
    """One line naming the report and when it was written, for staleness."""
    modified = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    return (f'Coverage report: {os.path.abspath(path)} '
            f'(modified {modified:%Y-%m-%d %H:%M:%S})')


def main(argv):
    branches = '--branches' in argv
    argv = [argument for argument in argv if argument != '--branches']
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    report_path, base_ref, *paths = argv
    paths = paths or DEFAULT_PATHS

    try:
        with open(report_path) as handle:
            coverage_data = json.load(handle)
    except OSError as exc:
        print(f'ERROR: cannot read coverage report {report_path}: {exc}', file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f'ERROR: {report_path} is not valid JSON: {exc}', file=sys.stderr)
        return 2

    print(describe_report(report_path))

    files = coverage_data.get('files') if isinstance(coverage_data, dict) else None
    if not files or not isinstance(files, dict):
        print(f'ERROR: {report_path} lists no files. Refusing to report success against an empty report.',
              file=sys.stderr)
        return 2

    if branches and not has_branch_data(coverage_data):
        print(f'ERROR: {report_path} has no branch data; run the tests with --cov-branch.', file=sys.stderr)
        return 2

    for path in paths:
        if not os.path.exists(path):
            print(f'ERROR: path {path} does not exist; git would diff nothing for it.', file=sys.stderr)
            return 2

    try:
        added = parse_added_lines(run_git(base_ref, paths))
    except GateError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

    uncovered, unmeasured = uncovered_added_lines(added, coverage_data)

    for path in unmeasured:
        print(f'ERROR: {path} has added lines but is not in the coverage report; it cannot be judged.',
              file=sys.stderr)
    if unmeasured:
        return 2

    for path, lines in uncovered.items():
        for number in lines:
            print(f'{path}:{number}: added line was not executed by any test', file=sys.stderr)

    missed = uncovered_added_branches(added, coverage_data) if branches else {}
    for path, pairs in missed.items():
        for source, destination in pairs:
            target = 'exit' if destination < 0 else destination
            print(f'{path}:{source}: branch to {target} was never taken', file=sys.stderr)

    if uncovered or missed:
        total = sum(len(lines) for lines in uncovered.values())
        branch_total = sum(len(pairs) for pairs in missed.values())
        print(f'{total} added line(s) not executed, {branch_total} branch(es) out of added lines not taken.',
              file=sys.stderr)
        return 1
    print(f'Every added line since {base_ref} that coverage measured was executed'
          f'{" and every branch out of one was taken" if branches else ""}.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
