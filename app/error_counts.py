"""Daily error counts with nothing personal in them (onion unlinkability spec 2026-10-26, goal 5 and section 5).

The journal keeps an hour, so "what failed this week" cannot come from it. `ErrorCountHandler` counts each WARNING-or-worse
record per (day, unit, kind, level) in the `error_count` table, kept 21 days. The kind is the exception class and the
innermost `app/` file and line, or just the call site: never the message, the arguments, a path, an address or a name.

A leaf module: `app.log_scrub` imports it, so it imports nothing from the app beyond `app.clock`.
"""
import logging
import os
import threading
import traceback

from sqlalchemy import text

from app.clock import utcnow

KEEP_DAYS = 21
KIND_LENGTH = 256

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_APP_DIR)

_UPSERT = text(
    'INSERT INTO error_count (day, unit, kind, level, count) VALUES (:day, :unit, :kind, :level, 1) '
    'ON CONFLICT (day, unit, kind, level) DO UPDATE SET count = error_count.count + 1')

_guard = threading.local()


def _relative(path: str) -> str:
    return os.path.relpath(os.path.abspath(path), _ROOT)


def error_kind(record: logging.LogRecord) -> str:
    """What failed and where, never what it said. `OnionScrubFilter` stamps `error_kind` on the record before it removes
    the exception, so an onion request is counted under its real frame."""
    stamped = getattr(record, 'error_kind', None)
    if stamped:
        return stamped
    if record.exc_info and record.exc_info[1] is not None:
        frames = [frame for frame in traceback.extract_tb(record.exc_info[2])
                  if os.path.abspath(frame.filename).startswith(_APP_DIR + os.sep)]
        if frames:
            return f'{type(record.exc_info[1]).__name__} at {_relative(frames[-1].filename)}:{frames[-1].lineno}'
    return f'{_relative(record.pathname)}:{record.lineno}'


class ErrorCountHandler(logging.Handler):
    """Counts a record with its own connection (never `db.session`, which may be mid-transaction, or the one that
    failed). It never raises, and never counts a record logged while it is counting."""

    def __init__(self, engine, level=logging.WARNING):
        super().__init__(level)
        self.engine = engine

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_guard, 'busy', False):
            return
        _guard.busy = True
        try:
            with self.engine.begin() as connection:
                connection.execute(_UPSERT, {'day': utcnow().date(), 'unit': os.environ.get('PIEFED_UNIT') or 'web',
                                             'kind': error_kind(record)[:KIND_LENGTH], 'level': record.levelname})
        except Exception:
            pass    # counting is best effort; a failing count must never fail or slow the request
        finally:
            _guard.busy = False
