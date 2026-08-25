"""Enforce per-module coverage floors.

coverage.py's own fail_under is a single global number, so the campaign's
per-module ratchet has to be implemented. Floors live in coverage_floors.ini and
only ever rise; a module with no floor is ignored, so unfinished modules block
nobody.

Usage, after a run that wrote coverage.json:

    python tests/check_coverage_floors.py coverage.json coverage_floors.ini
"""

import configparser
import json
import sys


def read_floors(path):
    """Read module -> minimum percentage from an ini file's [floors] section."""
    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section('floors'):
        return {}
    return {module: float(value) for module, value in parser.items('floors')}


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


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    with open(sys.argv[1]) as handle:
        coverage_data = json.load(handle)
    floors = read_floors(sys.argv[2])

    found = violations(coverage_data, floors)
    for module, actual, floor in found:
        print(f'{module}: {actual:.2f}% is below its floor of {floor:.2f}%',
              file=sys.stderr)

    if found:
        return 1
    print(f'All {len(floors)} module floors met.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
