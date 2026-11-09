"""The test runners' two owner-ruled behaviours (2026-11-09): a whole-suite run leaves the `serial` tests out of its
parallel phase and runs them alone afterwards, and a run that crashed reruns its failed tests once, alone. Read from
the scripts themselves, as tests/test_parallel_workers.py reads run_tests.sh's worker setup."""
import ast
import configparser
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _script(name: str) -> str:
    return (ROOT / name).read_text(encoding='utf8')


def test_serial_is_a_declared_marker():
    config = configparser.ConfigParser()
    config.read(ROOT / 'pytest.ini')
    assert any(line.strip().startswith('serial:') for line in config['pytest']['markers'].splitlines())


def test_the_parallel_phase_leaves_the_serial_tests_out_and_they_run_after_it():
    script = _script('run_tests.sh')
    parallel = script.index("-m 'not serial'")
    serial = script.index('serial_args=(-m serial)')
    assert parallel < serial
    assert 'serial_args+=(--cov-append)' in script   # one coverage report of the whole suite


def _builds_indexes_concurrently(migration: str) -> bool:
    return bool(re.search(r'CONCURRENTLY|autocommit_block', (ROOT / 'migrations/versions' / migration).read_text()))


def _is_serial(module: Path) -> bool:
    tree = ast.parse(module.read_text(encoding='utf8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'pytestmark' for t in node.targets):
            return 'serial' in ast.unparse(node.value)
    return False


def test_every_test_that_runs_a_concurrent_index_build_is_serial():
    """A migration test that runs its upgrade or downgrade for real does the build on the shared server."""
    unmarked = []
    for module in sorted((ROOT / 'tests').glob('test_migration_*.py')):
        text = module.read_text(encoding='utf8')
        named = re.findall(r"'([0-9a-f]{12}_[a-z0-9_]+\.py)'", text)
        runs_it = re.search(r'migration\.(upgrade|downgrade)\b', text)
        if runs_it and any(_builds_indexes_concurrently(name) for name in named) and not _is_serial(module):
            unmarked.append(module.name)
    assert unmarked == []


def test_a_crashed_run_reruns_its_failed_tests_once_alone():
    script = _script('run_tests_remote.sh')
    crash = script.index('A CRASH during this run')
    retry = script.index('PYTEST_WORKERS=1 ./run_tests.sh')
    assert crash < retry
    assert "grep -E '^(FAILED|ERROR) tests/'" in script and 'retry_args+=(--cov-append)' in script
    assert 'The crash stays on record' in script   # retried, never hidden
