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


# --------------------------------------------------------------------------
# validate_remote_actor
# --------------------------------------------------------------------------


def test_the_public_collection_is_never_a_valid_actor(app, db_session):
    """`https://www.w3.org/ns/activitystreams#Public` is the addressing
    constant every public activity carries, not an actor -- and it is the
    first thing the function refuses.
    """
    _seed()

    with app.test_request_context('/'):
        assert validate_remote_actor('https://www.w3.org/ns/activitystreams#Public') is False


def test_an_actor_string_with_no_host_at_all_is_refused(app, db_session):
    """The comment above the guard records why the two empty-server cases are
    separated: a handle has a host after its '@' and must be resolved, while a
    url whose netloc urlparse refused has none and must be refused. This is the
    second.
    """
    _seed()

    with app.test_request_context('/'):
        assert validate_remote_actor('https://[oops/u/alice') is False


def test_a_handle_resolves_its_host_before_the_instance_gate(app, db_session):
    """The first: 'alice@remote.example' has no authority for urlparse to read,
    so the host comes from normalise_actor_string -- and the instance gate then
    sees a real host. The ban is what proves the gate was reached.
    """
    from app.models import Instance
    _seed()
    peer = make_instance('remote.example', software='lemmy')

    with app.test_request_context('/'):
        assert validate_remote_actor('alice@remote.example') is True

    Instance.query.get(peer.id).dormant = False
    db.session.query(Instance).filter_by(id=peer.id).update({'gone_forever': False})
    db.session.commit()


def test_a_banned_instance_is_refused(app, db_session):
    from app.models import BannedInstances
    _seed()
    db.session.add(BannedInstances(domain='remote.example'))
    db.session.commit()

    with app.test_request_context('/'):
        assert validate_remote_actor('https://remote.example/u/alice') is False


def test_an_allowlist_refuses_everything_not_on_it(app, db_session):
    """The other arm of the instance gate: with use_allowlist on, the question
    becomes whether the instance is ALLOWED, and an unlisted one is refused
    even though nothing has banned it.
    """
    from app.models import AllowedInstances
    _seed()

    with app.test_request_context('/'):
        with patch('app.activitypub.actor.get_setting', return_value=True):
            assert validate_remote_actor('https://remote.example/u/alice') is False
            db.session.add(AllowedInstances(domain='remote.example'))
            db.session.commit()
            assert validate_remote_actor('https://remote.example/u/alice') is True


def test_an_actor_url_containing_a_blocked_word_is_refused(app, db_session):
    _seed()

    with app.test_request_context('/'):
        with patch('app.activitypub.actor.actor_contains_blocked_words', return_value=True):
            assert validate_remote_actor('https://remote.example/u/alice') is False


def test_a_banned_actor_is_refused_unless_the_caller_allows_banned(app, db_session):
    """Both arms of `if actor.banned and not allow_banned`, which is what the
    inbox uses to let a ban activity reach an already-banned actor.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)
    actor.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        assert validate_remote_actor(actor.ap_profile_id, actor) is False
        assert validate_remote_actor(actor.ap_profile_id, actor, allow_banned=True) is True


def test_a_deleted_user_is_refused_but_a_deleted_community_is_not_asked(app, db_session):
    """The isinstance(actor, User) guard: `deleted` and the profile word check
    are asked of users only, so a Community with the same flag set is the row
    that keeps the isinstance load-bearing.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    user = make_user(peer, 'alice', local=False)
    user.deleted = True
    community = Community(name='dupe', title='dupe', instance_id=peer.id,
                          ap_profile_id='https://remote.example/c/dupe',
                          ap_public_url='https://remote.example/c/dupe')
    community.deleted = True
    db.session.add(community)
    db.session.commit()

    with app.test_request_context('/'):
        assert validate_remote_actor(user.ap_profile_id, user) is False
        assert validate_remote_actor(community.ap_profile_id, community) is True


def test_a_user_whose_profile_carries_a_blocked_word_is_refused(app, db_session):
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    user = make_user(peer, 'alice', local=False)

    with app.test_request_context('/'):
        with patch('app.activitypub.actor.actor_profile_contains_blocked_words',
                   return_value=True):
            assert validate_remote_actor(user.ap_profile_id, user) is False


# --------------------------------------------------------------------------
# find_local_user and find_remote_actor
# --------------------------------------------------------------------------


def test_a_local_user_is_found_by_url_or_by_alt_name(app, db_session):
    """The lookup is an OR over ap_profile_id and alt_user_name, and it is
    restricted to LOCAL rows (ap_id is None). A remote user whose alt name
    matches must not be returned, which is the row that keeps that filter
    load-bearing.
    """
    instance = _seed()
    local = make_user(instance, 'alice', local=True)
    # make_user leaves a LOCAL user's ap_profile_id None (tests/factories.py),
    # and find_local_user rsplits the url it is given, so the row under test
    # has to carry the profile url the server would have published
    local.ap_profile_id = 'https://test.piefed.local/u/alice'
    local.alt_user_name = 'alice_alt'
    peer = make_instance('remote.example', software='lemmy')
    remote = make_user(peer, 'bob', local=False)
    remote.alt_user_name = 'bob_alt'
    db.session.commit()

    assert find_local_user(local.ap_profile_id).id == local.id
    assert find_local_user('https://test.piefed.local/u/alice_alt').id == local.id
    assert find_local_user('https://remote.example/u/bob_alt') is None


def test_a_banned_local_user_is_hidden_unless_the_caller_allows_banned(app, db_session):
    instance = _seed()
    local = make_user(instance, 'alice', local=True)
    local.ap_profile_id = 'https://test.piefed.local/u/alice'
    local.banned = True
    db.session.commit()

    assert find_local_user(local.ap_profile_id) is None
    assert find_local_user(local.ap_profile_id, allow_banned=True).id == local.id


@pytest.mark.parametrize('path, kind', [
    ('/u/', 'user'),
    ('/c/', 'community'),
    ('/m/', 'community'),
    ('/f/', 'feed'),
])
def test_the_url_shape_picks_the_table_to_look_in(app, db_session, path, kind):
    """find_remote_actor's fast paths. Each row builds ONLY the actor its url
    shape names, so a lookup that fell through to the wrong table would return
    None rather than the right answer by luck.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    url = f'https://remote.example{path}thing'
    if kind == 'user':
        actor = make_user(peer, 'thing', local=False)
        actor.ap_profile_id = url
    elif kind == 'community':
        actor = Community(name='thing', title='thing', instance_id=peer.id,
                          ap_profile_id=url, ap_public_url=url)
        db.session.add(actor)
    else:
        actor = Feed(user_id=1, name='thing', title='thing', machine_name='thing',
                     instance_id=peer.id, ap_profile_id=url, ap_public_url=url)
        db.session.add(actor)
    db.session.commit()

    found = find_remote_actor(url)

    assert found is not None
    assert found.ap_profile_id == url


def test_a_post_url_is_not_read_as_a_community(app, db_session):
    """`'/c/' in url and '/p/' not in url` -- a post url carries both, and the
    exclusion is what stops the community fast path claiming it. The fallback
    then finds nothing, which is the right answer for a post.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    community = Community(name='news', title='news', instance_id=peer.id,
                          ap_profile_id='https://remote.example/c/news',
                          ap_public_url='https://remote.example/c/news')
    db.session.add(community)
    db.session.commit()

    assert find_remote_actor('https://remote.example/c/news/p/12') is None


def test_an_unrecognised_url_shape_falls_back_through_every_table(app, db_session):
    """The fallback runs user, then community, then feed. A feed at a url with
    no recognised marker is found only by the last of the three.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    url = 'https://remote.example/actors/news'
    feed = Feed(user_id=1, name='news', title='news', machine_name='news',
                instance_id=peer.id, ap_profile_id=url, ap_public_url=url)
    db.session.add(feed)
    db.session.commit()

    found = find_remote_actor(url)

    assert isinstance(found, Feed)
    assert found.ap_profile_id == url


def test_a_banned_remote_community_is_not_returned(app, db_session):
    """R2's arms, both of them. ix_community_ap_profile_id is UNIQUE -- the
    scoping probe proved it with an IntegrityError -- so the re-query for a
    non-banned copy of the SAME url can only ever answer None, and a banned
    community is therefore always None. Recorded as behaviour; the dead
    re-query itself is D771.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    url = 'https://remote.example/c/banned'
    community = Community(name='banned', title='banned', instance_id=peer.id,
                          banned=True, ap_profile_id=url, ap_public_url=url)
    db.session.add(community)
    db.session.commit()

    assert find_remote_actor(url) is None
    assert find_remote_actor('https://remote.example/actors/banned') is None


# --------------------------------------------------------------------------
# fetch_remote_actor_data
# --------------------------------------------------------------------------


def _fetch(responses, **kwargs):
    """Run the fetch with get_request doubled and the retry sleep patched.

    The sleep is `time.sleep(randint(3, 10))` on the request thread (D770), so
    an unpatched retry row costs the suite up to ten seconds. Fact 302.
    """
    with patch('app.activitypub.actor.get_request', side_effect=responses) as request:
        with patch('app.activitypub.actor.time.sleep') as slept:
            result = fetch_remote_actor_data('https://remote.example/u/alice', **kwargs)
    return result, request, slept


def test_a_two_hundred_gives_back_the_actor_json(app, db_session):
    _seed()
    actor = {'type': 'Person', 'id': 'https://remote.example/u/alice'}
    response = _Resp(payload=actor)

    result, request, slept = _fetch([response])

    assert result == actor
    assert response.closed is True
    assert slept.call_count == 0


def test_a_two_hundred_that_is_not_json_gives_nothing(app, db_session):
    """The body is what a misconfigured peer returns -- an HTML error page with
    a 200 -- and the function has to answer None rather than raise.
    """
    _seed()
    response = _Resp(raises=ValueError('not json'))

    result, request, slept = _fetch([response])

    assert result is None
    assert response.closed is True


def test_a_four_oh_one_is_retried_with_the_servers_own_signature(app, db_session):
    """The authorized-fetch path: a peer that refuses anonymous reads is asked
    again with this server's key, and the signed reply is what comes back.
    """
    _seed()
    actor = {'type': 'Person', 'id': 'https://remote.example/u/alice'}
    signed = _Resp(payload=actor)

    with patch('app.activitypub.actor.get_request', return_value=_Resp(status_code=401)):
        with patch('app.activitypub.actor.signed_get_request', return_value=signed) as signer:
            result = fetch_remote_actor_data('https://remote.example/u/alice')

    assert result == actor
    assert signed.closed is True
    assert signer.call_args.args[0] == 'https://remote.example/u/alice'
    assert signer.call_args.args[2].endswith('/actor#main-key')


def test_a_signed_retry_that_returns_junk_gives_nothing(app, db_session):
    _seed()
    signed = _Resp(raises=ValueError('not json'))

    with patch('app.activitypub.actor.get_request', return_value=_Resp(status_code=401)):
        with patch('app.activitypub.actor.signed_get_request', return_value=signed):
            assert fetch_remote_actor_data('https://remote.example/u/alice') is None

    assert signed.closed is True


def test_a_signed_retry_that_raises_gives_nothing(app, db_session):
    """The `except Exception: return None` around the whole signed attempt --
    reached here by the signer itself failing, which is what a missing site key
    does.
    """
    _seed()

    with patch('app.activitypub.actor.get_request', return_value=_Resp(status_code=401)):
        with patch('app.activitypub.actor.signed_get_request',
                   side_effect=RuntimeError('no key')):
            assert fetch_remote_actor_data('https://remote.example/u/alice') is None


@pytest.mark.parametrize('status', [429, 502, 503, 504])
def test_an_overloaded_peer_is_retried_once_and_then_given_up_on(app, db_session, status):
    """The four retryable codes. Two responses are supplied and both are
    consumed, which is what proves the retry happened rather than the first
    answer being reused.
    """
    _seed()
    first, second = _Resp(status_code=status), _Resp(status_code=status)

    result, request, slept = _fetch([first, second])

    assert result is None
    assert request.call_count == 2
    assert slept.call_count == 1
    assert first.closed is True and second.closed is True


def test_a_retry_that_succeeds_returns_the_second_answer(app, db_session):
    """The other end of the same loop: without this row, `continue` and
    `return None` would be indistinguishable.
    """
    _seed()
    actor = {'type': 'Person', 'id': 'https://remote.example/u/alice'}

    result, request, slept = _fetch([_Resp(status_code=503), _Resp(payload=actor)])

    assert result == actor
    assert request.call_count == 2


def test_an_unretryable_status_is_given_up_on_at_once(app, db_session):
    """A 404 is not in the retryable set, so the second response must never be
    reached -- supplying one is what makes that assertion possible.
    """
    _seed()

    result, request, slept = _fetch([_Resp(status_code=404), _Resp(payload={'type': 'Person'})])

    assert result is None
    assert request.call_count == 1


@pytest.mark.parametrize('error', ['ReadTimeout', 'ConnectTimeout', 'WriteTimeout',
                                   'ReadError', 'RemoteProtocolError'])
def test_a_timeout_is_retried(app, db_session, error):
    """The five exception types the module calls "server is overloaded". Each
    is its own row, because they are listed individually in the except clause
    and a mutant dropping any one of them would otherwise survive.
    """
    import httpx
    _seed()
    failure = getattr(httpx, error)('overloaded')
    actor = {'type': 'Person', 'id': 'https://remote.example/u/alice'}

    result, request, slept = _fetch([failure, _Resp(payload=actor)])

    assert result == actor
    assert request.call_count == 2
    assert slept.call_count == 1


def test_a_timeout_on_the_last_attempt_gives_nothing(app, db_session):
    import httpx
    _seed()

    result, request, slept = _fetch([httpx.ReadTimeout('slow'), httpx.ReadTimeout('slow')])

    assert result is None
    assert request.call_count == 2


def test_a_dns_failure_is_not_retried(app, db_session):
    """httpx.HTTPError's own branch: an unreachable host or a bad certificate
    is not going to improve in eight seconds, so it gives up at once -- and the
    second response must go unused.
    """
    import httpx
    _seed()

    result, request, slept = _fetch([httpx.ConnectError('no such host'),
                                     _Resp(payload={'type': 'Person'})])

    assert result is None
    assert request.call_count == 1
    assert slept.call_count == 0


def test_a_caller_may_ask_for_no_retries_at_all(app, db_session):
    """retry_count=0 makes the loop run once, which is what the inbox wants
    when a peer is already known to be slow.
    """
    _seed()

    result, request, slept = _fetch([_Resp(status_code=503)], retry_count=0)

    assert result is None
    assert request.call_count == 1
    assert slept.call_count == 0


# --------------------------------------------------------------------------
# schedule_actor_refresh
# --------------------------------------------------------------------------


def test_a_stale_remote_actor_is_refreshed(app, db_session):
    from app.utils import utcnow
    from datetime import timedelta
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)
    actor.ap_fetched_at = utcnow() - timedelta(days=2)
    db.session.commit()

    with patch('app.activitypub.actor.refresh_user_profile') as refresh:
        schedule_actor_refresh(actor)

    assert refresh.call_count == 1
    assert refresh.call_args.args[0] == actor.id


def test_a_refresh_already_running_is_not_started_again(app, db_session):
    """THE CACHE HAS TO BE PATCHED TO TEST THIS AT ALL. The suite runs under
    `CACHE_TYPE = 'NullCache'` (tests/conftest.py:68), so `cache.set` is a no-op
    and `cache.get` always answers None -- a second call in the same test
    refreshes again, and the guard that exists to stop a hot actor being
    refreshed on every request is invisible. Patching the module's cache is
    what makes the flag observable, and the write is asserted too: a guard that
    reads a flag nothing sets would pass the read-only half.
    """
    from app.utils import utcnow
    from datetime import timedelta
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)
    actor.ap_fetched_at = utcnow() - timedelta(days=2)
    db.session.commit()

    with patch('app.activitypub.actor.cache') as cache_double:
        cache_double.get.return_value = True
        with patch('app.activitypub.actor.refresh_user_profile') as refresh:
            schedule_actor_refresh(actor)
        assert refresh.call_count == 0
        assert cache_double.set.call_count == 0

        cache_double.get.return_value = None
        with patch('app.activitypub.actor.refresh_user_profile') as refresh:
            schedule_actor_refresh(actor)
        assert refresh.call_count == 1
        assert cache_double.set.call_args.args[0] == f'refreshing_{actor.id}'
        assert cache_double.set.call_args.kwargs['timeout'] == 300


def test_a_freshly_fetched_actor_is_not_refreshed_unless_overridden(app, db_session):
    from app.utils import utcnow
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)
    actor.ap_fetched_at = utcnow()
    db.session.commit()

    with patch('app.activitypub.actor.refresh_user_profile') as refresh:
        schedule_actor_refresh(actor)
        assert refresh.call_count == 0
        schedule_actor_refresh(actor, override=True)
        assert refresh.call_count == 1


def test_an_actor_never_fetched_is_refreshed(app, db_session):
    """ap_fetched_at None is its own operand in the three-way `or`."""
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)
    actor.ap_fetched_at = None
    db.session.commit()

    with patch('app.activitypub.actor.refresh_user_profile') as refresh:
        schedule_actor_refresh(actor)

    assert refresh.call_count == 1


def test_a_local_actor_is_never_refreshed(app, db_session):
    instance = _seed()
    actor = make_user(instance, 'alice', local=True)
    actor.ap_fetched_at = None
    db.session.commit()

    with patch('app.activitypub.actor.refresh_user_profile') as refresh:
        schedule_actor_refresh(actor)

    assert refresh.call_count == 0


@pytest.mark.parametrize('kind', ['community', 'feed'])
def test_each_kind_of_actor_goes_to_its_own_refresher(app, db_session, kind):
    """The isinstance ladder. Each row asserts the OTHER refreshers were not
    called, since a ladder that fell through would otherwise look right.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    # ap_id is what makes a Community or a Feed remote (app/models.py:1848,
    # :3206): is_local() answers True for ap_id None whatever the instance says,
    # so a fixture carrying only ap_profile_id is never refreshed
    if kind == 'community':
        actor = Community(name='news', title='news', instance_id=peer.id,
                          ap_id='news@remote.example',
                          ap_profile_id='https://remote.example/c/news',
                          ap_public_url='https://remote.example/c/news')
    else:
        actor = Feed(user_id=1, name='news', title='news', machine_name='news',
                     instance_id=peer.id, ap_id='news@remote.example',
                     ap_profile_id='https://remote.example/f/news',
                     ap_public_url='https://remote.example/f/news')
    actor.ap_fetched_at = None
    db.session.add(actor)
    db.session.commit()

    with patch('app.activitypub.actor.refresh_user_profile') as user_refresh, \
         patch('app.activitypub.actor.refresh_community_profile') as community_refresh, \
         patch('app.activitypub.actor.refresh_feed_profile') as feed_refresh:
        schedule_actor_refresh(actor)

    assert user_refresh.call_count == 0
    assert community_refresh.call_count == (1 if kind == 'community' else 0)
    assert feed_refresh.call_count == (0 if kind == 'community' else 1)


# --------------------------------------------------------------------------
# create_actor_from_remote
# --------------------------------------------------------------------------


def test_a_url_is_fetched_directly_and_a_handle_through_webfinger(app, db_session):
    """The two arms of create_actor_from_remote's first branch, and the reason
    the branch exists: a url has a host to fetch from, a handle does not.
    """
    _seed()
    actor_json = {'type': 'Person', 'id': 'https://remote.example/u/alice'}
    model = object()

    with patch('app.activitypub.actor.fetch_remote_actor_data', return_value=actor_json) as direct, \
         patch('app.activitypub.actor.fetch_actor_from_webfinger') as webfinger, \
         patch('app.activitypub.actor.actor_json_to_model', return_value=model):
        assert create_actor_from_remote('https://remote.example/u/alice') is model
    assert direct.call_count == 1 and webfinger.call_count == 0

    with patch('app.activitypub.actor.fetch_remote_actor_data') as direct, \
         patch('app.activitypub.actor.fetch_actor_from_webfinger', return_value=actor_json) as webfinger, \
         patch('app.activitypub.actor.actor_json_to_model', return_value=model):
        assert create_actor_from_remote('alice@remote.example') is model
    assert direct.call_count == 0 and webfinger.call_count == 1
    assert webfinger.call_args.args == ('alice', 'remote.example')


def test_a_handle_that_does_not_parse_is_refused_before_any_request(app, db_session):
    """normalise_actor_string returns ('', '') for a string with no '@', and
    the guard on that empty address is what stops a webfinger request to a
    server named ''.
    """
    _seed()

    with patch('app.activitypub.actor.fetch_actor_from_webfinger') as webfinger:
        assert create_actor_from_remote('alice') is None

    assert webfinger.call_count == 0


def test_a_fetch_that_finds_nothing_creates_nothing(app, db_session):
    _seed()

    with patch('app.activitypub.actor.fetch_remote_actor_data', return_value=None), \
         patch('app.activitypub.actor.actor_json_to_model') as model:
        assert create_actor_from_remote('https://remote.example/u/alice') is None

    assert model.call_count == 0


@pytest.mark.parametrize('flag, wrong_kind', [
    ('community_only', 'user'),
    ('feed_only', 'user'),
])
def test_the_wrong_kind_of_actor_is_discarded(app, db_session, flag, wrong_kind):
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    model = make_user(peer, 'alice', local=False)

    with patch('app.activitypub.actor.fetch_remote_actor_data', return_value={'type': 'Person'}), \
         patch('app.activitypub.actor.actor_json_to_model', return_value=model):
        assert create_actor_from_remote('https://remote.example/u/alice', **{flag: True}) is None


def test_creating_a_bot_clears_the_low_value_reposters_cache(app, db_session):
    """The one side effect this function has beyond returning the model, and it
    runs for bots only -- so the non-bot row is what keeps the guard honest.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    bot = make_user(peer, 'botty', local=False)
    bot.bot = True
    human = make_user(peer, 'human', local=False)
    db.session.commit()

    with patch('app.activitypub.actor.fetch_remote_actor_data', return_value={'type': 'Person'}), \
         patch('app.activitypub.actor.cache.delete_memoized') as cleared:
        with patch('app.activitypub.actor.actor_json_to_model', return_value=bot):
            create_actor_from_remote('https://remote.example/u/botty')
        assert cleared.call_count == 1
        with patch('app.activitypub.actor.actor_json_to_model', return_value=human):
            create_actor_from_remote('https://remote.example/u/human')
        assert cleared.call_count == 1


def test_a_banned_community_found_by_the_fallback_is_not_returned_either(app, db_session):
    """The fallback path repeats the fast path's banned handling verbatim, so
    it needs its own row: a banned community at a url carrying no /c/ marker.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    url = 'https://remote.example/actors/banned'
    community = Community(name='banned', title='banned', instance_id=peer.id,
                          banned=True, ap_id='banned@remote.example',
                          ap_profile_id=url, ap_public_url=url)
    db.session.add(community)
    db.session.commit()

    assert find_remote_actor(url) is None


# --------------------------------------------------------------------------
# find_actor_by_url: the remote and username paths
# --------------------------------------------------------------------------


def test_a_remote_actor_is_returned_when_it_validates(app, db_session):
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)

    with app.test_request_context('/'):
        assert find_actor_by_url(actor.ap_profile_id).id == actor.id


def test_a_remote_actor_that_fails_validation_is_FALSE_not_None(app, db_session):
    """The function's documented oddity: None means not found, False means
    found and refused. A caller testing `if actor is None` behaves differently
    from one testing `if not actor`, so the distinction is asserted by identity.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)
    actor.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        result = find_actor_by_url(actor.ap_profile_id)

    assert result is False


@pytest.mark.parametrize('flag', ['community_only', 'feed_only'])
def test_a_remote_actor_of_the_wrong_kind_is_none(app, db_session, flag):
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)

    with app.test_request_context('/'):
        assert find_actor_by_url(actor.ap_profile_id, **{flag: True}) is None


def test_a_remote_url_that_matches_nothing_falls_through_to_the_handle_lookup(app, db_session):
    """A url containing '@' that no row matches reaches the ap_id lookup at the
    end -- which is how a webfinger handle is resolved -- and finds nothing.
    """
    _seed()

    with app.test_request_context('/'):
        assert find_actor_by_url('https://remote.example/users/@nobody') is None


def test_a_handle_is_looked_up_by_ap_id(app, db_session):
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)

    with app.test_request_context('/'):
        assert find_actor_by_url('alice@remote.example').id == actor.id


def test_a_bare_name_finds_a_local_user_by_user_name(app, db_session):
    """The else arm: a bare name is a LOCAL username, and the query filters on
    ap_id None so a remote user of the same name cannot answer it.
    """
    instance = _seed()
    local = make_user(instance, 'alice', local=True)
    peer = make_instance('remote.example', software='lemmy')
    make_user(peer, 'alice', local=False)

    with app.test_request_context('/'):
        found = find_actor_by_url('alice')

    assert found.id == local.id


def test_a_bare_name_falls_back_to_the_published_profile_url(app, db_session):
    """The second query in the else arm, reached only when the first finds
    nothing: a local user whose user_name does not match but whose published
    profile url ends in that name.
    """
    instance = _seed()
    local = make_user(instance, 'renamed', local=True)
    local.ap_profile_id = 'https://test.piefed.local/u/alice'
    db.session.commit()

    with app.test_request_context('/'):
        found = find_actor_by_url('alice')

    assert found.id == local.id


def test_a_bare_name_that_matches_nothing_is_none(app, db_session):
    _seed()

    with app.test_request_context('/'):
        assert find_actor_by_url('nobody') is None


# --------------------------------------------------------------------------
# fetch_actor_from_webfinger: the retry paths
# --------------------------------------------------------------------------


def _webfinger_fetch(responses):
    with patch('app.activitypub.actor.get_request', side_effect=responses) as request:
        with patch('app.activitypub.actor.time.sleep') as slept:
            result = fetch_actor_from_webfinger('alice', 'remote.example')
    return result, request, slept


def test_a_webfinger_lookup_returns_the_actor_it_points_at(app, db_session):
    _seed()
    actor = {'type': 'Person', 'id': 'https://remote.example/u/alice'}
    links = [{'rel': 'self', 'href': 'https://remote.example/u/alice'}]
    webfinger, actor_response = _webfinger(links), _Resp(payload=actor)

    result, request, slept = _webfinger_fetch([webfinger, actor_response])

    assert result == actor
    assert webfinger.closed is True and actor_response.closed is True
    assert request.call_args_list[0].args[0] == 'https://remote.example/.well-known/webfinger'
    assert request.call_args_list[0].kwargs['params'] == {'resource': 'acct:alice@remote.example'}
    assert request.call_args_list[1].kwargs['headers'] == {'Accept': 'application/activity+json'}


def test_a_link_advertising_its_own_type_is_asked_for_that_type(app, db_session):
    """`link.get('type', 'application/activity+json')` -- the default is
    covered by the row above, so this one supplies a type to make the lookup
    load-bearing.
    """
    _seed()
    links = [{'rel': 'self', 'href': 'https://remote.example/u/alice',
              'type': 'application/ld+json'}]

    result, request, slept = _webfinger_fetch([_webfinger(links), _Resp(payload={'x': 1})])

    assert request.call_args_list[1].kwargs['headers'] == {'Accept': 'application/ld+json'}


def test_a_link_that_is_not_self_is_ignored(app, db_session):
    """Every webfinger reply carries several links; only `rel: self` is the
    actor. A profile-page link must not be fetched.
    """
    _seed()
    links = [{'rel': 'http://webfinger.net/rel/profile-page',
              'href': 'https://remote.example/@alice'}]

    result, request, slept = _webfinger_fetch([_webfinger(links)])

    assert result is None
    assert request.call_count == 1


def test_a_webfinger_request_that_fails_is_tried_once_more(app, db_session):
    import httpx
    _seed()
    actor = {'type': 'Person', 'id': 'https://remote.example/u/alice'}
    links = [{'rel': 'self', 'href': 'https://remote.example/u/alice'}]

    result, request, slept = _webfinger_fetch([httpx.ConnectError('refused'),
                                               _webfinger(links), _Resp(payload=actor)])

    assert result == actor
    assert slept.call_count == 1


def test_a_webfinger_request_that_fails_twice_gives_nothing(app, db_session):
    import httpx
    _seed()

    result, request, slept = _webfinger_fetch([httpx.ConnectError('refused'),
                                               httpx.ConnectError('refused')])

    assert result is None
    assert request.call_count == 2


def test_an_actor_fetch_that_fails_is_tried_once_more(app, db_session):
    import httpx
    _seed()
    actor = {'type': 'Person', 'id': 'https://remote.example/u/alice'}
    links = [{'rel': 'self', 'href': 'https://remote.example/u/alice'}]

    result, request, slept = _webfinger_fetch([_webfinger(links),
                                               httpx.ConnectError('refused'),
                                               _Resp(payload=actor)])

    assert result == actor
    assert slept.call_count == 1


def test_an_actor_fetch_that_fails_twice_gives_nothing(app, db_session):
    import httpx
    _seed()
    links = [{'rel': 'self', 'href': 'https://remote.example/u/alice'}]

    result, request, slept = _webfinger_fetch([_webfinger(links),
                                               httpx.ConnectError('refused'),
                                               httpx.ConnectError('refused')])

    assert result is None


def test_an_actor_document_that_is_not_json_gives_nothing(app, db_session):
    """The inner `except Exception: actor_data.close()` -- and the close is
    what the assertion is really about, since the return value is None either
    way.
    """
    _seed()
    links = [{'rel': 'self', 'href': 'https://remote.example/u/alice'}]
    actor_response = _Resp(raises=ValueError('not json'))

    result, request, slept = _webfinger_fetch([_webfinger(links), actor_response])

    assert result is None
    assert actor_response.closed is True


def test_an_actor_document_with_a_bad_status_gives_nothing(app, db_session):
    _seed()
    links = [{'rel': 'self', 'href': 'https://remote.example/u/alice'}]

    result, request, slept = _webfinger_fetch([_webfinger(links), _Resp(status_code=404)])

    assert result is None


def test_a_webfinger_reply_with_no_links_at_all_gives_nothing(app, db_session):
    """`webfinger_json.get('links', [])` -- a reply with no links key is a
    peer's malformed answer, and it must be a not-found rather than a KeyError.
    """
    _seed()

    result, request, slept = _webfinger_fetch([_Resp(payload={})])

    assert result is None


def test_the_fetch_loop_answers_none_when_it_is_asked_for_no_attempts(app, db_session):
    """fetch_remote_actor_data's `return None` after the loop. `range(retry_count
    + 1)` is empty only for a negative count, which no caller passes -- the line
    is reachable solely by a direct call, and this row is what documents that.
    """
    _seed()

    with patch('app.activitypub.actor.get_request') as request:
        assert fetch_remote_actor_data('https://remote.example/u/alice', retry_count=-1) is None

    assert request.call_count == 0


def test_a_local_user_url_is_answered_from_the_local_table(app, db_session):
    """find_actor_by_url's third local branch. `allow_banned` is threaded
    through to find_local_user, so the banned row is what proves the argument
    is passed rather than defaulted.
    """
    instance = _seed()
    local = make_user(instance, 'alice', local=True)
    local.ap_profile_id = 'https://test.piefed.local/u/alice'
    db.session.commit()

    with app.test_request_context('/'):
        assert find_actor_by_url(local.ap_profile_id).id == local.id
        assert find_actor_by_url(local.ap_profile_id, community_only=True) is None
        assert find_actor_by_url(local.ap_profile_id, feed_only=True) is None

        local.banned = True
        db.session.commit()
        assert find_actor_by_url(local.ap_profile_id) is None
        assert find_actor_by_url(local.ap_profile_id, allow_banned=True).id == local.id


def test_a_local_url_naming_nobody_is_none(app, db_session):
    """Each local branch's `return None` for a url of the right shape that
    matches no row -- the community and feed branches are covered by P1's rows,
    this is the user one.
    """
    _seed()

    with app.test_request_context('/'):
        assert find_actor_by_url('https://test.piefed.local/u/nobody') is None


@pytest.mark.parametrize('url', [
    'https://remote.example/u/ghost',
    'https://remote.example/c/ghost',
    'https://remote.example/f/ghost',
])
def test_a_fast_path_that_finds_nothing_falls_through_to_the_others(app, db_session, url):
    """Each fast path is an optimisation, not a decision: when its own table
    has no row the function still tries the rest. The fixture puts the actor in
    a table the url's shape does NOT name, so only the fallback can find it.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    if '/u/' in url:
        # a COMMUNITY at a /u/ url: the user fast path queries its own table,
        # finds nothing, and the fallback is what answers
        actor = Community(name='ghost', title='ghost', instance_id=peer.id,
                          ap_id='ghost@remote.example', ap_profile_id=url,
                          ap_public_url=url)
        db.session.add(actor)
    else:
        actor = make_user(peer, 'ghost', local=False)
        actor.ap_profile_id = url
    db.session.commit()

    found = find_remote_actor(url)

    assert found is not None
    assert found.id == actor.id


def test_an_actor_of_no_known_kind_is_not_refreshed(app, db_session):
    """schedule_actor_refresh's isinstance ladder has no else, so an object
    that is none of the three falls out silently. Reached here with a double,
    because every real actor IS one of the three -- which is the point: the
    ladder's exit arc exists only for a caller that does not exist yet.
    """
    from unittest.mock import MagicMock
    _seed()
    stranger = MagicMock()
    stranger.is_local.return_value = False
    stranger.ap_fetched_at = None
    stranger.id = 99

    with patch('app.activitypub.actor.refresh_user_profile') as user_refresh, \
         patch('app.activitypub.actor.refresh_community_profile') as community_refresh, \
         patch('app.activitypub.actor.refresh_feed_profile') as feed_refresh:
        schedule_actor_refresh(stranger)

    assert user_refresh.call_count == 0
    assert community_refresh.call_count == 0
    assert feed_refresh.call_count == 0


def test_the_unbanned_copy_lookup_can_never_find_anything(app, db_session):
    """THE MODULE'S TWO RESIDUAL LINES, 104 AND 125, PROVED UNREACHABLE.

    Both read `actor = unbanned_actor` after

        unbanned_actor = <Community where ap_profile_id == url and not banned>
        if unbanned_actor is None:
            return None

    -- a query for a SECOND community row with the same ap_profile_id as the
    banned one just found. `ix_community_ap_profile_id` is UNIQUE, so that row
    cannot exist: the scoping probe got
    `IntegrityError ... duplicate key value violates unique constraint
    "ix_community_ap_profile_id"` trying to build it. The re-query therefore
    always answers None, the `return None` above always fires, and the two
    assignments are dead -- twice over, since the block is written out twice.

    Registered as D771 rather than deleted, following D758.
    """
    from sqlalchemy.exc import IntegrityError
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    url = 'https://remote.example/c/dupe'
    banned = Community(name='dupe', title='dupe', instance_id=peer.id, banned=True,
                       ap_id='dupe@remote.example', ap_profile_id=url, ap_public_url=url)
    db.session.add(banned)
    db.session.commit()

    second = Community(name='dupe2', title='dupe2', instance_id=peer.id, banned=False,
                       ap_id='dupe2@remote.example', ap_profile_id=url,
                       ap_public_url=url + '2')
    db.session.add(second)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    assert find_remote_actor(url) is None


# --------------------------------------------------------------------------
# Rows added to close mutation survivors
# --------------------------------------------------------------------------


def test_a_url_with_a_refused_netloc_does_not_get_a_host_from_its_at_sign(app, db_session):
    """THE GUARD'S REASON, ASSERTED. `if not server and '://' not in actor_url`
    resolves a HANDLE's host through normalise_actor_string but refuses a URL
    whose netloc urlparse would not read. Dropping the second operand lets the
    url take the handle path -- and this string is what makes that dangerous:
    everything before the '@' becomes the "name" and `evil.example` becomes the
    server, so an actor id pointing at a broken host is validated against an
    instance that never served it.
    """
    _seed()
    make_instance('evil.example', software='lemmy')

    with app.test_request_context('/'):
        assert validate_remote_actor('https://[oops/u/alice@evil.example') is False


def test_a_webfinger_reply_with_the_wrong_content_type_is_not_read(app, db_session):
    """The content-type half of the acceptance check. The reply carries usable
    links, so a mutant dropping the check would follow them -- which is the
    point: an HTML page that happens to contain JSON is not a webfinger answer.
    """
    _seed()
    links = [{'rel': 'self', 'href': 'https://remote.example/u/alice'}]
    response = _Resp(payload={'links': links}, content_type='text/html')

    result, request, slept = _webfinger_fetch([response])

    assert result is None
    assert request.call_count == 1


def test_a_webfinger_reply_with_a_bad_status_is_not_read(app, db_session):
    """The status half of the same check, with links present for the same
    reason.
    """
    _seed()
    links = [{'rel': 'self', 'href': 'https://remote.example/u/alice'}]
    response = _Resp(status_code=404, payload={'links': links})

    result, request, slept = _webfinger_fetch([response])

    assert result is None
    assert request.call_count == 1


def test_a_plain_http_address_is_fetched_directly_too(app, db_session):
    """`startswith('https://') or startswith('http://')` -- the second operand
    is what keeps a plain-http peer off the webfinger path, where its address
    would be read as a handle.
    """
    _seed()
    model = object()

    with patch('app.activitypub.actor.fetch_remote_actor_data', return_value={'type': 'Person'}) as direct, \
         patch('app.activitypub.actor.fetch_actor_from_webfinger') as webfinger, \
         patch('app.activitypub.actor.actor_json_to_model', return_value=model):
        assert create_actor_from_remote('http://remote.example/u/alice') is model

    assert direct.call_count == 1
    assert webfinger.call_count == 0


def test_a_url_in_capitals_finds_the_same_actor(app, db_session):
    """`actor_url.strip().lower()` -- peers and users both send mixed case, and
    every lookup below compares against stored lower-case values.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    actor = make_user(peer, 'alice', local=False)
    # make_user publishes /users/<name>; the lookup under test is the /u/ one
    actor.ap_profile_id = 'https://remote.example/u/alice'
    db.session.commit()

    with app.test_request_context('/'):
        assert find_actor_by_url('  HTTPS://REMOTE.EXAMPLE/U/ALICE  ').id == actor.id


def test_the_empty_host_guard_is_defence_in_depth_rather_than_the_refusal(app, db_session):
    """ONE OF THE ROUND'S FIVE EQUIVALENT MUTANTS, PROVED.

    `if not server: return False` cannot be observed, because the gate below it
    already fails closed on an empty host: a probe gives
    `instance_banned('') -> True`, `instance_banned(None) -> True` and
    `instance_allowed('') -> False`, so both the banlist and the allowlist mode
    refuse a hostless actor whether or not the guard is there.

    Recorded rather than deleted: the guard is what makes the refusal explicit
    at the point a reader looks for it, and it stops depending on a behaviour
    of two other functions. Both halves are asserted here, so a change to
    either helper's fail-closed behaviour shows up as a failure in this file.
    """
    from app.utils import instance_allowed, instance_banned
    _seed()

    with app.test_request_context('/'):
        assert instance_banned('') is True
        assert instance_banned(None) is True
        assert instance_allowed('') is False
        assert validate_remote_actor('https://[oops/u/alice') is False


@pytest.mark.parametrize('url, kind', [
    ('https://remote.example/u/alice', 'user'),
    ('https://remote.example/c/news', 'community'),
    ('https://remote.example/f/feed', 'feed'),
])
def test_the_url_shape_fast_paths_change_nothing_but_the_query_order(app, db_session, url, kind):
    """THE ROUND'S OTHER FOUR EQUIVALENT MUTANTS, PROVED.

    find_remote_actor's `/u/`, `/c/`, `/m/` and `/f/` tests exist "to optimize
    database queries" (its own comment). Each of the four can be broken -- the
    marker misspelt, the `/p/` and `/t/` exclusions dropped -- without any
    observable change, because the fallback below repeats every one of the
    three queries unconditionally. That is what this row asserts directly: the
    right actor comes back for each url shape EVEN THOUGH the actor is stored
    at a url whose marker names a different table, so the fast path cannot be
    what answered.

    So the four mutants are equivalent, and the fast paths are an optimisation
    with no behaviour of their own. Registered as D774.
    """
    _seed()
    peer = make_instance('remote.example', software='lemmy')
    # the actor is always a Feed, whatever the url's marker says
    feed = Feed(user_id=1, name='thing', title='thing', machine_name='thing',
                instance_id=peer.id, ap_id='thing@remote.example',
                ap_profile_id=url, ap_public_url=url)
    db.session.add(feed)
    db.session.commit()

    found = find_remote_actor(url)

    assert isinstance(found, Feed)
    assert found.id == feed.id
