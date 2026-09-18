"""app/feed/routes.py's reading routes: feed_list, show_feed,
get_all_child_feed_ids, feed_create_post and show_feed_rss.

MEASUREMENT BASIS. Before this file existed the five carried 74 missing
statements and 46 missing arcs on the full-suite --cov=app run at c8c85a0c.

feed_list is twelve statements nothing had ever executed, and all three of its
defects sit in its first five lines: it served any user's feeds to any
logged-in caller, a missing argument was a 500, and the feed title went into
returned HTML unescaped. The three are pinned below and repaired together.

Facts 292-294 apply: render_template is patched for anything that renders, POST
routes need a real CSRF token, and the site fixture supplies g.site.
"""
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Community, Feed, FeedItem, Site, User
from tests.factories import make_community, make_instance, make_user

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
    owner = make_user(instance, 'feedowner', local=True)
    snooper = make_user(instance, 'snooper', local=True)
    return instance, owner, snooper


def _feed(user, name, title=None, **kwargs):
    kwargs.setdefault('public', True)
    feed = Feed(user_id=user.id, title=title or name, name=name, machine_name=name,
                instance_id=1,
                ap_profile_id=f'https://test.piefed.local/f/{name}',
                ap_public_url=f'https://test.piefed.local/f/{name}', **kwargs)
    db.session.add(feed)
    db.session.commit()
    return feed


# --------------------------------------------------------------------------
# Task 1: feed_list's three defects.
# --------------------------------------------------------------------------


def test_the_feed_dropdown_lists_only_the_callers_own_feeds(app, db_session):
    """Was a PIN; INVERTED once the acting user came from the session.

    ORIGINAL PINNED CLAIM, now false: ":407 takes user_id from the QUERY STRING
    and :413 filters on it, with no `public` filter and no check that the
    caller is that user", so any logged-in account could read any other
    account's feed titles, private ones included.

    The snooper has a feed of their own, and it must appear: a repair that
    returned nothing at all would pass an assertion that only checked the
    owner's title was gone.
    """
    instance, owner, snooper = _seed()
    _feed(owner, 'secretfeed', title='Owner secret feed', public=False)
    _feed(snooper, 'snoopersfeed', title='Snoopers own feed')

    with app.test_client() as client:
        login(client, snooper)
        response = client.get(
            f'/feed/list?user_id={owner.id}&community_id=1&current_feed_id=0')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Owner secret feed' not in body
    assert 'Snoopers own feed' in body


def test_the_feed_dropdown_survives_a_request_without_arguments(app, db_session):
    """Was a PIN; INVERTED once the three reads gained defaults.

    ORIGINAL PINNED CLAIM, now false: ":407-411 call int() on three query
    parameters with no default", so a request without them raised TypeError
    before anything else ran.

    The caller's own feed still appears, so this asserts the route WORKS
    without its arguments rather than merely not raising -- and the generated
    link carries the zeros, which is what the defaults mean.
    """
    instance, owner, snooper = _seed()
    _feed(snooper, 'snoopersfeed', title='Snoopers own feed')

    with app.test_client() as client:
        login(client, snooper)
        response = client.get('/feed/list')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'Snoopers own feed' in body
    assert 'current_feed_id=0' in body and 'community_id=0' in body


def test_the_feed_dropdown_escapes_the_title(app, db_session):
    """Was a PIN; INVERTED once the title was escaped.

    ORIGINAL PINNED CLAIM, now false: ":427 builds HTML in an f-string and
    drops feed.title into it verbatim", so a title carrying markup was returned
    as markup for the caller's page to splice into a dropdown.

    Both halves are asserted: the payload appears ESCAPED, and the surrounding
    anchor is still real markup -- a repair that escaped the whole line would
    pass the first assertion and break the feature.
    """
    instance, owner, snooper = _seed()
    _feed(owner, 'xssfeed', title='<img src=x onerror=alert(1)>')

    with app.test_client() as client:
        login(client, owner)
        response = client.get(
            f'/feed/list?user_id={owner.id}&community_id=1&current_feed_id=0')

    body = response.get_data(as_text=True)
    assert '<img src=x onerror=alert(1)>' not in body
    assert '&lt;img src=x onerror=alert(1)&gt;' in body
    assert body.startswith('<li><a class="dropdown-item"')
