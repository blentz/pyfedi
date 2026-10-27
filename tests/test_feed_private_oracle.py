"""A private feed answers a reader who may not read it exactly as a feed nobody holds (plan F4, task 7; the oracle parked
in Task 4's review): the same status and the same body, for a page, an ActivityPub client, RSS and the API. Its owner and
its members still open it."""
import pytest

from app import db
from app.activitypub.util import notify_about_post_task
from app.constants import NOTIF_FEED
from app.models import FeedItem, FeedMember, Notification, NotificationSubscription
from tests.conftest import forget_login
from tests.factories import make_feed_member, make_post
from tests.test_onion_feeds_hidden import answer, login, signed_get
from tests.test_per_domain_feed_routes import csrf
from tests.test_onion_hidden_api_ids import call
from tests.three_domains import FIRST, ONION, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

AP = {'Accept': 'application/activity+json'}


@pytest.fixture(autouse=True)
def public_site(site):
    site.private_instance = False
    db.session.commit()
    return site


@pytest.fixture
def secret(three_domains):  # noqa: F811
    d = three_domains
    owner, member, stranger = (d.account(name, d.first) for name in ('owner', 'member', 'stranger'))
    feed = d.local_feed('secretfeed', d.first, owner, public=False)
    make_feed_member(member, feed)
    return feed, owner, member, stranger


def same(hidden, missing):
    assert hidden.status_code == 404
    assert answer(hidden) == (missing.status_code, missing.get_data().replace(b'nosuchfeed', b'secretfeed'))


@pytest.mark.parametrize('who', [None, 'stranger'])
def test_a_private_feed_page_answers_a_non_member_as_a_missing_feed(app, secret, who):
    user = secret[3] if who else None
    same(signed_get(app, user, '/f/secretfeed', FIRST), signed_get(app, user, '/f/nosuchfeed', FIRST))
    same(signed_get(app, user, '/f/secretfeed.rss', FIRST), signed_get(app, user, '/f/nosuchfeed.rss', FIRST))


@pytest.mark.parametrize('path', ['', '/outbox', '/following', '/followers', '/moderators'])
def test_a_private_feed_answers_an_activitypub_client_as_a_missing_feed(app, secret, path):
    same(signed_get(app, None, f'/f/secretfeed{path}', FIRST, AP), signed_get(app, None, f'/f/nosuchfeed{path}', FIRST, AP))


@pytest.mark.parametrize('who', ['owner', 'member'])
def test_its_owner_and_its_members_still_open_it(app, secret, who):
    user = secret[1] if who == 'owner' else secret[2]
    assert signed_get(app, user, '/f/secretfeed', FIRST).status_code == 200


def test_the_api_answers_a_reader_who_is_not_its_owner_as_for_a_missing_feed(app, secret, monkeypatch):
    """The API shows a private feed to its owner alone (`feed_view`), so a member is refused there too, now with the
    missing feed's answer rather than `access_denied`."""
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    feed, owner, member, stranger = secret
    missing = call(app, stranger, 'GET', '/api/alpha/feed?id=2147483000')
    assert missing.status_code == 400
    for reader in (stranger, member):
        hidden = call(app, reader, 'GET', f'/api/alpha/feed?id={feed.id}')
        assert answer(hidden) == answer(missing), reader.user_name
    shown = call(app, owner, 'GET', f'/api/alpha/feed?id={feed.id}')
    assert shown.status_code == 200 and shown.json['id'] == feed.id
    assert FeedMember.query.filter_by(feed_id=feed.id).count() == 2   # owner and member, as made


@pytest.mark.parametrize('method, path, body', [
    ('POST', '/api/alpha/feed/follow', {'follow': True}),
    ('POST', '/api/alpha/feed/follow', {'follow': False}),
    ('PUT', '/api/alpha/feed', {'title': 'Taken'}),
    ('POST', '/api/alpha/feed/delete', {'deleted': True}),
])
def test_a_feed_action_answers_a_stranger_of_a_private_feed_as_for_a_missing_one(app, secret, monkeypatch, method,
                                                                                   path, body):
    """Review I2: following a private feed answered `access_denied` and editing one `incorrect_login`, where an id
    nobody holds answers its own not-found text: which ids were private feeds showed."""
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    feed, owner, member, stranger = secret
    hidden = call(app, stranger, method, path, {**body, 'feed_id': feed.id})
    missing = call(app, stranger, method, path, {**body, 'feed_id': 2147483000})
    assert answer(hidden) == answer(missing)
    assert hidden.status_code == 400
    assert FeedMember.query.filter_by(feed_id=feed.id).count() == 2 and feed.title == 'secretfeed'


def test_a_member_thrown_out_of_a_private_feed_may_still_leave_it_and_a_stranger_may_not_edit_a_public_one(
        app, secret, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    feed, owner, member, stranger = secret
    FeedMember.query.filter_by(feed_id=feed.id, user_id=member.id).one().is_banned = True
    db.session.commit()
    call(app, member, 'POST', '/api/alpha/feed/follow', {'feed_id': feed.id, 'follow': False})
    assert FeedMember.query.filter_by(feed_id=feed.id, user_id=member.id).first() is None
    feed.public = True
    db.session.commit()
    refused = call(app, stranger, 'PUT', '/api/alpha/feed', {'feed_id': feed.id, 'title': 'Taken'})
    assert refused.status_code == 400 and refused.json['message'] == 'incorrect_login'


def test_turning_on_a_private_feeds_notifications_answers_a_stranger_as_a_missing_feed(app, secret):
    """Review I3: a stranger got the toggle (200) for a private feed and 404 for an id nobody holds, and the
    subscription it made was real."""
    feed, owner, member, stranger = secret
    client = app.test_client()
    login(client, stranger, FIRST)
    token = csrf(app, client)
    hidden = client.post(f'/feed/{feed.id}/notification', base_url=f'https://{FIRST}', data={'csrf_token': token})
    missing = client.post('/feed/2147483000/notification', base_url=f'https://{FIRST}', data={'csrf_token': token})
    assert hidden.status_code == 404
    assert answer(hidden) == (missing.status_code, missing.get_data().replace(b'2147483000', str(feed.id).encode()))
    assert NotificationSubscription.query.filter_by(entity_id=feed.id, type=NOTIF_FEED).count() == 0
    forget_login()
    login(client, member, FIRST)
    assert client.post(f'/feed/{feed.id}/notification', base_url=f'https://{FIRST}',
                       data={'csrf_token': token}).status_code == 200


def test_a_subscriber_who_may_no_longer_read_a_private_feed_is_not_notified(app, secret, two_domains):  # noqa: F811
    """The feed was made private after the stranger turned its notifications on: a new post in it tells the member,
    and not the stranger."""
    feed, owner, member, stranger = secret
    community = two_domains.local_community('inside', two_domains.domains[0])
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    for reader in (member, stranger):
        db.session.add(NotificationSubscription(name=feed.name, type=NOTIF_FEED, entity_id=feed.id, user_id=reader.id))
    db.session.commit()
    post = make_post(community, owner, f'https://{FIRST}/post/inside', title='INSIDE')
    notify_about_post_task(post.id)
    notified = {row.user_id for row in Notification.query.filter_by(notif_type=NOTIF_FEED)}
    assert notified == {member.id}


def test_a_reader_who_may_no_longer_read_a_feed_can_still_turn_its_notifications_off(app, secret):
    """Review m3: a subscription made while the feed was public, or while its holder was a member, outlives that. The
    toggle removes it, and does not 404; a stranger with no subscription still gets the missing feed's answer."""
    feed, owner, member, stranger = secret
    banned = FeedMember.query.filter_by(feed_id=feed.id, user_id=member.id).one()
    banned.is_banned = True
    for reader in (stranger, member):
        db.session.add(NotificationSubscription(name=feed.name, type=NOTIF_FEED, entity_id=feed.id, user_id=reader.id))
    db.session.add(NotificationSubscription(name=feed.name, type=NOTIF_FEED, entity_id=feed.id, user_id=owner.id))
    db.session.commit()
    client = app.test_client()
    for reader in (stranger, member):
        login(client, reader, FIRST)
        token = csrf(app, client)
        toggled = client.post(f'/feed/{feed.id}/notification', base_url=f'https://{FIRST}', data={'csrf_token': token})
        assert toggled.status_code == 200, reader.user_name
        assert NotificationSubscription.query.filter_by(entity_id=feed.id, user_id=reader.id).count() == 0
        again = client.post(f'/feed/{feed.id}/notification', base_url=f'https://{FIRST}', data={'csrf_token': token})
        assert again.status_code == 404 and NotificationSubscription.query.filter_by(user_id=reader.id).count() == 0
    assert NotificationSubscription.query.filter_by(entity_id=feed.id, user_id=owner.id).count() == 1


def test_the_feed_alerts_list_leaves_out_a_feed_its_subscriber_may_no_longer_read(app, secret):
    feed, owner, member, stranger = secret
    for reader in (stranger, member, owner):
        db.session.add(NotificationSubscription(name=feed.name, type=NOTIF_FEED, entity_id=feed.id, user_id=reader.id))
    db.session.commit()
    assert 'secretfeed' not in signed_get(app, stranger, '/alerts/feeds/all', FIRST).get_data(as_text=True)
    for reader in (member, owner):
        assert 'secretfeed' in signed_get(app, reader, '/alerts/feeds/all', FIRST).get_data(as_text=True)
    FeedMember.query.filter_by(feed_id=feed.id, user_id=member.id).one().is_banned = True
    db.session.commit()
    assert 'secretfeed' not in signed_get(app, member, '/alerts/feeds/all', FIRST).get_data(as_text=True)


def test_a_clearnet_account_that_subscribed_on_the_onion_sees_no_onion_feed_in_its_alerts_on_the_first_domain(
        app, three_domains):  # noqa: F811
    d = three_domains
    bob = d.account('clearbob', d.first)
    onion_feed = d.local_feed('onionpublic', d.onion, d.account('onionowner', d.onion))
    db.session.add(NotificationSubscription(name=onion_feed.name, type=NOTIF_FEED, entity_id=onion_feed.id,
                                            user_id=bob.id))
    db.session.commit()
    assert 'onionpublic' not in signed_get(app, bob, '/alerts/feeds/all', FIRST).get_data(as_text=True)
    assert 'onionpublic' in signed_get(app, bob, '/alerts/feeds/all', ONION).get_data(as_text=True)
