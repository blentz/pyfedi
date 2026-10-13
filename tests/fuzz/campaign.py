"""The campaign's plain-Python half: target selection, libFuzzer argv, test app.

Everything tests/fuzz/run_campaign.py does that is not atheris lives here, so
the collected tests (tests/test_fuzz_registry.py, tests/test_fuzz_smoke.py) can
check it without importing run_campaign. Importing run_campaign means importing
atheris and entering atheris.instrument_imports(), and whether that instruments
anything under the coverage run would then depend on what happened to be
imported first. This module imports no atheris, so the question does not arise;
tests/test_fuzz_registry.py fails if a collected test imports run_campaign or
calls instrument_imports()/Fuzz() itself.

run_campaign imports this module INSIDE instrument_imports(), which is what
gets tests.fuzz.targets -- and through it every function under test --
instrumented in a real campaign.
"""

import math
import os

from tests.fuzz.targets import REGISTRY

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
    never pays for create_app() or for conftest's test-only imports.
    run_campaign.main() calls it inside atheris.instrument_imports(), so the
    blueprint modules create_app() loads for the first time are instrumented
    too and the mutator gets coverage feedback from them.

    No database is touched by building it. A check that then touches the
    database is a harness bug: a campaign must not need one.
    """
    from app import create_app
    from tests.conftest import TestConfig
    return create_app(TestConfig)


def select_target(argv):
    """The Target argv[1] names, or None after printing usage."""
    if len(argv) < 2 or argv[1] not in TARGETS:
        print(f'usage: {argv[0]} <{"|".join(sorted(TARGETS))}> [libfuzzer args]')
        return None
    return TARGETS[argv[1]]
