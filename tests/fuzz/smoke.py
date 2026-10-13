"""Smoke campaign: every fuzz target for a few seconds each. NOT collected by pytest.

    python -m tests.fuzz.smoke [--seconds N] [--jobs J] [targets...]

Run it in the test container, from the repository root, like run_campaign:

    podman-compose -f compose.test.yaml exec -T -w /app test-runner \\
        python -m tests.fuzz.smoke --seconds 20

With no target names it runs every target in tests.fuzz.targets.REGISTRY.
Each target is its own `python -m tests.fuzz.run_campaign <name>` subprocess,
for three reasons: atheris.Fuzz() exits the process when libFuzzer finishes, so
one process cannot run two campaigns; a target that crashes the interpreter
outright takes only its own run down; and every campaign starts from a fresh
import, instrumented by run_campaign exactly as a hand-run campaign is. Like
run_campaign it runs outside the coverage run -- this file is not test_*.py
and holds no test functions.

Exits non-zero, listing each failing target, when a target left a crash-,
timeout-, oom- or leak- artifact, or its campaign exited non-zero, or it did not
finish in time. tests/test_fuzz_smoke.py tests the argument handling, the
command line and the result logic without starting libFuzzer.

ARTIFACTS DO NOT GO TO THE COMMITTED CORPUS. A hand-run campaign writes a
finding straight into tests/fuzz/corpus/<target>/ so it is picked up by the
replay; a smoke run is a check, and leaving SHA-named crash files in the
committed tree for someone to notice in `git status` is the wrong way to report
one. So each target gets -artifact_prefix=tests/fuzz/.work/smoke/<target>/
(gitignored, emptied before every run so a stale artifact cannot fail a clean
run), which tests.fuzz.campaign.campaign_argv lets override its default. The
coverage corpus still goes to tests/fuzz/.work/<target>/, shared with
run_campaign, so a smoke run also extends the next campaign's starting point.
The libFuzzer output of each run is kept beside its artifacts as campaign.log.
"""

import argparse
import concurrent.futures
import os
import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from tests.fuzz.targets import REGISTRY

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SMOKE_ROOT = pathlib.Path('tests/fuzz/.work/smoke')

# libFuzzer's artifact kinds that mean a finding. slow-unit- is deliberately
# absent: libFuzzer writes it for any unit slower than -report_slow_units (10 s
# by default) without failing the run, and a budgeted target's real slowness
# limit is -timeout, which produces timeout-.
FAILURE_PREFIXES = ('crash-', 'timeout-', 'oom-', 'leak-')

# How long past --seconds a campaign may run before it is killed and failed.
# -max_total_time starts counting after libFuzzer has loaded and replayed the
# seed corpus, and that load (import of app, instrumentation, the work corpus a
# previous run grew) takes a while on its own.
GRACE_SECONDS = 120

# libFuzzer's final stats line, e.g. "#90984\tDONE   cov: 90 ft: 513 corp: ...".
_DONE_LINE = re.compile(r'^#\d+\s+DONE\b.*$', re.MULTILINE)


@dataclass(frozen=True)
class Result:
    name: str
    returncode: int | None   # None: killed after the grace period
    artifacts: tuple[str, ...]
    summary: str              # libFuzzer's DONE line, or '' if it never printed one

    @property
    def failed(self) -> bool:
        return self.returncode != 0 or bool(self.artifacts)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='python -m tests.fuzz.smoke',
        description='Run every fuzz target (or the named ones) for a short campaign each.')
    parser.add_argument('--seconds', type=int, default=20,
                        help='libFuzzer -max_total_time per target (default 20)')
    parser.add_argument('--jobs', type=int, default=1,
                        help='targets run at once (default 1). More is faster, but '
                             'budgeted targets then share the CPU and a -timeout '
                             'set for an idle machine can trip on a busy one.')
    parser.add_argument('targets', nargs='*', metavar='target',
                        help='target names (default: every registered target)')
    args = parser.parse_args(argv)
    unknown = [name for name in args.targets if name not in REGISTRY]
    if unknown:
        parser.error(f'unknown target(s): {", ".join(unknown)}; '
                     f'known: {", ".join(sorted(REGISTRY))}')
    if args.seconds < 1:
        parser.error('--seconds must be at least 1')
    if args.jobs < 1:
        parser.error('--jobs must be at least 1')
    if not args.targets:
        args.targets = sorted(REGISTRY)
    return args


def artifact_dir(name: str) -> pathlib.Path:
    return SMOKE_ROOT / name


def campaign_command(name: str, seconds: int) -> list[str]:
    """The run_campaign invocation for one target. Paths are relative: it runs
    with the repository root as its working directory, as run_campaign expects."""
    return [sys.executable, '-m', 'tests.fuzz.run_campaign', name,
            f'-max_total_time={seconds}',
            f'-artifact_prefix={artifact_dir(name)}/']


def find_artifacts(directory: pathlib.Path) -> tuple[str, ...]:
    if not directory.is_dir():
        return ()
    return tuple(sorted(p.name for p in directory.iterdir()
                        if p.is_file() and p.name.startswith(FAILURE_PREFIXES)))


def last_done_line(log: str) -> str:
    matches = _DONE_LINE.findall(log)
    return matches[-1].strip() if matches else ''


def run_target(name: str, seconds: int, root: pathlib.Path = REPO_ROOT,
               run=subprocess.run) -> Result:
    """Run one target's campaign and read back what it left behind.

    `run` is a parameter so tests/test_fuzz_smoke.py can stand in for
    subprocess.run without starting libFuzzer.
    """
    directory = root / artifact_dir(name)
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    log_path = directory / 'campaign.log'
    with open(log_path, 'wb') as log:
        try:
            completed = run(campaign_command(name, seconds), cwd=root, stdout=log,
                            stderr=subprocess.STDOUT, timeout=seconds + GRACE_SECONDS)
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            returncode = None
    summary = last_done_line(log_path.read_text(encoding='utf-8', errors='replace'))
    return Result(name, returncode, find_artifacts(directory), summary)


def describe(result: Result) -> str:
    if not result.failed:
        return f'ok    {result.name}: {result.summary or "(no DONE line)"}'
    if result.returncode is None:
        why = f'did not finish within --seconds + {GRACE_SECONDS}s'
    elif result.artifacts:
        why = 'left ' + ', '.join(result.artifacts)
    else:
        why = f'exited {result.returncode}'
    if result.artifacts and result.returncode not in (0, None):
        why += f' (exited {result.returncode})'
    return f'FAIL  {result.name}: {why} -- see {artifact_dir(result.name)}/campaign.log'


def run_all(targets, seconds: int, jobs: int, runner=run_target) -> list[Result]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(runner, name, seconds) for name in targets]
        results = []
        for future in futures:
            result = future.result()
            print(describe(result), flush=True)
            results.append(result)
    return results


def main(argv=None, runner=run_target) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    print(f'smoke: {len(args.targets)} target(s), {args.seconds}s each, {args.jobs} at a time',
          flush=True)
    results = run_all(args.targets, args.seconds, args.jobs, runner=runner)
    failed = [r.name for r in results if r.failed]
    if failed:
        print(f'smoke: {len(failed)} of {len(results)} target(s) failed: {", ".join(failed)}')
        return 1
    print(f'smoke: all {len(results)} target(s) clean')
    return 0


if __name__ == '__main__':
    os.chdir(REPO_ROOT)
    sys.exit(main())
