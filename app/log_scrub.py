"""Log records of an onion request carry no trace of who made it (onion unlinkability spec 2026-10-26, goals 9 and 10).

`OnionScrubFilter` rewrites the record itself, so every handler that sees it -- the journal and the `ERRORS_TO` mail,
which leaves the box through the mail provider -- sees the same scrubbed record. In a request that is an onion
request, the message becomes the exception class name (or `error on an onion request`), the arguments go, and the
traceback keeps only its frames: file, line, function, no source line and no exception message. Anything else passes
unchanged, so clearnet output and Celery workers (no request, no onion context) are as they were.
"""
import logging
import traceback

from flask import current_app

from app.onion import is_onion_request

ONION_EXCEPTION_MESSAGE = 'Exception on an onion request'
ONION_GENERIC_MESSAGE = 'error on an onion request'

# `extra=ONION_SAFE` marks a line whose text is fixed and names nothing (the onion store is down): it keeps its words.
ONION_SAFE = {'onion_safe': True}
_SCRUBBED = 'onion_scrubbed'


class OnionScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _SCRUBBED, False) or getattr(record, 'onion_safe', False) or not is_onion_request():
            return True
        error = record.exc_info[1] if record.exc_info else None
        kind = type(error).__name__ if error is not None else None
        if record.msg == ONION_EXCEPTION_MESSAGE:
            record.msg = f'{ONION_EXCEPTION_MESSAGE}: {kind}' if kind else ONION_EXCEPTION_MESSAGE
        else:
            record.msg = kind or ONION_GENERIC_MESSAGE
        record.args = ()
        record.exc_text = ''.join(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
            for frame in traceback.extract_tb(record.exc_info[2])).rstrip('\n') if error is not None else None
        record.exc_info = None
        record.stack_info = None
        setattr(record, _SCRUBBED, True)
        return True


scrub = OnionScrubFilter()


def log_onion_safe_exception(app):
    """Flask's own `Exception on <path> [<method>]` names the path and the method, so on an onion request it logs
    `Exception on an onion request` instead (the filter then keeps only the class and the frames). Clearnet is Flask's."""
    flask_log_exception = app.log_exception

    def log_exception(exc_info):
        if is_onion_request():
            app.logger.error(ONION_EXCEPTION_MESSAGE, exc_info=exc_info)
        else:
            flask_log_exception(exc_info)

    app.log_exception = log_exception


def log_ldap_sync_failure(user_name: str, error: Exception) -> None:
    """The directory sync of a profile or login failed. On the onion the line names neither the account nor the
    error text (which may name it)."""
    if is_onion_request():
        current_app.logger.error('LDAP sync failed', extra=ONION_SAFE)
    else:
        current_app.logger.error(f'LDAP sync failed for user {user_name}: {error}')
