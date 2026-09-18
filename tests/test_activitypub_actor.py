"""app/activitypub/actor.py -- finding, validating and fetching remote actors.

MEASUREMENT BASIS. The module stood at 62.763% on the full-suite --cov=app run
at 09f143ade, carrying 82 missing statements and 42 missing arcs.

Four defects are pinned here and repaired together:

  P1  find_actor_by_url's local community and feed branches ignored the kind
      the caller asked for, so feed_only=True on a community url returned the
      Community and the caller went on to treat it as a Feed.
  P2  a webfinger `self` link with no `href` was a KeyError. This is D739's
      twin -- sub-project 55 registered the identical unguarded read in
      app/feed/util.py -- and both copies are repaired together.
  P3  the webfinger response was closed only on the success path, so a 404 or
      an unacceptable content type leaked its connection.
  P4  (harness, not code) the retry paths sleep 3-10 seconds on the request
      thread; every test here patches app.activitypub.actor.time.sleep, which
      is fact 302.
"""
import pytest
from unittest.mock import patch

from app import db
from app.activitypub.actor import (create_actor_from_remote, fetch_actor_from_webfinger,
                                   fetch_remote_actor_data, find_actor_by_url,
                                   find_local_user, find_remote_actor,
                                   schedule_actor_refresh, validate_remote_actor)
from app.models import Community, Feed, Site, User
from tests.factories import make_community, make_instance, make_local_feed, make_user

pytestmark = pytest.mark.usefixtures('site')


class _Resp:
    """The parts of an httpx response these functions read.

    `closed` is tracked because P3 is exactly a response that is never closed,
    and no other assertion would notice.
    """

    def __init__(self, status_code=200, payload=None,
                 content_type='application/jrd+json', raises=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = {'Content-Type': content_type}
        self.closed = False
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload

    def close(self):
        self.closed = True


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance


def _webfinger(links):
    return _Resp(payload={'links': links})


# --------------------------------------------------------------------------
# P1: the local branches ignored the kind they were asked for
# --------------------------------------------------------------------------


def test_asking_for_a_feed_does_not_return_a_local_community(app, db_session):
    """Before the repair:

        PROBE a1 feed_only on a community url gave: Community <Community 1>

    The branch's two arms returned the same object, so community_only decided
    nothing and feed_only was not consulted at all.
    """
    _seed()
    community = make_community('microblogs')

    with app.test_request_context('/'):
        assert find_actor_by_url(community.ap_profile_id, feed_only=True) is None


def test_asking_for_a_community_does_not_return_a_local_feed(app, db_session):
    """PROBE a2 community_only on a feed url gave: Feed <Feed localfeed_1>"""
    _seed()
    feed = make_local_feed('localfeed')

    with app.test_request_context('/'):
        assert find_actor_by_url(feed.ap_profile_id, community_only=True) is None


def test_a_local_community_is_still_found_when_it_is_what_was_asked_for(app, db_session):
    """The other half of P1's inversion, both ways round: a repair that
    returned None for everything would pass the two tests above.
    """
    _seed()
    community = make_community('microblogs')

    with app.test_request_context('/'):
        assert find_actor_by_url(community.ap_profile_id, community_only=True).id == community.id
        assert find_actor_by_url(community.ap_profile_id).id == community.id


def test_a_local_feed_is_still_found_when_it_is_what_was_asked_for(app, db_session):
    _seed()
    feed = make_local_feed('localfeed')

    with app.test_request_context('/'):
        assert find_actor_by_url(feed.ap_profile_id, feed_only=True).id == feed.id
        assert find_actor_by_url(feed.ap_profile_id).id == feed.id


# --------------------------------------------------------------------------
# P2: a self link with no href, D739's twin
# --------------------------------------------------------------------------


def test_a_self_link_without_an_href_is_skipped(app, db_session):
    """PROBE a3 exception: KeyError 'href'.

    The walk continues to the next link rather than giving up, because a peer
    may advertise several -- so the fixture puts a usable link AFTER the
    broken one. Without that ordering, "skipped" and "gave up" would be
    indistinguishable.
    """
    _seed()
    links = [{'rel': 'self', 'type': 'application/activity+json'},
             {'rel': 'self', 'href': 'https://remote.example/u/alice'}]
    actor = {'type': 'Person', 'id': 'https://remote.example/u/alice'}

    responses = [_webfinger(links), _Resp(payload=actor)]
    with patch('app.activitypub.actor.get_request', side_effect=responses):
        with patch('app.activitypub.actor.time.sleep'):
            result = fetch_actor_from_webfinger('alice', 'remote.example')

    assert result == actor


def test_a_webfinger_reply_whose_only_self_link_is_broken_finds_nothing(app, db_session):
    _seed()
    links = [{'rel': 'self', 'type': 'application/activity+json'}]

    with patch('app.activitypub.actor.get_request', return_value=_webfinger(links)):
        with patch('app.activitypub.actor.time.sleep'):
            assert fetch_actor_from_webfinger('alice', 'remote.example') is None


def test_the_feed_webfinger_walk_skips_a_self_link_without_an_href(app, db_session):
    """D739's OTHER copy, in app/feed/util.py, closed by the same round. A guard
    present in one copy and missing from the other is D712's shape, which this
    campaign has met five times.
    """
    from app.feed.util import search_for_feed
    _seed()
    links = [{'rel': 'self', 'type': 'application/activity+json'},
             {'rel': 'self', 'href': 'https://remote.example/f/news'}]
    feed_json = {'type': 'Feed', 'id': 'https://remote.example/f/news',
                 'preferredUsername': 'news'}

    responses = [_Resp(payload={'links': links}), _Resp(payload=feed_json)]
    with patch('app.feed.util.get_request', side_effect=responses), \
         patch('app.feed.util.sleep'), \
         patch('app.feed.util.actor_json_to_model', return_value=None):
        # actor_json_to_model returning None is the cheapest way to stop after
        # the walk: what is under test is that the broken link did not raise
        result = search_for_feed('~news@remote.example')

    assert result is None


# --------------------------------------------------------------------------
# P3: the leaked response
# --------------------------------------------------------------------------


@pytest.mark.parametrize('response, why', [
    (_Resp(status_code=404), 'webfinger said no'),
    (_Resp(status_code=200, content_type='text/html'), 'the content type is not JSON'),
])
def test_the_webfinger_response_is_closed_on_every_path(app, db_session, response, why):
    """PROBE a4 result: None response closed: False, on both paths. Under
    httpx's pooling an unclosed response holds its connection until garbage
    collection.
    """
    _seed()

    with patch('app.activitypub.actor.get_request', return_value=response):
        with patch('app.activitypub.actor.time.sleep'):
            assert fetch_actor_from_webfinger('alice', 'remote.example') is None

    assert response.closed is True
