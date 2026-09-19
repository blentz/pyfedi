"""app/search/routes.py.

MEASUREMENT BASIS. The module stood at 6.485% on the full-suite --cov=app run
at 7f1f6922c, carrying 162 missing statements and 112 missing arcs -- the
lowest start the campaign has taken.

Nothing here could run at all before this round: the test database is built by
migrations, so sqlalchemy_searchable's `parse_websearch` had never existed in
it and every `.search()` call raised `psycopg2.errors.UndefinedFunction`.
tests/conftest.py now installs the expressions once per session (fact 317).

Three defects are pinned here and repaired together:

  P1  an unknown search_for fell through to the render with next_url unbound,
      which is an UnboundLocalError and a 500 on a public route.
  P2  a non-numeric minimum_upvote reached int() and was a 500.
  P3  two redirects were built by f-string, so a query carrying an encoded '&'
      injected its own parameters into them.
"""
import pytest
from unittest.mock import patch

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import Post, PostReply, Site, utcnow
from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob


def _post(community, author, title, **kwargs):
    """A post the search can find: indexable, public and past review.

    make_post leaves status at Post.new()'s implicit value and indexable at the
    column default, and the search filters on both -- so a fixture that skips
    these is invisible to every assertion in this file.
    """
    post = make_post(community, author,
                     ap_id=f'https://test.piefed.local/post/{title.replace(" ", "")}',
                     title=title)
    post.status = POST_STATUS_REVIEWING + 1
    post.indexable = True
    for key, value in kwargs.items():
        setattr(post, key, value)
    db.session.commit()
    return post


def _reply(community, author, post, body, **kwargs):
    reply = PostReply(user_id=author.id, post_id=post.id, community_id=community.id,
                      body=body, body_html=f'<p>{body}</p>', from_bot=False,
                      nsfw=False, deleted=False, private=False, indexable=True,
                      posted_at=utcnow(),
                      ap_id=f'https://test.piefed.local/comment/{body.replace(" ", "")}')
    for key, value in kwargs.items():
        setattr(reply, key, value)
    db.session.add(reply)
    db.session.commit()
    return reply


def _titles(render):
    return [p.title for p in render.call_args.kwargs['posts'].items]


def _bodies(render):
    return [r.body for r in render.call_args.kwargs['replies'].items]


# --------------------------------------------------------------------------
# P1: an unknown search_for
# --------------------------------------------------------------------------


def test_an_unknown_search_for_renders_an_empty_result_page(app, db_session):
    """Before the repair:

        PROBE e1 exception: UnboundLocalError cannot access local variable
        'next_url' where it is not associated with a value

    next_url and prev_url are set inside the posts branch and again inside the
    comments branch; communities and people return redirects; anything else
    reached the render with both names unbound. `posts = None` and `replies =
    None` above them already say what the page should do with a search_for it
    does not know.
    """
    instance, alice, bob = _seed()
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get('/search?q=hello&search_for=bogus')

    assert response.status_code == 200
    assert render.call_args.kwargs['posts'] is None
    assert render.call_args.kwargs['replies'] is None
    assert render.call_args.kwargs['next_url'] is None
    assert render.call_args.kwargs['prev_url'] is None


# --------------------------------------------------------------------------
# P2: a non-numeric minimum_upvote
# --------------------------------------------------------------------------


def test_a_minimum_upvote_that_is_not_a_number_filters_nothing(app, db_session):
    """PROBE e2 exception: ValueError invalid literal for int() with base 10:
    'lots'. The value comes straight from the query string, so any link with a
    typo in it was a 500.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'popular article', up_votes=10, down_votes=0)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        response = client.get('/search?q=article&minimum_upvote=lots')

    assert response.status_code == 200
    assert _titles(render) == ['popular article']


def test_a_numeric_minimum_upvote_still_filters(app, db_session):
    """The other half of P2's inversion: a repair that ignored the parameter
    entirely would pass the test above.
    """
    instance, alice, bob = _seed()
    community = make_community('microblogs')
    _post(community, alice, 'popular article', up_votes=10, down_votes=0)
    _post(community, alice, 'ignored article', up_votes=1, down_votes=0)
    client = app.test_client()

    with patch('app.search.routes.render_template', return_value='rendered') as render:
        client.get('/search?q=article&minimum_upvote=5')

    assert _titles(render) == ['popular article']


# --------------------------------------------------------------------------
# P3: the redirects
# --------------------------------------------------------------------------


def test_a_query_carrying_an_ampersand_cannot_add_parameters_to_the_redirect(app, db_session):
    """Before the repair:

        PROBE e5 redirect: 302 /communities?search=cats&language_id=99&language_id=0

    -- the user's own text became a second language_id, arriving BEFORE the
    route's. Built with url_for, the ampersand is percent-encoded and stays
    part of the search term.
    """
    instance, alice, bob = _seed()
    client = app.test_client()

    response = client.get('/search?q=cats%26language_id=99&search_for=communities')

    assert response.status_code == 302
    location = response.headers['Location']
    # the ampersand and the equals are encoded, so the whole thing is one
    # search TERM -- and the route's own language_id appears once, as itself
    assert 'search=cats%26language_id%3D99' in location
    assert location.count('&language_id=') == 1


def test_a_people_search_redirects_with_its_query_encoded(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()

    response = client.get('/search?q=alice%26x=1&search_for=people')

    assert response.status_code == 302
    location = response.headers['Location']
    assert location.startswith('/instance/all/people?')
    assert 'q=alice%26x%3D1' in location
