"""An onion feed is shown only on the onion and never federates (plan F4, task 7; spec goals 8 and 12, anti-goal 2).

One rule decides it, the shared local-only one (`app.local_only.hidden_here`): off the onion a feed is hidden when its
home domain is a local-only domain other than this storefront, or when its owner is an account hidden here (a legacy row
an onion account owns, ruling N-m8). A hidden feed answers exactly as one nobody holds, and no other server ever sees
it: no actor, no WebFinger, no Accept, no Announce."""
import pytest

from app import db
from app.feed.util import actor_to_feed, search_for_feed
from app.feed_domains import federating_feed_clause, feed_local_only, feed_shown_here, feeds_shown_here_clause
from app.constants import NOTIF_FEED
from app.models import Feed, NotificationSubscription
from app.storefront import domain_for_host
from app.utils import feed_readable_by, feed_tree
from tests.conftest import forget_login
from tests.factories import a_keypair, make_feed
from tests.test_onion_guard_limits import a_session, onion_get
from tests.three_domains import FIRST, ONION, SHOP, ap_get, get, request_on

pytestmark = pytest.mark.usefixtures('site')

ONION_BASE = f'http://{ONION}'


@pytest.fixture(autouse=True)
def public_site(site):
    """Site.private_instance defaults true, which sends an anonymous reader of a feed page to the login page."""
    site.private_instance = False
    db.session.commit()
    return site


class World:
    pass


def login(client, user, host):
    forget_login()   # the app context outlives a request, and keeps the last caller
    scheme = 'http' if host == ONION else 'https'
    with client.session_transaction(base_url=f'{scheme}://{host}') as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def signed_get(app, user, path, host, headers=None):
    """GET `path` on `host` as `user` (None: logged out); the onion goes through its guard."""
    forget_login()
    if user is None:
        return get(app, path, host=host) if headers is None else ap_get(app, path, host=host)
    client = app.test_client()
    if host == ONION:
        a_session(client)
        login(client, user, host)
        return onion_get(client, path, headers=headers)
    login(client, user, host)
    return client.get(path, base_url=f'https://{host}', headers=headers)


def answer(response):
    return response.status_code, response.get_data()


# --- pages ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('host, who', [(FIRST, None), (FIRST, 'onion_owner'), (FIRST, 'first_owner'),
                                       (SHOP, 'onion_owner')])
def test_an_onion_feed_is_404_off_the_onion(app, onion_feeds_hidden_world, host, who):
    user = getattr(onion_feeds_hidden_world, who) if who else None
    assert signed_get(app, user, f'/f/onionfeed@{ONION}', host).status_code == 404


@pytest.mark.parametrize('who', ['onion_owner', 'first_owner'])
def test_an_onion_feed_opens_on_the_onion_for_a_signed_in_reader(app, onion_feeds_hidden_world, who):
    assert signed_get(app, getattr(onion_feeds_hidden_world, who), '/f/onionfeed', ONION).status_code == 200


def test_a_logged_out_onion_reader_does_not_see_an_onion_feed(app, onion_feeds_hidden_world):
    """Spec goal 8: logged-out viewers do not see onion feeds, as the C2 rule has it for accounts and communities."""
    assert signed_get(app, None, '/f/onionfeed', ONION).status_code == 404


@pytest.mark.parametrize('who', [None, 'onion_owner', 'first_owner'])
def test_a_legacy_feed_of_an_onion_account_answers_as_a_missing_one_on_the_first_domain(app, onion_feeds_hidden_world, who):
    """Ruling N-m8: filed under the first domain, but its owner is hidden there, so the fact that the account owns a
    feed is too. The answer is the missing feed's, byte for byte, for a page, an AP client and RSS."""
    user = getattr(onion_feeds_hidden_world, who) if who else None
    for suffix, headers in (('', None), ('', {'Accept': 'application/activity+json'}), ('.rss', None)):
        hidden = signed_get(app, user, f'/f/legacyfeed{suffix}', FIRST, headers)
        missing = signed_get(app, user, f'/f/nosuchfeed{suffix}', FIRST, headers)
        assert hidden.status_code == 404, (suffix, headers)
        assert answer(hidden) == (missing.status_code, missing.get_data().replace(b'nosuchfeed', b'legacyfeed'))


def test_an_onion_feed_answers_as_a_missing_one_off_the_onion(app, onion_feeds_hidden_world):
    for suffix in ('', '.rss'):
        hidden = signed_get(app, onion_feeds_hidden_world.onion_owner, f'/f/onionfeed@{ONION}{suffix}', FIRST)
        missing = signed_get(app, onion_feeds_hidden_world.onion_owner, f'/f/nosuchfeed@{ONION}{suffix}', FIRST)
        assert hidden.status_code == 404
        assert answer(hidden) == (missing.status_code, missing.get_data().replace(b'nosuchfeed', b'onionfeed'))


def test_the_resolver_finds_an_onion_feed_on_the_onion_only(app, onion_feeds_hidden_world):
    with request_on(app, onion_feeds_hidden_world.d.first):
        assert actor_to_feed(f'onionfeed@{ONION}') is None
        assert actor_to_feed('legacyfeed') is None
        assert actor_to_feed('firstfeed').id == onion_feeds_hidden_world.first_feed.id
    with request_on(app, onion_feeds_hidden_world.d.onion):
        assert actor_to_feed('onionfeed', signed_in=True).id == onion_feeds_hidden_world.onion_feed.id
        assert actor_to_feed('onionfeed', signed_in=False) is None


def test_an_onion_feed_has_no_actor_or_webfinger_anywhere(app, onion_feeds_hidden_world):
    assert ap_get(app, '/f/onionfeed', host=ONION).status_code == 404
    for host in (FIRST, SHOP, ONION):
        for resource in (f'acct:~onionfeed@{ONION}', f'acct:onionfeed@{ONION}', f'acct:~legacyfeed@{FIRST}'):
            assert get(app, f'/.well-known/webfinger?resource={resource}', host=host).status_code == 404, \
                (host, resource)


def test_an_onion_feed_by_id_is_unreadable_off_the_onion(app, onion_feeds_hidden_world):
    for feed in (onion_feeds_hidden_world.onion_feed, onion_feeds_hidden_world.legacy_feed):
        for host in (FIRST, SHOP):
            with request_on(app, domain_for_host(host)):
                assert feed_readable_by(feed, onion_feeds_hidden_world.onion_owner.id) is False
                assert feed_readable_by(feed, None) is False
    with request_on(app, onion_feeds_hidden_world.d.onion):
        assert feed_readable_by(onion_feeds_hidden_world.onion_feed, onion_feeds_hidden_world.onion_owner.id) is True
        assert feed_readable_by(onion_feeds_hidden_world.onion_feed, None) is False   # a logged-out reader: C2


def test_an_onion_feed_cannot_be_copied_off_the_onion(app, onion_feeds_hidden_world):
    for feed in (onion_feeds_hidden_world.onion_feed, onion_feeds_hidden_world.legacy_feed):
        assert signed_get(app, onion_feeds_hidden_world.first_owner, f'/feed/{feed.id}/copy', FIRST).status_code == 404
    assert signed_get(app, onion_feeds_hidden_world.first_owner, '/feed/2147483000/copy', FIRST).status_code == 404


def test_an_onion_accounts_lists_off_the_onion_omit_its_onion_feeds(app, onion_feeds_hidden_world):
    for path in ('/feeds_menu', '/explore', f'/u/{onion_feeds_hidden_world.first_owner.user_name}/myfeeds', '/feeds'):
        body = signed_get(app, onion_feeds_hidden_world.onion_owner, path, FIRST).get_data(as_text=True)
        assert 'onionfeed' not in body and 'legacyfeed' not in body, path
    with request_on(app, onion_feeds_hidden_world.d.first):
        assert feed_tree(onion_feeds_hidden_world.onion_owner.id) == []
    with request_on(app, onion_feeds_hidden_world.d.onion):
        assert {item['feed'].id for item in feed_tree(onion_feeds_hidden_world.onion_owner.id)} == {onion_feeds_hidden_world.onion_feed.id,
                                                                                  onion_feeds_hidden_world.legacy_feed.id}


def test_on_the_onion_its_owner_sees_its_onion_feeds_listed(app, onion_feeds_hidden_world):
    body = signed_get(app, onion_feeds_hidden_world.onion_owner, f'/u/{onion_feeds_hidden_world.onion_owner.user_name}/myfeeds', ONION).get_data(as_text=True)
    assert 'onionfeed' in body


def test_a_profile_off_the_onion_never_lists_a_feed_filed_on_the_onion(app, onion_feeds_hidden_world):
    """A first-domain account's feed filed under the onion (no path makes one, but a row could hold it) is not listed
    on its owner's clearnet profile or feed list."""
    stray = onion_feeds_hidden_world.d.local_feed('strayfeed', onion_feeds_hidden_world.d.onion, onion_feeds_hidden_world.first_owner)
    assert stray.user_id == onion_feeds_hidden_world.first_owner.id
    for path in (f'/u/{onion_feeds_hidden_world.first_owner.user_name}', f'/u/{onion_feeds_hidden_world.first_owner.user_name}/feeds'):
        body = get(app, path, host=FIRST).get_data(as_text=True)
        assert 'strayfeed' not in body, path
    assert 'firstfeed' in get(app, f'/u/{onion_feeds_hidden_world.first_owner.user_name}/feeds', host=FIRST).get_data(as_text=True)


def test_search_for_an_onion_feed_off_the_onion_finds_nothing(app, onion_feeds_hidden_world, no_http):
    with request_on(app, onion_feeds_hidden_world.d.first):
        assert search_for_feed(f'~onionfeed@{ONION}', signed_in=True) is None
        assert search_for_feed(f'~legacyfeed@{FIRST}', signed_in=True) is None
        assert search_for_feed(f'~firstfeed@{FIRST}', signed_in=True).id == onion_feeds_hidden_world.first_feed.id
    with request_on(app, onion_feeds_hidden_world.d.onion):
        assert search_for_feed(f'~onionfeed@{ONION}', signed_in=True).id == onion_feeds_hidden_world.onion_feed.id
    assert no_http == []


# --- the shared rule's feed forms ---------------------------------------------------------------------------------

def test_shown_here_is_the_shared_rule(app, onion_feeds_hidden_world, three_domains_peer):
    remote = make_feed(three_domains_peer, 'faraway', public=True)
    feeds = (onion_feeds_hidden_world.onion_feed, onion_feeds_hidden_world.legacy_feed, onion_feeds_hidden_world.first_feed, remote)
    for host in (FIRST, SHOP, ONION):
        with request_on(app, domain_for_host(host)):
            for signed_in in (False, True):
                by_row = {feed.id for feed in feeds if feed_shown_here(feed, signed_in)}
                by_clause = set(db.session.scalars(db.select(Feed.id).where(feeds_shown_here_clause(signed_in))))
                assert by_row == by_clause, (host, signed_in)
    with request_on(app, onion_feeds_hidden_world.d.first):
        assert {feed.id for feed in feeds if feed_shown_here(feed, True)} == {onion_feeds_hidden_world.first_feed.id, remote.id}
    with request_on(app, onion_feeds_hidden_world.d.onion):
        assert {feed.id for feed in feeds if feed_shown_here(feed, True)} == {feed.id for feed in feeds}


def test_the_federation_rule_and_its_clause_agree(app, onion_feeds_hidden_world, three_domains_peer):
    remote = make_feed(three_domains_peer, 'faraway', public=True)
    shop_feed = onion_feeds_hidden_world.d.local_feed('shopfeed', onion_feeds_hidden_world.d.shop, onion_feeds_hidden_world.d.account('shopowner', onion_feeds_hidden_world.d.shop))
    ownerless = onion_feeds_hidden_world.d.local_feed('ownerless', onion_feeds_hidden_world.d.first)
    feeds = (onion_feeds_hidden_world.onion_feed, onion_feeds_hidden_world.legacy_feed, onion_feeds_hidden_world.first_feed, remote, shop_feed, ownerless)
    local_only = {feed.id for feed in feeds if feed_local_only(feed)}
    assert local_only == {onion_feeds_hidden_world.onion_feed.id, onion_feeds_hidden_world.legacy_feed.id}
    federating = set(db.session.scalars(db.select(Feed.id).where(federating_feed_clause())))
    assert federating & {feed.id for feed in feeds} == {feed.id for feed in feeds} - local_only


def test_an_onion_accounts_feed_alerts_off_the_onion_omit_its_onion_feeds(app, onion_feeds_hidden_world):
    for feed in (onion_feeds_hidden_world.onion_feed, onion_feeds_hidden_world.legacy_feed, onion_feeds_hidden_world.first_feed):
        db.session.add(NotificationSubscription(name=feed.title, type=NOTIF_FEED, entity_id=feed.id,
                                                user_id=onion_feeds_hidden_world.onion_owner.id))
    db.session.commit()
    body = signed_get(app, onion_feeds_hidden_world.onion_owner, '/alerts/feeds/all', FIRST).get_data(as_text=True)
    assert 'firstfeed' in body
    assert 'onionfeed' not in body and 'legacyfeed' not in body
    body = signed_get(app, onion_feeds_hidden_world.onion_owner, '/alerts/feeds/all', ONION).get_data(as_text=True)
    assert 'onionfeed' in body and 'legacyfeed' in body


def test_an_onion_feeds_rss_on_the_onion_follows_its_token(app, onion_feeds_hidden_world):
    """Review M7: an RSS reader is the account its `?token=` names, signed in for the rule as for the view."""
    onion_feeds_hidden_world.onion_owner.rss_token = 'ONIONTOKEN'
    db.session.commit()
    assert signed_get(app, None, '/f/onionfeed.rss?token=ONIONTOKEN', ONION).status_code == 200
    assert signed_get(app, None, '/f/onionfeed.rss', ONION).status_code == 404
    assert signed_get(app, None, f'/f/onionfeed@{ONION}.rss?token=ONIONTOKEN', FIRST).status_code == 404
