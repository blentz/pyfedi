"""On-demand fuzz campaign. NOT collected by the default test suite.

    python -m tests.fuzz.run_campaign is_valid_xml_utf8 -max_total_time=60

Run it with -m, from the repository root. Running the file by path puts
tests/fuzz/ on sys.path instead of the root, and `from tests.fuzz.targets
import ...` then fails with ModuleNotFoundError: No module named 'tests'.

The targets are tests.fuzz.targets.REGISTRY; TARGETS below IS that dict, not a
copy, and tests/test_fuzz_registry.py asserts the identity so a hand-maintained
list cannot creep back in here.

Runs outside the coverage run deliberately: atheris installs its own tracing to
guide mutation, and coverage.py is already tracing. Any input that trips a
property check is written to tests/fuzz/corpus/<target>/ by libFuzzer, where the
corpus replay test picks it up permanently.

The file name is not test_*.py and the directory holds no test functions, so
pytest collects nothing here. That is what keeps the default suite
deterministic.
"""

import math
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
    from tests.fuzz.targets import REGISTRY

# Importing this module from the registry gate (tests/test_fuzz_registry.py,
# which IS collected by the coverage run) instruments nothing: that test imports
# tests.fuzz.targets first, so the import above is a sys.modules hit and the
# atheris import hook -- a sys.meta_path finder that only exists for the
# duration of the with block -- never sees a module load. Bytecode tracing only
# starts with atheris.Fuzz(), which only main() calls.

# name -> Target. Each target seeds from and writes back to
# tests/fuzz/corpus/<name>/.
#
# There used to be a fourth target, allowlist_html_past_known_defects: the same
# target with two reported-but-unfixed defects suppressed, so a campaign could
# keep hunting instead of aborting on them at execution 0. Both defects are now
# fixed in app/utils.py -- the javascript:-URL bypass and the IndexError on
# "</>" -- so the suppression has been deleted rather than left in place to
# swallow a regression, and the honest allowlist_html target runs.
TARGETS = REGISTRY


WORK_ROOT = 'tests/fuzz/.work'
CORPUS_ROOT = 'tests/fuzz/corpus'


def default_timeout_seconds(budget_ms: int) -> int:
    """libFuzzer -timeout for a budgeted target: ten times its replay budget.

    The replay fails an input that exceeds budget_ms; the campaign is looser on
    purpose, because the fuzzer runs instrumented (several times slower than the
    replay) and a timeout artifact should mean "pathologically slow", not "the
    tracer was busy". libFuzzer takes whole seconds, so round up, never to 0 --
    -timeout=0 means no timeout at all.
    """
    return max(1, math.ceil(budget_ms * 10 / 1000))


def _flag_name(argument: str) -> str:
    """'-timeout=5' -> 'timeout'. libFuzzer flags are all -name=value."""
    return argument.lstrip('-').partition('=')[0]


def campaign_argv(program: str, target, user_args) -> list[str]:
    """The argv handed to atheris.Setup for one target.

    A default this function adds is DROPPED when the caller passed the same flag
    rather than merely placed before it. libFuzzer happens to let the last
    occurrence win, but relying on that would make "an explicit -timeout= or
    -artifact_prefix= overrides the default" a property of libFuzzer's flag
    parser instead of something this file says and tests/test_fuzz_registry.py
    checks. tests/fuzz/smoke.py depends on it: it passes its own
    -artifact_prefix so a smoke run never writes into the committed corpus.
    """
    committed = f'{CORPUS_ROOT}/{target.name}'

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
    work = os.path.join(WORK_ROOT, target.name)

    # A property violation is a different thing from a coverage unit: libFuzzer
    # writes it as a crash- artifact under -artifact_prefix, which points at the
    # committed corpus so a finding is picked up by the corpus replay test and
    # becomes permanent. Give it a name that says what it is before committing it.
    defaults = [f'-artifact_prefix={committed}/']

    # A budgeted target is one whose slowness IS the finding (a ReDoS shape).
    # libFuzzer's own default -timeout is 1200 s, which no campaign would ever
    # reach; this makes a slow unit come out as a timeout- artifact instead.
    if target.budget_ms is not None:
        defaults.append(f'-timeout={default_timeout_seconds(target.budget_ms)}')

    supplied = {_flag_name(argument) for argument in user_args if argument.startswith('-')}
    defaults = [flag for flag in defaults if _flag_name(flag) not in supplied]
    return [program, work, committed] + defaults + list(user_args)


def build_test_app():
    """The Flask app a needs_app target runs inside, built the way the suite
    builds it: create_app(TestConfig) from tests/conftest.py.

    Imported here, not at module level, so a target that does not need an app
    never pays for create_app() or for conftest's test-only imports. Called
    inside atheris.instrument_imports() by main(), so the blueprint modules
    create_app() loads for the first time are instrumented too and the mutator
    gets coverage feedback from them.

    No database is touched by building it. A check that then touches the
    database is a harness bug: a campaign must not need one.
    """
    from app import create_app
    from tests.conftest import TestConfig
    return create_app(TestConfig)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) < 2 or argv[1] not in TARGETS:
        print(f'usage: {argv[0]} <{"|".join(sorted(TARGETS))}> [libfuzzer args]')
        return 2
    target = TARGETS[argv[1]]

    os.makedirs(os.path.join(WORK_ROOT, target.name), exist_ok=True)
    atheris.Setup(campaign_argv(argv[0], target, argv[2:]), target.check)

    if not target.needs_app:
        atheris.Fuzz()
        return 0

    with atheris.instrument_imports(include=['app', 'tests']):
        application = build_test_app()
    # One context for the whole campaign, pushed before Fuzz() and never
    # popped: atheris.Fuzz() does not return, it exits the process when
    # libFuzzer finishes.
    with application.app_context():
        atheris.Fuzz()
    return 0


if __name__ == '__main__':
    sys.exit(main())
