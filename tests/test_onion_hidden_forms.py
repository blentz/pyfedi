"""What the route gate cannot see (follow-up F6, re-review N6 to N9): ids in a form field or query string, a community's
banned list, and the unread-notifications email. Off the onion an onion row answers as one nobody holds there too, and
no `Location` header spells its name."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import current_app

from app import cli, db
from app.constants import NOTIF_POST
from app.models import CommunityBan, CommunityMember, Feed, FeedItem, Notification, utcnow
from tests.test_instance_visible_here import get, scene  # noqa: F401 -- scene is a fixture
from tests.test_local_only_actors_hidden import ONION_ART, world  # noqa: F401 -- world is a fixture
from tests.test_onion_hidden_on_clearnet import near, normalised as strip_volatile, onion  # noqa: F401 -- a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

NOBODY = 2 ** 30


def same(found, absent):
    assert (found.status_code, found.headers.get('Location'), found.get_data()) == \
        (absent.status_code, absent.headers.get('Location'), absent.get_data()), \
        (found.status_code, found.headers.get('Location'), absent.status_code, absent.headers.get('Location'))


# --- N7: form-field community ids -------------------------------------------------------------------------------

@pytest.fixture
def feed(onion):
    feed = Feed(user_id=onion.first_bob.id, title='Mine', name='mine', machine_name='mine', public=True)
    db.session.add(feed)
    db.session.commit()
    for community in (onion.onion_art, onion.first_art):
        db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    db.session.commit()
    onion.feed = feed
    return onion


def test_a_feeds_submit_answers_an_onion_community_as_one_nobody_holds(app, feed):
    viewer = Visitor(app, feed.first_bob, FIRST)
    found = viewer.post('/f/mine/submit', {'community_id': str(feed.onion_art.id)})
    absent = viewer.post('/f/mine/submit', {'community_id': str(NOBODY)})
    same(found, absent)
    assert found.status_code == 404
    body = viewer.get('/f/mine/submit').get_data(as_text=True)
    assert 'Onion art' not in body and 'First art' in body


def test_a_topics_submit_answers_an_onion_community_as_one_nobody_holds(app, onion):
    viewer = Visitor(app, onion.first_bob, FIRST)
    same(viewer.post('/topic/arts/submit', {'community_id': str(onion.onion_art.id)}),
         viewer.post('/topic/arts/submit', {'community_id': str(NOBODY)}))
    assert 'Onion art' not in viewer.get('/topic/arts/submit').get_data(as_text=True)


def test_adding_an_onion_community_to_a_feed_answers_as_one_nobody_holds(app, feed):
    viewer = Visitor(app, feed.first_bob, FIRST)
    same(viewer.post('/feed/add_community', {'new_feed_id': str(feed.feed.id), 'community_id': str(feed.onion_art.id)}),
         viewer.post('/feed/add_community', {'new_feed_id': str(feed.feed.id), 'community_id': str(NOBODY)}))


def test_blocking_a_community_from_an_onion_posts_page_answers_as_for_a_post_nobody_holds(app, onion):
    viewer = Visitor(app, onion.first_bob, FIRST)
    headers = {'HX-Request': 'true', 'HX-Current-Url': f'https://{FIRST}/post/1'}
    answers = [viewer.client.post(f'/community/community/{onion.first_art.id}/block?post_id={post_id}',
                                  data={'csrf_token': viewer.token}, base_url=viewer.base, headers=headers)
               for post_id in (onion.post.id, NOBODY)]
    same(*answers)


def test_on_the_onion_the_submit_redirects_to_it(app, feed):
    response = Visitor(app, feed.first_bob, ONION).post('/f/mine/submit', {'community_id': str(feed.onion_art.id)})
    assert response.status_code == 307 and response.headers['Location'].endswith('/community/art/join_then_add')


# --- N8: /search?community= ---------------------------------------------------------------------------------------

@pytest.mark.parametrize('signed_in', [False, True])
def test_a_search_in_an_onion_community_answers_as_one_in_a_community_nobody_holds(app, onion, signed_in):
    def ask(path):
        return Visitor(app, onion.first_bob, FIRST).get(path) if signed_in else get(app, path)
    found, absent = ask(f'/search?community={ONION_ART}'), ask(f'/search?community=nope@{ONION}')
    assert found.status_code == absent.status_code
    equal(normalised(found, ONION_ART), normalised(absent, f'nope@{ONION}'))
    found, absent = ask(f'/search?community_id={onion.onion_art.id}'), ask(f'/search?community_id={NOBODY}')
    assert found.status_code == absent.status_code == 200   # the landing page, never a server error (round 3)
    equal(normalised(found, '\x00'), normalised(absent, '\x00'))   # the page does not echo the id


def equal(found, absent):
    at = next((i for i, (a, b) in enumerate(zip(found, absent)) if a != b), min(len(found), len(absent)))
    assert found == absent, (found[at - 100:at + 60], absent[at - 100:at + 60])


def normalised(response, term):
    return strip_volatile(response.get_data(), term)


# --- N6: the community's banned list on its moderation page --------------------------------------------------------

def test_a_communitys_banned_list_on_its_page_names_no_onion_account_on_the_first_domain(app, onion):
    db.session.add(CommunityBan(community_id=onion.first_art.id, user_id=onion.onion_bob.id, banned_by=onion.admin.id,
                                reason='BANNEDONION'))
    db.session.add(CommunityMember(community_id=onion.first_art.id, user_id=onion.onion_bob.id, is_banned=True))
    db.session.commit()
    body = get(app, '/community/art/moderate/subscribers', user=onion.admin).get_data(as_text=True)
    assert not near(body, ONION, 'BANNEDONION'), near(body, ONION, 'BANNEDONION')
    on_onion = get(app, f'/community/art@{FIRST}/moderate/subscribers', host=ONION, user=onion.admin)
    assert 'bob' in on_onion.get_data(as_text=True)


# --- N9: the unread-notifications email --------------------------------------------------------------------------

def test_the_unread_email_of_a_first_domain_account_leaves_out_onion_items(app, onion):
    bob = onion.first_bob
    bob.email_unread, bob.email_unread_sent, bob.email = True, False, 'bob@example.com'
    bob.last_seen = utcnow() - timedelta(days=2)
    hour_ago = utcnow() - timedelta(hours=1)
    db.session.add(Notification(user_id=bob.id, title='ONIONTITLE', url=f'/post/{onion.post.id}', notif_type=NOTIF_POST,
                                created_at=hour_ago, targets={'post_id': onion.post.id}))
    db.session.add(Notification(user_id=bob.id, title='FIRSTTITLE', url=f'/post/{onion.first_post.id}',
                                notif_type=NOTIF_POST, created_at=hour_ago, targets={'post_id': onion.first_post.id}))
    for post in (onion.post, onion.in_first, onion.first_post):
        post.posted_at = hour_ago
    db.session.commit()
    sent = {}
    with patch('app.cli.send_email', lambda *a, **k: sent.update({k['recipients'][0]: (k['text_body'],
                                                                                         k['html_body'])})):
        cli.register(current_app)
        current_app.test_cli_runner().invoke(args=['send_missed_notifs'])
    text, html = sent['bob@example.com']
    for body in (text, html):
        assert 'ONIONTITLE' not in body and 'ONIONPOST' not in body and 'ONIONINFIRST' not in body
    assert 'FIRSTTITLE' in text and 'FIRSTPOST' in html


def test_an_unread_email_with_only_onion_items_is_not_sent_off_the_onion(app, onion):
    bob = onion.first_bob
    bob.email_unread, bob.email_unread_sent, bob.email = True, False, 'bob@example.com'
    bob.last_seen = utcnow() - timedelta(days=2)
    db.session.add(Notification(user_id=bob.id, title='ONIONTITLE', url=f'/post/{onion.post.id}', notif_type=NOTIF_POST,
                                created_at=utcnow() - timedelta(hours=1), targets={'post_id': onion.post.id}))
    db.session.commit()
    sent = {}
    with patch('app.cli.send_email', lambda *a, **k: sent.update({k['recipients'][0]: k['html_body']})):
        cli.register(current_app)
        current_app.test_cli_runner().invoke(args=['send_missed_notifs'])
    assert sent == {}


def test_an_onion_accounts_email_keeps_them(app, onion):
    bob = onion.onion_bob
    bob.email_unread, bob.email_unread_sent, bob.email = True, False, 'onionbob@example.com'
    bob.last_seen = utcnow() - timedelta(days=2)
    db.session.add(Notification(user_id=bob.id, title='ONIONTITLE', url=f'/post/{onion.post.id}', notif_type=NOTIF_POST,
                                created_at=utcnow() - timedelta(hours=1), targets={'post_id': onion.post.id}))
    db.session.commit()
    sent = {}
    with patch('app.cli.send_email', lambda *a, **k: sent.update({k['recipients'][0]: k['text_body']})):
        cli.register(current_app)
        current_app.test_cli_runner().invoke(args=['send_missed_notifs'])
    assert 'ONIONTITLE' in sent['onionbob@example.com']

