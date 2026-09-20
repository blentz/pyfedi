"""app/errors/handlers.py, plus two modules a single statement from complete.

MEASUREMENT BASIS, from the full-suite --cov=app run at 5d68164e1:

    app/errors/handlers.py   81.481   missing [23, 31, 32, 43]   arcs [[22, 23]]
    app/admin/constants.py   90.0     missing [13]
    app/nntp/__init__.py      0.0     missing [1, 3]

One defect is pinned here and repaired with it:

  P1  the ordinary 404 returned the nine-byte string 'not found', while the
      branch that exists to SKIP the pretty page -- static files, API paths,
      asset extensions -- rendered errors/404.html. Backwards: a person
      mistyping a URL is the one case where the rendered page is worth having.

REACHING THE OTHER THREE HANDLERS. They only run when a request fails, and the
suite has no failing routes. Two things block the obvious approaches, both
measured:

  * `app.route` cannot be called after the first request -- the `app` fixture is
    session-scoped, so a test cannot add a route that raises.
  * `app.handle_http_exception()` from a `test_request_context` skips the
    before_request hook that sets `g.site`, and errors/401.html and
    errors/500.html extend base.html, which needs it:
    UndefinedError: 'flask.ctx._AppCtxGlobals object' has no attribute 'site'.

So an EXISTING route is made to fail: patch the render_template it uses with a
side_effect and drop PROPAGATE_EXCEPTIONS for the call. The whole lifecycle
runs, which is the point. Fact 328.
"""
import pytest
from unittest.mock import patch
from sqlalchemy import text
from werkzeug.exceptions import Unauthorized, TooManyRequests

from app import db
from app.models import CmsPage, Site, Tag
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def _seed():
    """An open instance with the Site row the error templates read through
    g.site.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, burn


def _cms_page(url='/about-us', title='About us'):
    page = CmsPage(url=url, title=title, body='hello', body_html='<p>hello</p>')
    db.session.add(page)
    db.session.commit()
    return page


@pytest.fixture
def failing_request(app):
    """Make /tags fail with whatever is handed in, through the real lifecycle.

    Yields a callable taking the exception to raise and returning the response.
    PROPAGATE_EXCEPTIONS is restored even if the row fails, because leaving it
    off would make every later test in the session swallow its own errors.
    """
    def run(exception, side_effect=None):
        app.config['PROPAGATE_EXCEPTIONS'] = False
        try:
            with patch('app.tag.routes.render_template',
                       side_effect=side_effect or exception):
                return app.test_client().get('/tags')
        finally:
            app.config['PROPAGATE_EXCEPTIONS'] = None
    return run


# --------------------------------------------------------------------------
# P1: the ordinary 404
# --------------------------------------------------------------------------


def test_a_mistyped_url_gets_the_404_page(app, db_session):
    """Before the repair:

        PROBE q1 status: 404   PROBE q1 body: b'not found'

    while an unmatched IMAGE path -- the branch meant to skip the page --
    rendered it:

        PROBE q2 body starts: b'\\n<p>Oops, something is broken!</p>...'
    """
    _seed()
    client = app.test_client()

    response = client.get('/no/such/path')

    assert response.status_code == 404
    assert b'Oops, something is broken!' in response.data


@pytest.mark.parametrize('path', [
    '/static/nosuch.txt',
    '/api/nosuch',
    '/.well-known/nosuch',
    '/admin/nosuch',
    '/auth/nosuch',
    '/nosuch.png',
    '/nosuch.woff2',
])
def test_a_path_that_cannot_be_a_page_skips_the_lookup(app, db_session, path):
    """The fast path, one row per prefix and two for the extension tuple. Each
    still answers 404 with the page; what the branch saves is the CmsPage
    query, which is proved by the row below rather than by this one.
    """
    _seed()
    client = app.test_client()

    response = client.get(path)

    assert response.status_code == 404
    assert b'Oops, something is broken!' in response.data


@pytest.mark.parametrize('path', [
    '/static/trap',
    '/api/trap',
    '/.well-known/trap',
    '/admin/trap',
    '/auth/trap',
    '/trap.png',
    '/trap.woff2',
    '/trap.txt',
])
def test_the_fast_path_does_not_look_for_a_page(app, db_session, path):
    """What the prefix and extension checks are FOR, and the ONLY thing that
    distinguishes them now that both arms render the same page: a CmsPage whose
    url sits under one of these prefixes is not served, because the lookup
    never happens.

    One row per operand. Asserting the status and the body alone cannot tell
    the two arms apart -- six mutants dropping one operand each survived a
    version of this file that did, because after P1 both arms answer 404 with
    errors/404.html and only the query differs. `.woff2` needs its own row
    because `.woff` is not a suffix of it.
    """
    _seed()
    _cms_page(url=path, title='Trap')
    client = app.test_client()

    response = client.get(path)

    assert response.status_code == 404
    assert b'Trap' not in response.data


def test_a_cms_page_is_served_at_its_own_url(app, db_session):
    """The CmsPage branch: no route matches /about-us, so Flask raises 404 and
    the handler answers with the page -- at 200, because the page exists.
    """
    _seed()
    _cms_page()
    client = app.test_client()

    response = client.get('/about-us')

    assert response.status_code == 200
    assert b'About us' in response.data


def test_a_url_with_no_page_behind_it_is_still_a_404(app, db_session):
    """The other arm of `if cms_page:`, with a page in the database at a
    different url so the query runs and returns nothing.
    """
    _seed()
    _cms_page(url='/elsewhere')
    client = app.test_client()

    response = client.get('/no/such/path')

    assert response.status_code == 404
    assert b'elsewhere' not in response.data


# --------------------------------------------------------------------------
# 401, 429 and 500
# --------------------------------------------------------------------------


def test_an_unauthorised_request_gets_the_401_page(app, db_session, failing_request):
    _seed()

    response = failing_request(Unauthorized())

    assert response.status_code == 401
    assert b'<!doctype html>' in response.data


def test_a_rate_limited_request_gets_the_429_page(app, db_session, failing_request):
    _seed()

    response = failing_request(TooManyRequests())

    assert response.status_code == 429
    assert b'429 - Too Many Requests' in response.data


def test_an_unhandled_exception_gets_the_500_page(app, db_session, failing_request):
    _seed()

    response = failing_request(RuntimeError('boom'))

    assert response.status_code == 500
    assert b'<!doctype html>' in response.data


def _poison_then(exception):
    """A side effect that breaks the session's transaction, then raises.

    A failed statement leaves the session unusable: every later query on it
    raises until something rolls back. That is the state the handlers'
    db.session.rollback() exists for, and the only one in which it is
    observable.
    """
    def side_effect(*args, **kwargs):
        try:
            db.session.execute(text('SELECT nonexistent_column_zz FROM "user"'))
        except Exception:
            pass
        raise exception
    return side_effect


def test_a_poisoned_session_is_unusable_without_a_rollback(app, db_session):
    """The control for the two rows below: it establishes that the poison
    works, so their assertions are about the handler rather than about nothing.
    """
    _seed()
    try:
        db.session.execute(text('SELECT nonexistent_column_zz FROM "user"'))
    except Exception:
        pass

    with pytest.raises(Exception):
        Tag.query.count()

    db.session.rollback()


@pytest.mark.parametrize('exception, expected', [
    (RuntimeError('boom'), 500),
    (Unauthorized(), 401),
])
def test_the_handler_leaves_the_session_usable(app, db_session, failing_request,
                                               exception, expected):
    """Both handlers roll back. With the session poisoned first, the rollback
    is the difference between a working session afterwards and one that raises
    on its next query -- see the control above.
    """
    _seed()

    response = failing_request(exception, side_effect=_poison_then(exception))

    assert response.status_code == expected
    assert Tag.query.count() == 0


def test_the_429_handler_does_not_roll_back(app, db_session, failing_request):
    """The asymmetry, pinned rather than assumed: 429 is raised by a rate
    limiter in front of the work, not by the work failing, so there is nothing
    to roll back -- and the handler does not. A session poisoned before it
    stays poisoned.
    """
    _seed()

    response = failing_request(TooManyRequests(),
                               side_effect=_poison_then(TooManyRequests()))

    assert response.status_code == 429
    with pytest.raises(Exception):
        Tag.query.count()
    db.session.rollback()


# --------------------------------------------------------------------------
# app/admin/constants.py
# --------------------------------------------------------------------------


def test_the_report_type_choices_cover_every_type():
    """ReportTypes.get_choices() feeds a SelectField, so every constant on the
    class has to appear in it -- a type with no choice is a filter the admin
    cannot select. The class is read rather than transcribed, so a new
    constant fails this row instead of being silently unlisted.
    """
    from app.admin.constants import ReportTypes

    choices = ReportTypes.get_choices()
    declared = {value for name, value in vars(ReportTypes).items()
                if not name.startswith('_') and isinstance(value, int)}

    assert [value for value, label in choices] == [-1, 0, 1, 2, 4]
    assert {value for value, label in choices} == declared
    assert [str(label) for value, label in choices] == ['All', 'User', 'Post',
                                                        'Comment', 'Direct Message']


def test_the_report_type_values_are_distinct():
    """3 is skipped -- `# 3 is unused` at the top of the file -- so the values
    are not a range and a duplicate would not be obvious by eye.
    """
    from app.admin.constants import ReportTypes

    values = [value for value, label in ReportTypes.get_choices()]

    assert len(set(values)) == len(values)
    assert 3 not in values


# --------------------------------------------------------------------------
# app/nntp/__init__.py
# --------------------------------------------------------------------------


def test_the_nntp_blueprint_exists_and_is_named():
    """The module is two statements and nothing in the suite imported it, so it
    measured 0.0. The blueprint's name is what every url_for('nntp...') in the
    app would resolve through.
    """
    from flask import Blueprint
    from app.nntp import bp

    assert isinstance(bp, Blueprint)
    assert bp.name == 'nntp'
