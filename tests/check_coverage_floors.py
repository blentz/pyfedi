"""Enforce per-module coverage floors.

coverage.py's own fail_under is a single global number, so the campaign's
per-module ratchet has to be implemented. Floors live in coverage_floors.ini and
only ever rise; a module with no floor is ignored, so unfinished modules block
nobody.

Usage, after a run that wrote coverage.json:

    python tests/check_coverage_floors.py coverage.json coverage_floors.ini

This script FAILS CLOSED. A missing, unreadable, section-less or empty floors
file is an error (exit 2), not "0 floors, all met" -- `rm coverage_floors.ini`,
a merge that drops the [floors] section, or a typo in a CI path must never
produce a green ratchet. That is the one thing this script exists to prevent.

It also prints the coverage report's path and modification time on every run,
because coverage.json is gitignored and therefore persists in the working tree.
A pytest run that fails to start writes no report, so the previous run's file is
still sitting there and the ratchet would happily pass against it. The printed
timestamp makes that visible; chaining the two commands with `&&` (see
tests/README.md) is what actually prevents it.

Exit codes: 0 all floors met, 1 at least one floor violated, 2 the check could
not be performed.
"""

import configparser
import datetime
import json
import os
import sys


class FloorsFileError(Exception):
    """The floors file cannot be turned into a usable set of floors."""


def read_floors(path):
    """Read module -> minimum percentage from an ini file's [floors] section.

    Raises FloorsFileError instead of returning an empty mapping, because an
    empty mapping means "every floor passes". configparser.read() in particular
    ignores a nonexistent path and returns silently, which is why the file is
    opened explicitly here.
    """
    parser = configparser.ConfigParser()
    try:
        with open(path) as handle:
            parser.read_file(handle, source=path)
    except OSError as exc:
        raise FloorsFileError(f'cannot read floors file {path}: {exc}') from exc
    except configparser.Error as exc:
        raise FloorsFileError(f'{path} is not valid ini: {exc}') from exc

    if not parser.has_section('floors'):
        raise FloorsFileError(f'{path} has no [floors] section')

    floors = {module: float(value) for module, value in parser.items('floors')}
    if not floors:
        raise FloorsFileError(f'{path} has an empty [floors] section')
    return floors


def violations(coverage_data, floors):
    """Return (module, actual, floor) for every module below its floor.

    A floored module missing from the report counts as 0.0 rather than passing:
    a rename or an import failure would otherwise satisfy the ratchet silently.
    """
    files = coverage_data.get('files', {})
    found = []
    for module, floor in floors.items():
        entry = files.get(module)
        actual = entry['summary']['percent_covered'] if entry else 0.0
        if actual < floor:
            found.append((module, actual, floor))
    return found


def describe_report(path):
    """One line naming the report and when it was written, for staleness."""
    modified = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    return (f'Coverage report: {os.path.abspath(path)} '
            f'(modified {modified:%Y-%m-%d %H:%M:%S})')


def main(argv):
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    report_path, floors_path = argv

    try:
        with open(report_path) as handle:
            coverage_data = json.load(handle)
    except OSError as exc:
        print(f'ERROR: cannot read coverage report {report_path}: {exc}',
              file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f'ERROR: {report_path} is not valid JSON: {exc}', file=sys.stderr)
        return 2

    print(describe_report(report_path))

    try:
        floors = read_floors(floors_path)
    except FloorsFileError as exc:
        print(f'ERROR: {exc}. Refusing to report success against no floors.',
              file=sys.stderr)
        return 2

    found = violations(coverage_data, floors)
    for module, actual, floor in found:
        print(f'{module}: {actual:.2f}% is below its floor of {floor:.2f}%',
              file=sys.stderr)

    if found:
        return 1
    print(f'All {len(floors)} module floors met.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
