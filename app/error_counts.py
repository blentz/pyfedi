"""Daily error counts with nothing personal in them (onion unlinkability spec 2026-10-26, goal 5 and section 5).

The journal keeps an hour, so "what failed this week" cannot come from it. `ErrorCountHandler` counts each WARNING-or-worse
record per (day, unit, kind, level) in the `error_count` table, kept 21 days. The kind is the exception class and the
innermost `app/` file and line, or just the call site: never the message, the arguments, a path, an address or a name.

A leaf module: `app.log_scrub` imports it, so it imports nothing from the app beyond `app.clock`.
"""
import atexit
import logging
import os
import sys
import threading
import time
import traceback
import weakref

from celery.signals import celeryd_init
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.clock import utcnow

KEEP_DAYS = 21
KIND_LENGTH = 256
FLUSH_SECONDS = 60
REPORT_SECONDS = 600
WORKER_UNIT = 'celery'

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_APP_DIR)

_UPSERT = text(
    'INSERT INTO error_count (day, unit, kind, level, count) VALUES (:day, :unit, :kind, :level, :n) '
    'ON CONFLICT (day, unit, kind, level) DO UPDATE SET count = error_count.count + :n')

_guard = threading.local()


@celeryd_init.connect
def name_the_worker_unit(**kwargs) -> None:
    """A Celery worker counts under the unit `celery` unless the deploy named one (PIEFED_UNIT)."""
    os.environ.setdefault('PIEFED_UNIT', WORKER_UNIT)


def flush_engine(database_url: str):
    """The flusher's own engine: no pool shared with requests, and a connect that gives up after 2 seconds."""
    return create_engine(database_url, poolclass=NullPool, connect_args={'connect_timeout': 2})


def _relative(path: str) -> str:
    return os.path.relpath(os.path.abspath(path), _ROOT)


def _exception_kind(exc_info):
    """`<Class> at <app-relative path>:<line of the innermost app/ frame>`, or None when no frame is in app/."""
    frames = [frame for frame in traceback.extract_tb(exc_info[2])
              if os.path.abspath(frame.filename).startswith(_APP_DIR + os.sep)]
    if not frames:
        return None
    return f'{type(exc_info[1]).__name__} at {_relative(frames[-1].filename)}:{frames[-1].lineno}'


def error_kind(record: logging.LogRecord) -> str:
    """What failed and where, never what it said. `OnionScrubFilter` stamps `error_kind` on the record before it removes
    the exception, so an onion request is counted under its real frame."""
    stamped = getattr(record, 'error_kind', None)
    if stamped:
        return stamped
    if record.exc_info and record.exc_info[1] is not None:
        kind = _exception_kind(record.exc_info)
        if kind:
            return kind
    return f'{_relative(record.pathname)}:{record.lineno}'


class ErrorCountHandler(logging.Handler):
    """Counts records in memory; a daemon thread writes the counts to the database in batches.

    `emit` never touches the database: it runs under the handler lock, on the request's thread, and a warning flood
    must neither serialise requests nor take their connections. The flusher uses its own engine (`flush_engine`) and
    drops a batch it cannot write. The thread starts on the first record in each process, so a forked child (gunicorn,
    Celery prefork) starts with an empty dict and its own thread, and the parent's counts are written only once."""

    def __init__(self, engine, level=logging.WARNING, interval=FLUSH_SECONDS):
        super().__init__(level)
        self.engine = engine
        self.interval = interval
        self._counts = {}
        self._counts_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._stop = threading.Event()
        self._pid = None
        self._last_report = None
        atexit.register(self.flush)
        me = weakref.ref(self)    # a callback cannot be unregistered, so it holds the handler weakly
        os.register_at_fork(after_in_child=lambda: me() and me()._after_fork())

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_guard, 'busy', False):
            return
        _guard.busy = True
        try:
            key = (utcnow().date(), os.environ.get('PIEFED_UNIT') or 'web', error_kind(record)[:KIND_LENGTH],
                   record.levelname)
            self._start_in_this_process()
            with self._counts_lock:
                self._counts[key] = self._counts.get(key, 0) + 1
        except Exception:
            pass    # counting is best effort; a failing count must never fail or slow the request
        finally:
            _guard.busy = False

    def _start_in_this_process(self) -> None:
        if self._pid == os.getpid():
            return
        with self._start_lock:
            if self._pid == os.getpid():
                return
            self._pid = os.getpid()
            threading.Thread(target=self._run, daemon=True, name='error-count-flusher').start()

    def _after_fork(self) -> None:
        """In a forked child: forget what the parent counted (it writes those itself), replace locks the fork may have
        copied held, and let the first record start this process's own flusher."""
        self._counts = {}
        self._counts_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._pid = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.flush()

    def flush(self) -> None:
        """Write the counts so far (adding to what is stored) and forget them. A batch that cannot be written is
        dropped, with one stderr line per 10 minutes (not a log record: that could count itself)."""
        with self._counts_lock:
            counts, self._counts = self._counts, {}
        if not counts:
            return
        try:
            rows = [{'day': day, 'unit': unit, 'kind': kind, 'level': level, 'n': n}
                    for (day, unit, kind, level), n in counts.items()]
            with self.engine.begin() as connection:
                connection.execute(_UPSERT, rows)
        except Exception as error:
            now = time.monotonic()
            if self._last_report is None or now - self._last_report >= REPORT_SECONDS:
                self._last_report = now
                print(f'error counts: dropped {sum(counts.values())} counts, the database refused them '
                      f'({type(error).__name__})', file=sys.stderr)

    def close(self) -> None:
        self._stop.set()
        self.flush()
        atexit.unregister(self.flush)
        super().close()

    def task_failed(self, sender=None, exception=None, traceback=None, **kwargs) -> None:
        """Celery's `task_failure`, sent once per failed task: counted by the exception class and the task's frame, or
        the task's name when no frame is in app/."""
        exc_info = (type(exception), exception, traceback)
        name = getattr(sender, 'name', 'task')
        record = logging.LogRecord('celery.task', logging.ERROR, name, 0, name, (), exc_info)
        record.error_kind = _exception_kind(exc_info) or f'{type(exception).__name__} in {name}'
        self.handle(record)
