"""The alpha API's front door: the switch that turns the whole API off, and
the handler that turns an exception into a response.

Sub-project 85, slice A -- `app/api/alpha/routes.py`'s gate and
`app/api/alpha/__init__.py`'s error handler. Two defects, both measured:

* ONE route of the 118 in that file skipped `enable_api()`:
  `/site/instance_chooser_search` checked only the instance chooser's own
  setting, so an instance that had turned the chooser on and the alpha API
  off answered that one endpoint anyway. Its sibling `/site/instance_chooser`
  checks both (D1248);
* and the reason for every refusal was thrown away. `flask_smorest`'s
  `abort()` stashes its `message` in `e.data`, the shared error handler read
  `str(e)` instead, and `str(e)` on a Werkzeug `BadRequest` is the generic
  "The browser (or proxy) sent a request that this server could not
  understand" -- which is what an operator saw in place of "alpha api is not
  enabled", for all 117 deliberate refusals in the file (D1249).

The first test here is a sweep over `app.url_map`, so a route added tomorrow
without the gate fails it without anyone having to remember to add a row.
"""
import pytest
from flask import current_app, g

from app import db
from app.api.alpha.routes import enable_api
from app.models import Site
from app.utils import set_setting


def alpha_routes(app, documented=True):
    """Every route under /api/alpha, one row per method.

    `documented` picks between the two kinds this module holds: the endpoints
    on the flask-smorest blueprints (Site, Community, Post, ...), which are
    the API, and the ones on the plain `api_alpha` blueprint, which are
    placeholders answering `not_yet_implemented` for paths Lemmy's V3 API has
    and this one does not. Only the first kind is gated.
    """
    found = set()
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if not path.startswith('/api/alpha'):
            continue
        is_placeholder = rule.endpoint.split('.')[0] == 'api_alpha'
        if is_placeholder == documented:
            continue
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            found.add((method, path))
    return sorted(found)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(client=app.test_client(), app=app,
                           reader=api_baseline.user3)


class TestTheSwitch:
    def test_no_route_succeeds_when_the_api_is_off(self, env):
        """D1248. One route of 118 skipped the gate, and this is the property
        that catches the next one: with the API off, NOTHING answers.

        Driven from `app.url_map` rather than a list, so a route added
        tomorrow without the gate fails this without anyone remembering to add
        a row. The request bodies here are empty, so many routes are refused by
        schema validation before the gate is reached -- that refusal is just as
        good, and the routes whose gate is reached are pinned by name below.
        """
        answered = []
        for method, path in alpha_routes(env.app):
            response = env.client.open(path, method=method, json={})
            if response.status_code < 400:
                answered.append(f'{method} {path} -> {response.status_code}')
        assert answered == []

    @pytest.mark.parametrize('method, path', [
        ('GET', '/api/alpha/site'),
        ('GET', '/api/alpha/site/version'),
        ('GET', '/api/alpha/site/instance_chooser'),
        ('GET', '/api/alpha/site/instance_chooser_search'),
        ('GET', '/api/alpha/federated_instances'),
        ('GET', '/api/alpha/post/list'),
        ('GET', '/api/alpha/post/list2'),
        ('GET', '/api/alpha/comment/list'),
        ('GET', '/api/alpha/community/list'),
        ('GET', '/api/alpha/feed/list'),
        ('GET', '/api/alpha/modlog'),
    ])
    def test_a_route_that_needs_nothing_says_why_it_refused(self, env, method,
                                                            path):
        """D1249. These are the routes a caller can reach without sending
        anything, so the gate is what answers -- and what it says is the
        operator's only clue. It used to say "The browser (or proxy) sent a
        request that this server could not understand"."""
        set_setting('enable_instance_chooser', True)
        response = env.client.open(path, method=method, json={})
        assert response.status_code == 400
        assert response.get_json()['message'] == 'alpha api is not enabled'

    def test_the_routes_are_all_there(self, env):
        """A guard on the guard: if the blueprints stopped registering, the
        sweep above would pass with nothing to sweep."""
        assert len(alpha_routes(env.app)) > 100

    def test_the_placeholders_answer_whatever_the_switch_says(self, env):
        """The Lemmy-V3 paths this API does not implement are NOT gated, and
        should not be: they say what they are, switch or no switch."""
        placeholders = alpha_routes(env.app, documented=False)
        assert len(placeholders) > 20
        for method, path in placeholders:
            response = env.client.open(path, method=method, json={})
            body = response.get_json(silent=True) or {}
            assert response.status_code == 400, f'{method} {path}'
            assert 'not_yet_implemented' in body.get('error', '') \
                or 'renamed' in body.get('error', ''), f'{method} {path}'

    def test_no_route_says_that_when_the_api_is_on(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        still_refusing = []
        for method, path in alpha_routes(env.app):
            response = env.client.open(path, method=method, json={})
            body = response.get_json(silent=True) or {}
            if body.get('message') == 'alpha api is not enabled':
                still_refusing.append(f'{method} {path}')
        assert still_refusing == []


class TestEnableApi:
    def test_the_config_string_true_turns_it_on(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        assert enable_api() is True

    @pytest.mark.parametrize('value', [False, 'false', 'True', '1', '', None])
    def test_anything_else_leaves_it_off(self, env, monkeypatch, value):
        """The comparison is against the STRING 'true', so every other
        spelling -- including the boolean True and the capitalised string --
        leaves the API off."""
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', value)
        assert enable_api() is False

    def test_debug_turns_it_on_whatever_the_config_says(self, env,
                                                        monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', False)
        monkeypatch.setitem(current_app.config, 'DEBUG', True)
        assert enable_api() is True


class TestTheInstanceChooser:
    """The pair that has two gates, and the one that used to have one."""

    @pytest.mark.parametrize('path', ['/api/alpha/site/instance_chooser',
                                      '/api/alpha/site/instance_chooser_search'])
    def test_the_api_switch_comes_first(self, env, path):
        """D1248. With the chooser ON and the API OFF, the search answered
        200 while its sibling refused."""
        set_setting('enable_instance_chooser', True)
        response = env.client.get(path)
        assert response.status_code == 400
        assert response.get_json()['message'] == 'alpha api is not enabled'

    @pytest.mark.parametrize('path', ['/api/alpha/site/instance_chooser',
                                      '/api/alpha/site/instance_chooser_search'])
    def test_the_chooser_switch_comes_second(self, env, path, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        set_setting('enable_instance_chooser', False)
        assert env.client.get(path).status_code == 404

    @pytest.mark.parametrize('path', ['/api/alpha/site/instance_chooser',
                                      '/api/alpha/site/instance_chooser_search'])
    def test_both_switches_on(self, env, path, monkeypatch):
        monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
        set_setting('enable_instance_chooser', True)
        assert env.client.get(path).status_code == 200


class TestTheErrorHandler:
    """`shared_error_handler`, which every alpha blueprint registers for
    `Exception`: there is no 500 in this API, only differently worded 400s."""

    @pytest.fixture
    def handler(self, app):
        from app.api.alpha import shared_error_handler
        return shared_error_handler

    def test_a_refusal_keeps_the_words_it_was_given(self, env, handler):
        """D1249. `flask_smorest.abort(400, message=...)` stashes the message
        in `e.data`; `str(e)` is Werkzeug's generic description."""
        from flask_smorest import abort
        from werkzeug.exceptions import HTTPException
        try:
            abort(400, message='a reason worth reading')
        except HTTPException as raised:
            response, status = handler(raised)
        assert status == 400
        assert response.get_json()['message'] == 'a reason worth reading'

    def test_a_refusal_with_no_message_falls_through(self, env, handler):
        from werkzeug.exceptions import NotFound
        response, status = handler(NotFound())
        assert status == 400
        assert 'Not Found' in response.get_json()['message']

    def test_a_rate_limit(self, env, handler):
        from flask_limiter import RateLimitExceeded
        from limits import parse

        class _Limit:
            limit = parse('1/minute')
            error_message = None
            scope = None

        response, status = handler(RateLimitExceeded(_Limit()))
        assert status == 429

    def test_a_row_that_is_not_there(self, env, handler):
        from sqlalchemy.orm.exc import NoResultFound
        response, status = handler(NoResultFound('nothing here'))
        assert status == 400
        assert response.get_json()['status'] == 'Not found'

    def test_bad_credentials(self, env, handler):
        response, status = handler(BlockingIOError('no'))
        assert status == 400
        assert response.get_json()['status'] == 'Bad credentials'

    def test_a_request_that_does_not_validate(self, env, handler):
        from werkzeug.exceptions import UnprocessableEntity
        failure = UnprocessableEntity()
        failure.data = {'messages': {'json': {'id': ['Missing.']}}}
        response, status = handler(failure)
        assert status == 400
        assert response.get_json()['message'] == 'Validation failed'

    def test_an_unexpected_exception_reaches_sentry_when_one_is_configured(
            self, env, handler, monkeypatch):
        from unittest.mock import patch
        monkeypatch.setitem(current_app.config, 'SENTRY_DSN',
                            'https://key@example.test/1')
        with patch('app.api.alpha.sentry_sdk.capture_exception') as captured:
            handler(RuntimeError('a genuine surprise'))
        captured.assert_called_once()

    def test_a_failed_validation_reaches_sentry_too(self, env, handler,
                                                    monkeypatch):
        from unittest.mock import patch
        from werkzeug.exceptions import UnprocessableEntity
        monkeypatch.setitem(current_app.config, 'SENTRY_DSN',
                            'https://key@example.test/1')
        failure = UnprocessableEntity()
        failure.data = {'messages': {'json': {'id': ['Missing.']}}}
        with patch('app.api.alpha.sentry_sdk.capture_exception') as captured:
            handler(failure)
        captured.assert_called_once()

    def test_anything_else_says_what_it_was(self, env, handler):
        response, status = handler(Exception('something went wrong'))
        assert status == 400
        assert response.get_json()['message'] == 'something went wrong'

    def test_an_incorrect_login_is_not_filed_as_an_application_error(
            self, env, handler):
        """Two messages are deliberately NOT logged or sent to Sentry: a
        rejected login and a missing object are ordinary answers."""
        with patch_logger() as logged:
            handler(Exception('incorrect_login'))
            handler(Exception('No object found.'))
        assert logged == []

    def test_anything_else_is(self, env, handler):
        """D537, fixed: a bare `Exception` is a deliberate refusal and is logged
        at info; only an internal error is logged as an exception."""
        with patch_logger() as logged:
            handler(Exception('access_denied'))
            handler(RuntimeError('a genuine surprise'))
        assert logged == ['API exception']


class patch_logger:
    """Records what `current_app.logger.exception` was called with."""

    def __enter__(self):
        from unittest.mock import patch
        self.calls = []
        self.patcher = patch.object(
            current_app.logger, 'exception',
            side_effect=lambda message, *a, **k: self.calls.append(message))
        self.patcher.start()
        return self.calls

    def __exit__(self, *exc):
        self.patcher.stop()
        return False
