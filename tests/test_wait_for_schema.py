"""The Celery workers and the notifs server wait for the schema the web container migrates.

The autodeploy restarts every unit at once and only the web container runs `flask db upgrade`. A worker that came
up first ran queued tasks on a half-migrated schema and eleven failed. These tests hold the guard
(wait_for_schema.py) to: current means the database's revisions equal the code's heads, an unreachable database
is "not yet", and the entrypoints run the guard before they start."""
import pathlib
import runpy

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

import wait_for_schema
from wait_for_schema import schema_is_current, wait_for_schema as wait

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = str(ROOT / 'migrations')


def _script():
    return ScriptDirectory(MIGRATIONS)


def _engine_holding(revisions):
    engine = create_engine('sqlite://')
    if revisions is not None:
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)'))
            for revision in revisions:
                connection.execute(text('INSERT INTO alembic_version VALUES (:r)'), {'r': revision})
    return engine


def _current(revisions):
    with _engine_holding(revisions).connect() as connection:
        return schema_is_current(connection, MIGRATIONS)


def test_no_alembic_version_table_is_not_current():
    assert _current(None) is False


def test_an_empty_alembic_version_table_is_not_current():
    assert _current([]) is False


def test_an_older_revision_is_not_current():
    heads = set(_script().get_heads())
    older = next(r.revision for r in _script().walk_revisions() if r.revision not in heads)
    assert _current([older]) is False


def test_the_code_heads_are_current():
    assert _current(_script().get_heads()) is True


def test_some_but_not_all_heads_is_not_current(monkeypatch):
    monkeypatch.setattr(ScriptDirectory, 'get_heads', lambda self: ['a', 'b'])
    assert _current(['a']) is False
    assert _current(['a', 'b']) is True


def test_a_database_revision_the_code_does_not_know_is_not_current():
    assert _current(list(_script().get_heads()) + ['not_a_revision']) is False


class _Connect:
    """A connect() that yields one scripted outcome per call: an exception to raise, or a bool for 'current'."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return _Stub(outcome)


class _Stub:
    def __init__(self, current):
        self.current = current

    def close(self):
        pass


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


def _wait(outcomes, timeout=10, interval=2):
    clock = _Clock()
    connect = _Connect(outcomes)
    result = wait(connect, lambda connection: connection.current, timeout=timeout, interval=interval,
                  sleep=clock.sleep, clock=clock)
    return result, clock, connect


def test_current_on_the_third_poll_returns_true_after_two_sleeps():
    result, clock, _ = _wait([False, False, True])
    assert result is True and clock.sleeps == [2, 2]


def test_current_at_once_never_sleeps():
    result, clock, _ = _wait([True])
    assert result is True and clock.sleeps == []


def test_never_current_returns_false_once_the_clock_passes_the_timeout():
    result, clock, connect = _wait([False], timeout=5, interval=2)
    assert result is False and clock.now >= 5 and connect.calls >= 3


def test_an_unreachable_database_keeps_waiting_then_succeeds():
    down = OperationalError('connect', {}, Exception('refused'))
    result, clock, _ = _wait([down, down, True])
    assert result is True and clock.sleeps == [2, 2]


def test_an_unreachable_database_for_the_whole_timeout_returns_false():
    down = OperationalError('connect', {}, Exception('refused'))
    result, _, _ = _wait([down], timeout=5)
    assert result is False


def test_the_connection_is_closed_after_each_check():
    class Connection:
        closed = 0

        def close(self):
            Connection.closed += 1

    connection = Connection()
    clock = _Clock()
    wait(lambda: connection, lambda c: Connection.closed >= 1, timeout=10, interval=1, sleep=clock.sleep, clock=clock)
    assert Connection.closed == 2


def test_the_connection_is_a_real_engine_connection(tmp_path):
    engine = _engine_holding(_script().get_heads())
    assert wait(engine.connect, lambda c: schema_is_current(c, MIGRATIONS), timeout=1, interval=0.01) is True


@pytest.mark.parametrize('revisions,expected', [(None, 1), ('heads', 0)])
def test_main_exits_zero_when_current_and_one_on_timeout(monkeypatch, revisions, expected):
    heads = list(_script().get_heads())
    engine = _engine_holding(heads if revisions == 'heads' else None)
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.setenv('SCHEMA_WAIT_TIMEOUT', '0')
    monkeypatch.setattr(wait_for_schema, 'create_engine', lambda url: engine)
    assert wait_for_schema.main() == expected


def test_main_defaults_to_a_ten_minute_timeout(monkeypatch):
    seen = {}

    def fake_wait(connect, check, timeout, interval, **kwargs):
        seen.update(timeout=timeout, interval=interval)
        return True

    monkeypatch.delenv('SCHEMA_WAIT_TIMEOUT', raising=False)
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.setattr(wait_for_schema, 'wait_for_schema', fake_wait)
    assert wait_for_schema.main() == 0
    assert seen == {'timeout': 600, 'interval': 2}


def test_run_as_a_script_exits_one_when_the_schema_never_matches(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.setenv('SCHEMA_WAIT_TIMEOUT', '0')
    with pytest.raises(SystemExit) as exit_:
        runpy.run_path(str(ROOT / 'wait_for_schema.py'), run_name='__main__')
    assert exit_.value.code == 1


@pytest.mark.parametrize('script', ['entrypoint_celery.sh', 'entrypoint_async.sh'])
def test_the_waiting_entrypoints_run_the_guard_before_they_start(script):
    text_ = (ROOT / script).read_text()
    guard = text_.index('python wait_for_schema.py')
    start = text_.index('exec celery' if script == 'entrypoint_celery.sh' else 'uvicorn fastapi_server')
    assert guard < start
    line = text_[guard:text_.index('\n', guard)]
    assert line == ('python wait_for_schema.py' if script == 'entrypoint_async.sh' else 'python wait_for_schema.py || exit 1')


def test_the_web_entrypoint_does_not_wait():
    assert 'wait_for_schema' not in (ROOT / 'entrypoint.sh').read_text()
