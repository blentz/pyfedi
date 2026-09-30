"""`shared_error_handler`: what the alpha API tells a caller went wrong.

Registered on all eleven alpha blueprints (`app/api/alpha/__init__.py:143`) as
`errorhandler(Exception)`, so every uncaught exception in every API route comes
through here.

D1390. Its final arm returned `str(e)` to the caller. For the deliberate refusals
that is the API's contract -- the code raises bare
`Exception('incorrect_login')`, `Exception('access_denied')` and the like, and a
client is meant to read them. For a `SQLAlchemyError` it is not: that class's
`str()` carries the driver's message, the **full statement** and the **bound
parameters**. Measured, the body a client received:

    {"code":400,
     "message":"(psycopg2.errors.InvalidTextRepresentation) invalid input syntax
      for type boolean: \\"not-a-bool\\" ... [SQL: INSERT INTO \\"user\\"
      (user_name, banned) VALUES (%(n)s, %(b)s)] [parameters: {...}]",
     "status":"Bad Request"}

-- schema, table and column names and the query's shape, to any caller who can
provoke a database error. The exception is still logged and sent to Sentry; only
what crosses the wire changed.

D895, fixed (owner ruling 2026-09-30). D1390 was deliberately narrow; the ruling
widened it. Every exception that is not one of the API's deliberate refusals -- a
bare `Exception('...')`, a `PostReplyValidationError`, a marshmallow
`ValidationError` or an `HTTPException` -- is an internal error: logged in full,
and answered with a generic 500 `internal error`. A database error is one of them.

WHAT THIS ROUND SET OUT TO FIND, AND DID NOT. `page_cursor` is the one
`int(data[...])` key in the whole alpha API that no schema declares as
`fields.Integer` -- 54 of 55 are validated, and `unknown=INCLUDE` lets an
undeclared one through as a raw string. `/post/list?page_cursor=abc` looked like a
500 waiting to happen, and `/post/list2` even hands clients an opaque sqlakeyset
bookmark in `next_page` that would not parse. It answered 400, because of this very
handler. D895 made that `ValueError` a 500, so both listings now raise the API's
own refusal, `invalid page_cursor`, for a cursor that does not parse (D895
follow-up) and it is a 400 again.
"""
import pytest
from flask import current_app
from unittest.mock import patch

from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm.exc import NoResultFound
from werkzeug.exceptions import BadRequest, UnprocessableEntity

from app.api.alpha import shared_error_handler


def handled(app, exception):
    """Run the handler in a request context and return (status, body dict)."""
    with app.test_request_context('/api/alpha/post/list'):
        response, code = shared_error_handler(exception)
        return code, response.get_json()


def a_db_error():
    """A DataError shaped like the real one: SQLAlchemy puts the statement and
    the parameters into `str(e)` itself, so they are supplied here rather than
    faked into the message."""
    return DataError('INSERT INTO "user" (user_name, banned) VALUES (%(n)s, %(b)s)',
                     {'n': 'yyy', 'b': 'not-a-bool'},
                     Exception('invalid input syntax for type boolean'))


# --------------------------------------------------------------------------
# D1390
# --------------------------------------------------------------------------


class TestADatabaseError:
    def test_the_caller_is_told_nothing_about_the_query(self, app):
        """Since D895 a database error is answered as every internal error is."""
        code, body = handled(app, a_db_error())

        assert code == 500
        assert body['message'] == 'internal error'

    def test_the_statement_and_parameters_do_not_cross_the_wire(self, app):
        """The measured leak, asserted on the serialised body rather than on the
        message alone -- `status` and any future field must not carry it either."""
        import json

        code, body = handled(app, a_db_error())
        serialised = json.dumps(body)

        assert 'INSERT INTO' not in serialised
        assert 'user_name' not in serialised
        assert 'not-a-bool' not in serialised
        assert 'SQL' not in serialised
        assert 'parameters' not in serialised

    @pytest.mark.parametrize('exception', [
        DataError('SELECT secret FROM "user"', {}, Exception('boom')),
        IntegrityError('INSERT INTO "user" (email) VALUES (%(e)s)', {},
                       Exception('duplicate key')),
        SQLAlchemyError('a bare sqlalchemy error'),
    ])
    def test_every_sqlalchemy_error_is_generic(self, app, exception):
        code, body = handled(app, exception)

        assert code == 500
        assert body['message'] == 'internal error'

    def test_it_is_still_logged_and_reported(self, app):
        """The detail belongs in the log and in Sentry, not in the response. A fix
        that merely stopped logging would make the response safe and the defect
        invisible."""
        with patch.object(current_app.logger, 'exception') as logged:
            handled(app, a_db_error())

        assert logged.called


# --------------------------------------------------------------------------
# The contract the handler must keep
# --------------------------------------------------------------------------


class TestTheDeliberateRefusals:
    """Every one of these is a bare `Exception('...')` raised on purpose in
    `app/shared` or `app/api/alpha/utils`, and its text IS the API's answer."""

    @pytest.mark.parametrize('message', [
        'incorrect_login', 'access_denied', 'banned_from_community',
        'not implemented', 'is_instance_feed requires an admin account',
        'You cannot leave your own feed', 'community_not_found',
    ])
    def test_the_message_reaches_the_caller(self, app, message):
        code, body = handled(app, Exception(message))

        assert code == 400
        assert body['message'] == message

    @pytest.mark.parametrize('message', ['incorrect_login', 'No object found.'])
    def test_the_two_quiet_ones_are_not_logged(self, app, message):
        """These two are so routine that logging them is noise -- an explicit
        exemption in the handler, pinned so it is not lost."""
        with patch.object(current_app.logger, 'exception') as logged:
            handled(app, Exception(message))

        assert not logged.called

    def test_an_ordinary_exception_is_logged(self, app):
        with patch.object(current_app.logger, 'exception') as logged:
            handled(app, Exception('access_denied'))

        assert logged.called

    def test_a_post_reply_refusal_reaches_the_caller(self, app):
        """`PostReply.new` refuses with its own exception type, which reaches the
        API uncaught; its text is written for the user."""
        from app.models import PostReplyValidationError

        code, body = handled(app, PostReplyValidationError('Comments are disabled on this post'))

        assert code == 400
        assert body['message'] == 'Comments are disabled on this post'

    def test_a_plain_http_exception_keeps_its_text(self, app):
        from werkzeug.exceptions import NotFound

        code, body = handled(app, NotFound())

        assert code == 400
        assert body['message'] == str(NotFound())


class TestAnInternalError:
    """D895, fixed. Anything that is not a deliberate refusal is an internal
    error: its text may name a table, a column, a constraint or a path, so the
    caller gets a generic 500 and the detail goes to the log."""

    @pytest.mark.parametrize('exception', [
        ValueError("invalid literal for int() with base 10: 'abc'"),
        RuntimeError('relation "user_role" does not exist'),
        KeyError('secret_column'),
        AttributeError("'NoneType' object has no attribute 'ap_id'"),
    ])
    def test_the_caller_gets_a_generic_500(self, app, exception):
        code, body = handled(app, exception)

        assert code == 500
        assert body == {'code': 500, 'message': 'internal error',
                        'status': 'Internal Server Error'}

    def test_it_is_logged_in_full(self, app):
        with patch.object(current_app.logger, 'exception') as logged:
            handled(app, RuntimeError('relation "user_role" does not exist'))

        assert logged.called


class TestTheEarlierBranches:
    """Each of these is matched before the final arm, so the new isinstance test
    must not shadow them. `NoResultFound` matters most: it is itself a
    `SQLAlchemyError` subclass, and it has its own branch above."""

    def test_no_result_found_keeps_its_own_status(self, app):
        code, body = handled(app, NoResultFound('No row was found'))

        assert code == 400
        assert body['status'] == 'Not found'
        assert body['message'] != 'internal error'

    def test_a_blocking_io_error_is_bad_credentials(self, app):
        code, body = handled(app, BlockingIOError('too many attempts'))

        assert code == 400
        assert body['status'] == 'Bad credentials'

    def test_a_validation_failure_says_so(self, app):
        error = UnprocessableEntity()
        error.data = {'messages': {'json': {'page': ['Not a valid integer.']}}}

        code, body = handled(app, error)

        assert code == 400
        assert body['message'] == 'Validation failed'
        assert 'page' in body['status']

    def test_a_smorest_abort_keeps_its_reason(self, app):
        """flask_smorest's `abort()` stashes the reason in `e.data`, and all 117
        deliberate refusals in app/api/alpha/routes.py depend on this branch --
        without it they reached the caller as Werkzeug's generic description."""
        error = BadRequest()
        error.data = {'message': 'alpha api is not enabled'}

        code, body = handled(app, error)

        assert code == 400
        assert body['message'] == 'alpha api is not enabled'


class TestRateLimiting:
    def test_a_rate_limit_is_429(self, app):
        from flask_limiter import RateLimitExceeded

        class _Limit:
            error_message = None
            limit = '5 per 1 minute'

            def __str__(self):
                return self.limit

        code, body = handled(app, RateLimitExceeded(_Limit()))

        assert code == 429
