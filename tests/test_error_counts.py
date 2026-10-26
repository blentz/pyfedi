"""Daily error counts that name nothing and no one, kept 21 days (project 5, task 5; spec goal 5, section 5
"Daily counts"). The journal keeps an hour; this is what a week-long log search used to be."""
import logging
import os
import re
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

import app as app_pkg
from app import cli, create_app, db
from app.clock import utcnow
from app.error_counts import ErrorCountHandler, error_kind
from app.models import ErrorCount
from tests.conftest import TestConfig
from tests.test_onion_logging import onion, onion_context   # noqa: F401  (the fixture and the request context)

APP_DIR = os.path.dirname(os.path.abspath(app_pkg.__file__))
SECRET = 'alice-secret-name'
URL = 'https://example.org/u/alice-secret-name?token=hunter2'


def raise_from_app_file(message='boom', name='fake_module.py'):
    """Raise from a frame whose file lives in app/, so the innermost app/ frame is a known file:line."""
    exec(compile(f'\n\nraise ValueError({message!r})', os.path.join(APP_DIR, name), 'exec'), {})


def counted():
    db.session.expire_all()
    return {(c.kind, c.level, c.unit): c.count for c in ErrorCount.query.all()}


@pytest.fixture
def cli_app(app, db_session):
    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    return app


@pytest.fixture
def log(app, db_session):
    """The app's logger with the counting handler create_app attaches in production (TestConfig leaves it off)."""
    handler = ErrorCountHandler(db.engine)
    app.logger.addHandler(handler)
    yield app.logger
    app.logger.removeHandler(handler)


def test_kind_has_no_message_ip_path_or_name(log):
    log.warning('auth: login failed ip=203.0.113.7')
    try:
        raise_from_app_file(f'no user {SECRET} at {URL}')
    except ValueError:
        log.error('whatever %s', URL, exc_info=True)
    kinds = {kind for kind, _, _ in counted()}
    assert 'ValueError at app/fake_module.py:3' in kinds
    [call_site] = kinds - {'ValueError at app/fake_module.py:3'}
    assert re.fullmatch(r'tests/test_error_counts\.py:\d+', call_site)
    for kind in kinds:
        assert not any(bad in kind for bad in ('203.0.113.7', SECRET, 'example.org', 'hunter2', 'login failed'))


def test_kind_without_an_exception_is_the_call_site():
    record = logging.LogRecord('app', logging.WARNING, os.path.join(APP_DIR, 'foo.py'), 42, 'ip=203.0.113.7', (), None)
    assert error_kind(record) == 'app/foo.py:42'


def test_kind_with_an_exception_but_no_app_frame_is_the_call_site():
    try:
        raise KeyError(SECRET)
    except KeyError:
        record = logging.LogRecord('app', logging.ERROR, os.path.join(APP_DIR, 'foo.py'), 7, 'm', (), sys.exc_info())
    assert error_kind(record) == 'app/foo.py:7'


def test_kind_uses_the_innermost_app_frame():
    try:
        raise_from_app_file()
    except ValueError:
        record = logging.LogRecord('app', logging.ERROR, os.path.join(APP_DIR, 'foo.py'), 7, 'm', (), sys.exc_info())
    assert error_kind(record) == 'ValueError at app/fake_module.py:3'


def test_kind_is_cut_to_256_characters(log):
    record = logging.LogRecord('app', logging.WARNING, os.path.join(APP_DIR, 'x' * 400 + '.py'), 1, 'm', (), None)
    log.handle(record)
    [(kind, _, _)] = counted()
    assert len(kind) == 256


def test_same_kind_twice_counts_two(log):
    for _ in range(2):
        log.critical('words that differ are not part of the kind')
    [((kind, level, unit), count)] = counted().items()
    assert (level, unit, count) == ('CRITICAL', 'web', 2)


def test_unit_comes_from_piefed_unit(log, monkeypatch):
    monkeypatch.setenv('PIEFED_UNIT', 'worker')
    log.warning('x')
    assert {unit for _, _, unit in counted()} == {'worker'}


def test_info_lines_are_not_counted(log):
    log.info('quiet')
    log.debug('quieter')
    assert counted() == {}


def test_handler_never_raises_when_the_db_is_down(log):
    def down():
        raise ConnectionError('the database is down')
    handler = ErrorCountHandler(SimpleNamespace(begin=down))
    record = logging.LogRecord('app', logging.ERROR, 'app/x.py', 1, 'm', (), None)
    handler.handle(record)    # returns, raises nothing, and does not wait


def test_handler_does_not_reenter_on_its_own_failure():
    calls = []
    logger = logging.getLogger('app.error_counts_test')
    logger.propagate = False

    def failing_begin():
        calls.append(1)
        logger.warning('the connection failed')    # a log line from inside the handler's own failure
        raise ConnectionError('down')

    handler = ErrorCountHandler(SimpleNamespace(begin=failing_begin))
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        logger.warning('first')
    finally:
        logger.removeHandler(handler)
    assert calls == [1]


def test_prune_deletes_older_than_21_days_only(cli_app):
    today = utcnow().date()
    for age in (22, 21, 20, 0):
        db.session.add(ErrorCount(day=today - timedelta(days=age), unit='web', kind=f'age {age}', level='ERROR',
                                  count=1))
    db.session.commit()
    result = cli_app.test_cli_runner().invoke(args=['prune-error-counts'])
    assert result.exit_code == 0, result.output
    assert sorted(c.kind for c in ErrorCount.query.all()) == ['age 0', 'age 20', 'age 21']


def test_error_counts_cli_prints_sorted(cli_app):
    today = utcnow().date()
    older = today - timedelta(days=3)
    db.session.add_all([
        ErrorCount(day=today, unit='web', kind='b at app/b.py:2', level='ERROR', count=5),
        ErrorCount(day=older, unit='worker', kind='a at app/a.py:1', level='WARNING', count=2),
        ErrorCount(day=today, unit='web', kind='a at app/a.py:1', level='ERROR', count=1),
        ErrorCount(day=today - timedelta(days=30), unit='web', kind='too old', level='ERROR', count=9),
    ])
    db.session.commit()
    result = cli_app.test_cli_runner().invoke(args=['error-counts', '--days', '7'])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f'{older} worker WARNING 2 a at app/a.py:1',
        f'{today} web ERROR 1 a at app/a.py:1',
        f'{today} web ERROR 5 b at app/b.py:2',
    ]


def test_error_counts_cli_defaults_to_21_days(cli_app):
    today = utcnow().date()
    db.session.add_all([ErrorCount(day=today - timedelta(days=21), unit='web', kind='in', level='ERROR', count=1),
                        ErrorCount(day=today - timedelta(days=22), unit='web', kind='out', level='ERROR', count=1)])
    db.session.commit()
    result = cli_app.test_cli_runner().invoke(args=['error-counts'])
    assert result.output.splitlines() == [f'{today - timedelta(days=21)} web ERROR 1 in']


def test_an_onion_exception_is_counted_at_its_app_frame_not_in_log_scrub(app, onion, log):
    try:
        raise_from_app_file(f'no such user {SECRET}', 'fake_onion_module.py')
    except ValueError:
        with onion_context(app, '/some/path?token=hunter2'):
            app.log_exception(sys.exc_info())
    kinds = {kind for kind, _, _ in counted()}
    assert kinds == {'ValueError at app/fake_onion_module.py:3'}


def test_error_kind_keeps_one_already_stamped():
    record = logging.LogRecord('app', logging.ERROR, 'app/x.py', 1, 'm', (), None)
    record.error_kind = 'Stamped at app/y.py:9'
    assert error_kind(record) == 'Stamped at app/y.py:9'


def test_create_app_attaches_one_handler_even_when_run_twice(app):
    class Counting(TestConfig):
        ERROR_COUNTS = True

    logger = logging.getLogger('app')
    before = list(logger.handlers)
    try:
        create_app(Counting)
        create_app(Counting)
        assert sum(isinstance(h, ErrorCountHandler) for h in logger.handlers) == 1
    finally:
        logger.handlers[:] = before


def test_a_test_app_has_no_handler_unless_it_asks(app):
    assert not any(isinstance(h, ErrorCountHandler) for h in logging.getLogger('app').handlers)
