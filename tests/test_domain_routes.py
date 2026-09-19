"""app/domain/routes.py -- the domain page, its feed, and the moderation pairs.

MEASUREMENT BASIS. The module stood at 18.107% on the full-suite --cov=app run
at e37767dc5, carrying 145 missing statements and 54 missing arcs.

Three defects are pinned here and repaired together:

  P1  a dot-less, non-numeric id went to the database as a primary key and was
      a DataError -- a 500 on a public route, in both the page and the feed.
  P2  the feed's de-duplication added the entry before deciding to skip it, so
      a second post sharing a url produced an <item> with no description, guid,
      author or date.
  P3  domain_unblock read the OPTIONAL HX-Current-Url header without a guard.
      This is D756 exactly, repaired in app/chat/routes.py four rounds ago.

Facts 292-294 and 314 apply: render_template is patched for anything that
renders, POST routes need a real CSRF token, the site fixture supplies g.site,
and a cookie needs the app's SERVER_NAME as its domain.
"""
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import Domain, DomainBlock, Post, Site, utcnow
from tests.factories import (grant_permission, make_community, make_instance, make_post,
                             make_user)

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


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


def _domain(name='example.test', **kwargs):
    domain = Domain(name=name, post_count=0, banned=False)
    for key, value in kwargs.items():
        setattr(domain, key, value)
    db.session.add(domain)
    db.session.commit()
    return domain


def _post_on(domain, community, author, title='an article', url=None, **kwargs):
    post = make_post(community, author,
                     ap_id=f'https://test.piefed.local/post/{title.replace(" ", "")}',
                     title=title)
    post.domain_id = domain.id
    post.url = url if url is not None else f'https://{domain.name}/{title.replace(" ", "-")}'
    post.status = POST_STATUS_REVIEWING + 1
    for key, value in kwargs.items():
        setattr(post, key, value)
    db.session.commit()
    domain.post_count = Post.query.filter_by(domain_id=domain.id).count()
    db.session.commit()
    return post


# --------------------------------------------------------------------------
# P1: a dot-less, non-numeric id
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path', ['/d/notanumber', '/d/notanumber/feed'])
def test_an_id_that_is_neither_a_name_nor_a_number_is_a_404(app, db_session, path):
    """Before the repair:

        PROBE d1 exception: DataError (psycopg2.errors.InvalidTextRepresentation)
        invalid input syntax for type integer: "notanumber"

    The route is public and unauthenticated, so any crawler following a mangled
    link reached it. Both copies of the lookup are pinned, because repairing
    one and leaving the other is the shape this campaign has met six times.
    """
    instance, alice, bob = _seed()
    _domain()
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered'):
        assert client.get(path).status_code == 404


def test_a_numeric_id_still_finds_its_domain(app, db_session):
    """The other half of P1's inversion: a repair that refused every dot-less
    id would pass the test above.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/d/{domain.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['domain'].id == domain.id


def test_a_name_with_a_dot_still_finds_its_domain(app, db_session):
    """The name branch, which the dot decides. `example.test` never reaches the
    primary-key lookup at all.
    """
    instance, alice, bob = _seed()
    domain = _domain('example.test')
    client = app.test_client()

    with patch('app.domain.routes.render_template', return_value='rendered') as render:
        response = client.get('/d/example.test')

    assert response.status_code == 200
    assert render.call_args.kwargs['domain'].id == domain.id


# --------------------------------------------------------------------------
# P2: the feed's de-duplication
# --------------------------------------------------------------------------


def test_two_posts_sharing_a_url_give_one_complete_feed_entry(app, db_session):
    """Before the repair the entry was added and THEN skipped:

        PROBE d2 entries: 2 guids: 1 authors: 0

    -- an <item> with a title and a link and nothing else. The assertion counts
    the parts of an entry rather than the entries alone, since a repair that
    emitted two complete items would also give `entries: 2`.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='first', url='https://example.test/same')
    _post_on(domain, community, alice, title='second', url='https://example.test/same')

    body = app.test_client().get(f'/d/{domain.id}/feed').get_data(as_text=True)

    # one complete entry: the parts the old `continue` skipped are the
    # assertion, not the item count alone. <author> is not among them -- feedgen
    # omits an RSS author that carries a name and no email -- so pubDate, the
    # last field after the skip, stands in for it.
    assert body.count('<item>') == 1
    assert body.count('<guid') == 1
    assert body.count('<pubDate>') == 1
    assert body.count('<description>') == 1
    # the newer post wins, since the feed is ordered by posted_at descending
    assert '<title>second</title>' in body
    assert '<title>first</title>' not in body


def test_two_posts_with_different_urls_both_appear(app, db_session):
    """The other half: the dedupe must not swallow distinct articles."""
    instance, alice, bob = _seed()
    domain = _domain()
    community = make_community('microblogs')
    _post_on(domain, community, alice, title='first', url='https://example.test/one')
    _post_on(domain, community, alice, title='second', url='https://example.test/two')

    body = app.test_client().get(f'/d/{domain.id}/feed').get_data(as_text=True)

    assert body.count('<item>') == 2
    assert body.count('<guid') == 2
    assert body.count('<pubDate>') == 2


# --------------------------------------------------------------------------
# P3: D756's twin
# --------------------------------------------------------------------------


def test_unblocking_without_a_current_url_still_answers(app, db_session):
    """PROBE d3 exception: TypeError argument of type 'NoneType' is not iterable.

    HX-Current-Url is optional, and by the time it is read the unblock has
    already been written -- so the user's block was removed and the response
    was a 500. D756, in a second module.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    db.session.add(DomainBlock(user_id=alice.id, domain_id=domain.id))
    db.session.commit()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == f'/d/{domain.id}'
    assert DomainBlock.query.count() == 0


def test_unblocking_from_elsewhere_returns_the_reader_there(app, db_session):
    """The true arm of the same guard, which keeps the `/d/` test
    load-bearing: a current url that is not a domain page comes back unchanged.
    """
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': 'https://test.piefed.local/u/bob'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == 'https://test.piefed.local/u/bob'


def test_unblocking_from_a_domain_page_returns_to_that_page(app, db_session):
    instance, alice, bob = _seed()
    domain = _domain()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/d/{domain.id}/unblock', data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': f'https://test.piefed.local/d/{domain.id}'})

    assert response.headers['HX-Redirect'] == f'/d/{domain.id}'
