"""On-demand fuzz campaign. NOT collected by the default test suite.

    python -m tests.fuzz.run_campaign is_valid_xml_utf8 -max_total_time=60

Run it with -m, from the repository root. Running the file by path puts
tests/fuzz/ on sys.path instead of the root, and `from tests.fuzz.harnesses
import ...` then fails with ModuleNotFoundError: No module named 'tests'.

Runs outside the coverage run deliberately: atheris installs its own tracing to
guide mutation, and coverage.py is already tracing. Any input that trips a
property check is written to tests/fuzz/corpus/<target>/ by libFuzzer, where the
corpus replay test picks it up permanently.

The file name is not test_*.py and the directory holds no test functions, so
pytest collects nothing here. That is what keeps the default suite
deterministic.
"""

import os
import sys

import atheris

# The import has to happen inside instrument_imports() -- atheris rewrites
# bytecode as the module is loaded, so a module already imported cannot be
# instrumented afterwards. Without this, libFuzzer gets no coverage feedback at
# all: it prints "no interesting inputs were found so far. Is the code
# instrumented for coverage?", keeps a one-byte corpus, and degenerates into
# blind random bytes. Measured on this repository: uninstrumented, a 60s
# is_valid_xml_utf8 run did 40,499,168 executions and grew the corpus not once.
#
# include= narrows instrumentation to PieFed's own code and these harnesses.
# Instrumenting the whole import graph underneath app.utils -- Flask,
# SQLAlchemy, boto3, lxml's Python shims -- would spend the budget guiding the
# mutator towards branches in third-party libraries that are not under test.
with atheris.instrument_imports(include=['app', 'tests']):
    from tests.fuzz.harnesses import (check_allowlist_html,
                                      check_is_valid_xml_utf8,
                                      check_sanitize_svg_bytes)

# name -> (property check, corpus directory it seeds from and writes back to).
#
# There used to be a fourth target, allowlist_html_past_known_defects: the same
# target with two reported-but-unfixed defects suppressed, so a campaign could
# keep hunting instead of aborting on them at execution 0. Both defects are now
# fixed in app/utils.py -- the javascript:-URL bypass and the IndexError on
# "</>" -- so the suppression has been deleted rather than left in place to
# swallow a regression, and the honest allowlist_html target runs.
TARGETS = {
    'allowlist_html': (check_allowlist_html, 'allowlist_html'),
    'is_valid_xml_utf8': (check_is_valid_xml_utf8, 'is_valid_xml_utf8'),
    'sanitize_svg_bytes': (check_sanitize_svg_bytes, 'sanitize_svg_bytes'),
}


WORK_ROOT = 'tests/fuzz/.work'


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        print(f'usage: {sys.argv[0]} <{"|".join(sorted(TARGETS))}> [libfuzzer args]')
        return 2
    check, corpus_name = TARGETS[sys.argv[1]]
    committed = f'tests/fuzz/corpus/{corpus_name}'

    # Two corpus directories, and which one comes first matters. libFuzzer WRITES
    # every coverage-increasing unit it discovers into the FIRST directory it is
    # given and only READS the rest, so the committed corpus goes second and stays
    # what a human put there: hand-written hostile inputs, plus reproducers for
    # real findings. One 60s run against sanitize_svg_bytes produced 112 new units;
    # letting those land in tests/fuzz/corpus/ would bury the fourteen named seeds
    # under SHA-named noise and commit it.
    #
    # The working directory is gitignored and kept between runs on purpose: a
    # later campaign starts from the coverage the last one reached instead of
    # from the seeds again.
    work = os.path.join(WORK_ROOT, corpus_name)
    os.makedirs(work, exist_ok=True)

    # A property violation is a different thing from a coverage unit: libFuzzer
    # writes it as a crash- artifact under -artifact_prefix, which points at the
    # committed corpus so a finding is picked up by the corpus replay test and
    # becomes permanent. Give it a name that says what it is before committing it.
    artifacts = f'-artifact_prefix={committed}/'

    atheris.Setup([sys.argv[0], work, committed, artifacts] + sys.argv[2:], check)
    atheris.Fuzz()
    return 0


if __name__ == '__main__':
    sys.exit(main())
