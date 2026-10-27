"""An onion feed never federates (plan F4, task 7; spec goal 8): no Accept or Reject for a Follow of it, no Announce,
no actor, no listing, and a URL of ours naming it names nothing off the onion. The same holds for a legacy feed filed
under the first domain that an onion account owns (ruling N-m8)."""
import pytest

import app.shared.feed as shared_feed
from app import db
from app.activitypub import routes as ap_routes
from app.activitypub.routes import process_inbox_request
from app.federation_urls import local_object_for_url
from app.models import ActivityPubLog, FeedMember, Relay
from app.relays import RELAY_ACCEPTED, STYLE_MASTODON, inbound
from app.utils import utcnow
from tests.factories import inbox_activity, make_user
from tests.test_onion_feeds_hidden import ONION_BASE, public_site, world  # noqa: F401 -- fixtures
from tests.three_domains import FIRST, ONION, ap_get, peer, request_on, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


# --- federation ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('target', ['onion', 'legacy'])
def test_a_follow_of_an_onion_feed_gets_no_answer(app, world, peer, monkeypatch, target):  # noqa: F811
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    sends = []
    monkeypatch.setattr(ap_routes, 'send_post_request', lambda *args, **kwargs: sends.append(args))
    follower = make_user(peer, 'alice')
    follower.ap_fetched_at = utcnow()
    db.session.commit()
    feed = world.onion_feed if target == 'onion' else world.legacy_feed

    process_inbox_request(inbox_activity(follower, activity_type='Follow', object_uri=feed.ap_profile_id), True)

    assert sends == []
    assert FeedMember.query.filter_by(feed_id=feed.id, user_id=follower.id).first() is None
    if target == 'legacy':   # the onion's URL is no actor of ours to other servers at all
        assert ActivityPubLog.query.one().exception_message == 'Local only cannot be followed by remote users'


@pytest.mark.parametrize('target', ['onion', 'legacy'])
def test_an_onion_feed_is_never_announced(app, world, peer, monkeypatch, target):  # noqa: F811
    feed = world.onion_feed if target == 'onion' else world.legacy_feed
    community = world.d.local_community('somewhere', world.d.first)
    peer.inbox = f'https://{peer.domain}/inbox'
    db.session.add(FeedMember(feed_id=feed.id, user_id=make_user(peer, 'bob').id))
    db.session.commit()
    sends = []
    monkeypatch.setattr(shared_feed, 'send_post_request', lambda *args, **kwargs: sends.append(args))
    monkeypatch.setattr(shared_feed, 'instance_banned', lambda domain: False)

    shared_feed.announce_feed_add_remove_to_subscribers('Add', feed.id, community.id)

    assert sends == []


def test_a_legacy_feed_of_an_onion_account_still_does_not_federate(app, world):
    for path in ('/f/legacyfeed', '/f/legacyfeed/outbox', '/f/legacyfeed/following', '/f/legacyfeed/followers',
                 '/f/legacyfeed/moderators'):
        assert ap_get(app, path, host=FIRST).status_code == 404, path
    listed = ap_get(app, '/feeds', host=FIRST).json['site_view']['public_feeds']
    assert listed == [f'https://{FIRST}/f/firstfeed']


def test_a_url_of_ours_naming_an_onion_feed_names_nothing_off_the_onion(app, world):
    """Task 2 review I1: `local_object_for_url` reads the database, so it is the rule's to answer for a hidden feed."""
    urls = (f'https://{FIRST}/f/onionfeed@{ONION}', f'{ONION_BASE}/f/onionfeed', world.onion_feed.ap_profile_id,
            f'https://{FIRST}/f/legacyfeed')
    for url in urls:
        assert local_object_for_url(url) is None, url   # outside a request: the default domain's storefront
        with request_on(app, world.d.shop):
            assert local_object_for_url(url) is None, url
    with request_on(app, world.d.onion):
        assert local_object_for_url(f'{ONION_BASE}/f/onionfeed') == world.onion_feed
    assert local_object_for_url(f'https://{FIRST}/f/firstfeed') == world.first_feed


def test_a_relayed_onion_feed_url_is_ignored_as_one_nobody_holds(app, world, redis_double, monkeypatch):
    """The relay caller of `local_object_for_url`, which runs off the onion: the hidden feed and a missing one are
    logged alike."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    relay = Relay(url='https://relay.example/inbox', style=STYLE_MASTODON, inbox_url='https://relay.example/inbox',
                  actor_id='https://relay.example/actor', public_key='PEM', state=RELAY_ACCEPTED,
                  follow_activity_id=f'https://{FIRST}/activities/relay-follow/abc')
    db.session.add(relay)
    db.session.commit()
    for url in (f'{ONION_BASE}/f/onionfeed', f'{ONION_BASE}/f/nosuchfeed'):
        inbound.process_relayed_announce(relay.id, url)
    assert [log.exception_message for log in ActivityPubLog.query.order_by(ActivityPubLog.id)] == \
        ['relayed object on a domain of ours, not here'] * 2
