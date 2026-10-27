"""A notification about a feed names it, so off the onion one about an onion feed is hidden as one about any onion row
(plan F4, task 7 review C1): a clearnet account that joined an onion feed on the onion, with its notifications on,
sees nothing of them on the first domain: not on the page, in the unread count, in the API's list or in the email."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from flask import current_app

from app import cli, db
from app.activitypub.util import notify_about_post_task
from app.constants import NOTIF_FEED
from app.hidden_routes import hidden_notifications, unread_shown
from app.models import FeedItem, FeedMember, Notification, NotificationSubscription, User, utcnow
from app.storefront import domain_for_host
from tests.factories import make_local_feed, make_post
from tests.test_instance_visible_here import scene  # noqa: F401 -- a fixture
from tests.test_local_only_actors_hidden import world  # noqa: F401 -- a fixture
from tests.test_onion_hidden_api_ids import alpha, call  # noqa: F401 -- alpha is a fixture
from tests.test_onion_hidden_on_clearnet import onion  # noqa: F401 -- a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST, ONION, request_on, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

TITLE = 'ONIONFEEDTITLE'


@pytest.fixture(params=['onion', 'legacy'])
def notified(request, app, alpha, two_domains):  # noqa: F811
    """`first_bob` joined a feed holding the first domain's art, notifications on, and a first-domain post in art
    notified him. The feed is the onion's, or (ruling N-m8) filed under the first domain but owned by an onion account."""
    w = alpha
    first, onion_domain = two_domains.domains
    domain = onion_domain if request.param == 'onion' else first
    feed = make_local_feed('onionfeed', public=True, domain=domain, user=w.onion_bob)
    feed.title = TITLE
    db.session.add_all([FeedItem(feed_id=feed.id, community_id=w.first_art.id),
                        FeedMember(feed_id=feed.id, user_id=w.first_bob.id),
                        NotificationSubscription(name=TITLE, type=NOTIF_FEED, entity_id=feed.id,
                                                 user_id=w.first_bob.id)])
    w.first_bob.unread_notifications = 0
    Notification.query.filter_by(user_id=w.first_bob.id).delete()
    db.session.commit()
    post = make_post(w.first_art, w.admin, f'https://{FIRST}/post/new-in-art', title='NEWINART')
    notify_about_post_task(post.id)
    db.session.expire_all()
    w.notification = Notification.query.filter_by(user_id=w.first_bob.id, notif_type=NOTIF_FEED).one()
    w.feed = feed
    return w


def test_the_notification_is_stamped_as_naming_a_local_only_row(app, notified):
    assert notified.notification.local_only is True
    assert notified.notification.targets['feed_id'] == notified.feed.id


def test_the_first_domain_shows_nothing_of_it(app, notified):
    bob = notified.first_bob
    body = Visitor(app, bob, FIRST).get('/notifications').get_data(as_text=True)
    assert TITLE not in body and 'NEWINART' not in body
    with request_on(app, domain_for_host(FIRST)):
        assert unread_shown(db.session.get(User, bob.id)) == 0
    listed = call(app, bob, 'GET', '/api/alpha/user/notifications?status=All').get_json()
    assert TITLE not in str(listed) and listed['counts']['total'] == 0
    assert call(app, bob, 'GET', '/api/alpha/user/notifications_count').get_json()['count'] == 0


def test_the_onion_still_shows_it(app, notified):
    bob = notified.first_bob
    assert TITLE in Visitor(app, bob, ONION).get('/notifications').get_data(as_text=True)
    assert call(app, bob, 'GET', '/api/alpha/user/notifications_count', host=ONION).get_json()['count'] == 1


def test_the_notification_links_the_feed_by_its_stored_link_not_its_title(app, notified):
    feed, row = notified.feed, notified.notification
    assert row.targets['feed_link'] == feed.stored_link() and row.targets['feed_name'] == TITLE
    body = Visitor(app, notified.first_bob, ONION).get('/notifications').get_data(as_text=True)
    expected = feed.stored_link() if '@' in feed.stored_link() else f'{feed.stored_link()}@{FIRST}'   # a bare name is the first domain's
    assert f'href="/f/{expected}"' in body and f'href="/f/{TITLE}"' not in body


def test_a_notification_stored_before_the_link_was_kept_still_renders(app, notified):
    notified.notification.targets = {k: v for k, v in notified.notification.targets.items() if k != 'feed_link'}
    db.session.commit()
    body = Visitor(app, notified.first_bob, ONION).get('/notifications').get_data(as_text=True)
    assert f'href="/f/{TITLE}"' in body


def test_the_unread_email_leaves_it_out_off_the_onion(app, notified):
    bob = db.session.get(User, notified.first_bob.id)
    bob.email_unread, bob.email_unread_sent, bob.email = True, False, 'bob@example.com'
    bob.last_seen = utcnow() - timedelta(days=2)
    notified.notification.created_at = utcnow() - timedelta(hours=1)
    db.session.commit()
    sent = {}

    def run_the_command():
        with patch('app.cli.send_email', lambda *a, **k: sent.update({k['recipients'][0]: k['html_body']})):
            cli.register(current_app)
            current_app.test_cli_runner().invoke(args=['send_missed_notifs'])

    run_the_command()   # the email lists notification titles (a post's, not the feed's); the only one is hidden here
    assert 'bob@example.com' not in sent
    db.session.add(Notification(user_id=bob.id, title='VISIBLEPOST', url='/notifications', notif_type=NOTIF_FEED,
                                created_at=utcnow() - timedelta(hours=1)))   # control: the command does send mail
    db.session.commit()
    run_the_command()
    visible = Notification.query.filter_by(user_id=bob.id, title='VISIBLEPOST').one()
    listed = sent['bob@example.com']   # NEWINART is a first-domain post, so "recent posts" may list it: look at the links
    assert f'/notification/{visible.id}/goto' in listed and f'/notification/{notified.notification.id}/goto' not in listed


def test_the_stamp_and_the_per_row_rule_agree_on_feed_targets(app, alpha, two_domains):  # noqa: F811
    """The stamp says a notification may name a local-only row; the per-row rule hides it here. A feed is named by
    `targets.feed_id`: stamped when the feed is local-only by its domain or its owner, and hidden off the onion."""
    first, onion_domain = two_domains.domains
    feeds = [make_local_feed('onionfeed', True, onion_domain, alpha.onion_bob),
             make_local_feed('legacyfeed', True, first, alpha.onion_bob),
             make_local_feed('firstfeed', True, first, alpha.first_bob)]
    rows = [Notification(user_id=alpha.first_bob.id, title='t', url=f'/post/{alpha.first_post.id}', notif_type=NOTIF_FEED,
                         targets={'feed_id': feed.id, 'post_id': alpha.first_post.id}) for feed in feeds]
    db.session.add_all(rows)
    db.session.commit()
    assert [row.local_only for row in rows] == [True, True, False]
    with request_on(app, domain_for_host(FIRST)):
        assert hidden_notifications(rows) == [True, True, False]
    with request_on(app, domain_for_host(ONION)):
        assert hidden_notifications(rows) == [False, False, False]
