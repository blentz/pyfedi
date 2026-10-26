"""Daily error counts that name nothing and no one, kept 21 days (project 5, task 5; spec goal 5, section 5
"Daily counts"). The journal keeps an hour; this is what a week-long log search used to be."""
import gc
import logging
import os
import re
import threading
import time
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

import app as app_pkg
from unittest.mock import patch

from celery.signals import celeryd_init, task_failure

from app import cli, create_app, db
from app.clock import utcnow
from app.error_counts import ErrorCountHandler, error_kind, flush_engine, name_the_worker_unit
from app.models import ErrorCount
from tests.conftest import TestConfig
from tests.test_onion_logging import onion, onion_context   # noqa: F401  (the fixture and the request context)

APP_DIR = os.path.dirname(os.path.abspath(app_pkg.__file__))
SECRET = 'alice-secret-name'
URL = 'https://example.org/u/alice-secret-name?token=hunter2'


class ATask:
    name = 'app.shared.tasks.x'


def raise_from_app_file(message='boom', name='fake_module.py'):
    """Raise from a frame whose file lives in app/, so the innermost app/ frame is a known file:line."""
    exec(compile(f'\n\nraise ValueError({message!r})', os.path.join(APP_DIR, name), 'exec'), {})


def counted():
    for handler in logging.getLogger('app').handlers:
        if isinstance(handler, ErrorCountHandler):
            handler.flush()
    db.session.expire_all()
    return {(c.kind, c.level, c.unit): c.count for c in ErrorCount.query.all()}


@pytest.fixture
def cli_app(app, db_session):
    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    return app


@pytest.fixture
def log(app, db_session):
    """The app's logger with the counting handler create_app attaches in production (TestConfig leaves it off)."""
    handler = ErrorCountHandler(flush_engine(app.config['SQLALCHEMY_DATABASE_URI']))
    app.logger.addHandler(handler)
    yield app.logger
    app.logger.removeHandler(handler)
    handler.close()


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


def a_handler(engine, **kwargs):
    handler = ErrorCountHandler(engine, **kwargs)
    return handler


def a_record(level=logging.ERROR):
    return logging.LogRecord('app', level, os.path.join(APP_DIR, 'x.py'), 1, 'm', (), None)


def test_emit_never_touches_the_database(capsys):
    release = threading.Event()
    reached = []

    def blocked():
        reached.append(1)
        release.wait(5)
        raise ConnectionError('the database is down')

    handler = a_handler(SimpleNamespace(begin=blocked))
    started = time.monotonic()
    for _ in range(50):
        handler.handle(a_record())
    assert time.monotonic() - started < 1
    assert reached == []
    release.set()
    handler.close()
    assert reached == [1]    # only the flush reaches the database
    assert 'dropped 50 counts' in capsys.readouterr().err


def test_emit_swallows_an_error_of_its_own():
    handler = a_handler(None)
    with patch('app.error_counts.error_kind', side_effect=RuntimeError('boom')):
        handler.handle(a_record())    # raises nothing
    assert handler._counts == {}
    handler.close()


def test_handler_does_not_reenter_on_its_own_failure():
    logger = logging.getLogger('app.error_counts_test')
    logger.propagate = False
    handler = a_handler(None)
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)

    def noisy(record):
        logger.warning('a line logged while counting')    # must not be counted, nor recurse
        return 'k'

    try:
        with patch('app.error_counts.error_kind', noisy):
            logger.warning('first')
    finally:
        logger.removeHandler(handler)
    assert list(handler._counts.values()) == [1]
    handler.close()


def test_counts_accumulate_and_one_flush_writes_them_summed(log):
    for _ in range(3):
        log.error('x')
    log.warning('y')
    assert sorted(counted().values()) == [1, 3]


def test_two_flushes_add_up(log):
    for _ in range(3):
        log.error('x')
        counted()
    assert list(counted().values()) == [3]


def test_a_failed_flush_drops_the_batch_and_says_so_once(capsys):
    def down():
        raise ConnectionError('password=hunter2')

    handler = a_handler(SimpleNamespace(begin=down))
    for _ in range(2):
        handler.handle(a_record())
        handler.flush()
    assert handler._counts == {}
    err = capsys.readouterr().err
    assert err.count('error counts: dropped') == 1
    assert 'hunter2' not in err
    handler._last_report -= 601    # ten minutes later it says so again
    handler.handle(a_record())
    handler.flush()
    assert 'error counts: dropped' in capsys.readouterr().err
    handler.close()


def test_a_forked_child_starts_empty_with_its_own_flusher_and_locks():
    handler = a_handler(SimpleNamespace(begin=None), interval=3600)
    handler.handle(a_record())
    held = handler._counts_lock
    held.acquire()    # as if another thread of the parent held it at fork time
    with patch('app.error_counts.os.register_at_fork') as register:
        late = a_handler(None)
    child_reset = register.call_args.kwargs['after_in_child']
    late.handle(a_record())
    child_reset()
    assert late._counts == {} and late._pid is None
    handler._after_fork()    # the callback the handler registered, called as the child would
    assert handler._counts == {} and handler._pid is None and handler._counts_lock is not held
    with patch('app.error_counts.threading.Thread') as thread:
        handler.handle(a_record(logging.CRITICAL))
    assert [level for (_, _, _, level) in handler._counts] == ['CRITICAL']
    thread.return_value.start.assert_called_once()
    handler.close()
    late.close()


def test_the_at_fork_callback_does_nothing_once_its_handler_is_gone():
    with patch('app.error_counts.os.register_at_fork') as register:
        handler = a_handler(None)
    callback = register.call_args.kwargs['after_in_child']
    handler.close()
    del handler
    gc.collect()
    callback()    # raises nothing


def test_two_first_records_race_for_one_flusher():
    handler = a_handler(None)

    class Rival:
        def __enter__(self):
            handler._pid = os.getpid()    # another thread won while this one waited for the lock

        def __exit__(self, *args):
            return False

    handler._start_lock = Rival()
    with patch('app.error_counts.threading.Thread') as thread:
        handler.handle(a_record())
    thread.assert_not_called()
    handler.close()


def test_the_flusher_thread_writes_every_interval(app):
    written = threading.Event()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            written.set()

        def execute(self, statement, rows):
            self.rows = rows

    handler = a_handler(SimpleNamespace(begin=Connection), interval=0.01)
    handler.handle(a_record())
    assert written.wait(5)
    handler.close()


def test_the_handler_flushes_at_exit_and_not_after_close():
    with patch('app.error_counts.atexit') as atexit_module:
        handler = a_handler(None)
        atexit_module.register.assert_called_once_with(handler.flush)
        handler.close()
        atexit_module.unregister.assert_called_once_with(handler.flush)


def test_the_worker_unit_is_celery_unless_the_deploy_named_one():
    with patch.dict(os.environ):
        os.environ.pop('PIEFED_UNIT', None)
        celeryd_init.send(sender='worker@host')
        assert os.environ['PIEFED_UNIT'] == 'celery'
    with patch.dict(os.environ, {'PIEFED_UNIT': 'celery-send'}):
        name_the_worker_unit()
        assert os.environ['PIEFED_UNIT'] == 'celery-send'


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


@pytest.fixture
def counting_app(app, db_session):
    """create_app as production runs it, with the counting handler on; the shared logger and signal are restored."""
    class Counting(TestConfig):
        ERROR_COUNTS = True

    logger = logging.getLogger('app')
    before = list(logger.handlers)
    created = create_app(Counting)
    yield created
    for handler in list(logger.handlers):
        if handler not in before:
            logger.removeHandler(handler)
            handler.close()
    task_failure.disconnect(dispatch_uid='error_counts')


def counting_handlers():
    return [h for h in logging.getLogger('app').handlers if isinstance(h, ErrorCountHandler)]


def test_create_app_attaches_one_handler_even_when_run_twice(counting_app):
    class Counting(TestConfig):
        ERROR_COUNTS = True

    create_app(Counting)
    assert len(counting_handlers()) == 1


def test_a_real_warning_through_the_production_path_is_counted(counting_app):
    counting_app.logger.warning('nothing personal here, ip=203.0.113.7')
    [handler] = counting_handlers()
    handler.flush()
    [row] = ErrorCount.query.all()
    assert (row.unit, row.level, row.count) == ('web', 'WARNING', 1)
    assert re.fullmatch(r'tests/test_error_counts\.py:\d+', row.kind)


def test_a_task_failure_is_counted_once(counting_app):
    try:
        raise_from_app_file('secret')
    except ValueError as error:
        task_failure.send(sender=ATask(), exception=error,
                          traceback=error.__traceback__)
    [handler] = counting_handlers()
    handler.flush()
    [row] = ErrorCount.query.all()
    assert (row.kind, row.level, row.count) == ('ValueError at app/fake_module.py:3', 'ERROR', 1)


def test_a_task_failure_with_no_app_frame_is_counted_under_the_task_name(counting_app):
    task_failure.send(sender=ATask(), exception=KeyError('secret'), traceback=None)
    [handler] = counting_handlers()
    handler.flush()
    [row] = ErrorCount.query.all()
    assert row.kind == 'KeyError in app.shared.tasks.x'


def test_a_test_app_has_no_handler_unless_it_asks(app):
    assert not any(isinstance(h, ErrorCountHandler) for h in logging.getLogger('app').handlers)
