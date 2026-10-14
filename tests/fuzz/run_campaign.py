"""On-demand fuzz campaign. NOT collected by the default test suite.

    python -m tests.fuzz.run_campaign is_valid_xml_utf8 -max_total_time=60

Run it with -m, from the repository root. Running the file by path puts
tests/fuzz/ on sys.path instead of the root, and `from tests.fuzz.targets
import ...` then fails with ModuleNotFoundError: No module named 'tests'.

The targets are tests.fuzz.targets.REGISTRY, reached through tests.fuzz.campaign,
which holds everything here that is not atheris (argv, target selection, the
test app). Collected tests check that module, never this one.

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
#
# PIL is the one exception. The image targets (tests/fuzz/targets/images.py)
# exist to drive Pillow's format parsers, and most of those are Python -- the
# plugin modules read headers, chunks and frame tables before any C decoder
# runs. Left out, a 60s upload_decode run plateaued at cov 38: every input
# looked alike to libFuzzer, because only the harness's own branches counted.
# images.py calls Image.init() at import, inside this block, so the plugins
# Pillow otherwise imports lazily on the first unknown format are loaded --
# and instrumented -- here.
#
# tests.fuzz.campaign is what imports tests.fuzz.targets, so importing it here
# is what pulls the registry, and every function its checks call, through the
# hook. TARGETS comes from there and is tests.fuzz.targets.REGISTRY itself;
# tests/test_fuzz_registry.py checks this file's source takes it from nowhere
# else, without importing this file.
INSTRUMENTED = ['app', 'tests', 'PIL']

with atheris.instrument_imports(include=INSTRUMENTED):
    from tests.fuzz.campaign import (TARGETS, WORK_ROOT, build_test_app,
                                     campaign_argv, select_target)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    target = select_target(argv)
    if target is None:
        return 2

    os.makedirs(os.path.join(WORK_ROOT, target.name), exist_ok=True)
    atheris.Setup(campaign_argv(argv[0], target, argv[2:]), target.check)

    if not target.needs_app:
        atheris.Fuzz()
        return 0

    with atheris.instrument_imports(include=INSTRUMENTED):
        application = build_test_app()
    # One context for the whole campaign, pushed before Fuzz() and never
    # popped: atheris.Fuzz() does not return, it exits the process when
    # libFuzzer finishes.
    with application.app_context():
        atheris.Fuzz()
    return 0


if __name__ == '__main__':
    sys.exit(main())
