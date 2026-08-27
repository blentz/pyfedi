"""get_deduped_post_ids's Redis cache key: who may read it, and when it is written.

`result_id` is CLIENT-CONTROLLED. All three web callers take it straight from the
query string and echo it back into their own pagination links:

    app/main/routes.py:83   result_id=request.args.get('result_id', gibberish(15)) ...
    app/feed/routes.py:419  (same shape)
    app/topic/routes.py:38  (same shape)

The cached value is a list of post ids that was filtered for ONE specific viewer's
authorisation -- hidden posts, blocked users/communities/domains/instances, private
community membership, NSFW/NSFL/bot/AI preferences, language preferences. Nothing
downstream re-checks it: `post_ids_to_models` re-filters nothing, it just loads the
ids it is handed. So whatever key that value is stored under is, in effect, the
access-control boundary for the whole feed.

Before the fix, the key was the raw `result_id` with no user component and a 24h
TTL. Two sessions that shared a `result_id` -- a shared page-2 link is enough --
shared a cache entry, in both directions: the second reader received the first
reader's authorised ids, and an attacker who could get a victim to load a URL with
a chosen `result_id` could seed what the victim's next page would render.

The fix derives ONE key used by both the read and the write:

    cache_key = f'feed:{current_user.id}:{result_id}' if result_id and current_user.is_authenticated else None

so the cache is per-user, and the read guard and the write guard can no longer
disagree about whether an entry exists.

The tests below assert the three observable consequences. They are written against
returned post ids and against which Redis keys exist -- never against the generated
SQL and never against mocks.

Redis policy: every test here requests `redis_double`, which patches
`app.redis_client` (see its docstring in tests/conftest.py). Without it each
authenticated call would leave a real 24-hour key in the shared test Redis that
only `./run_tests.sh --down` clears.
"""
import json
import uuid

from flask_login import login_user

from app import db
from app.models import Feed, FeedItem, Topic
from app.utils import get_deduped_post_ids
from tests.factories import (hide_post, make_community, make_community_member, make_instance,
                             make_post, make_user)


def ids_for(app, viewer, result_id, community_ids, sort='new'):
    """get_deduped_post_ids as `viewer`, with an explicit result_id."""
    with app.test_request_context('/'):
        login_user(viewer)
        return get_deduped_post_ids(result_id, community_ids, sort)


def anonymous_ids(app, result_id, community_ids, sort='new'):
    """get_deduped_post_ids with no logged-in user."""
    with app.test_request_context('/'):
        return get_deduped_post_ids(result_id, community_ids, sort)


# --- 1. The leak, as a regression test ---------------------------------------

def test_two_users_sharing_a_result_id_each_get_their_own_authorised_posts(
        app, db_session, redis_double):
    """The cross-user feed cache leak, pinned.

    Alice and Bob are members of the same community and are handed the SAME
    client-controlled `result_id` -- exactly what happens when one of them shares
    a page-2 link, since every web caller both reads `?result_id=` and writes it
    back into the pagination URLs it renders.

    Their authorised results differ: each has hidden the other's post. The
    hidden_posts filter is the discriminator on purpose -- it is appended
    UNCONDITIONALLY for every authenticated viewer (`p.id NOT IN (SELECT
    hidden_post_id FROM "hidden_posts" WHERE user_id = :user_id)`,
    app/utils.py:3888), so neither user's exclusion depends on any other
    preference, config value or membership state being set a particular way.

    Against the unfixed function this fails on the second assertion with
    `bob_ids == [alice_only.id]`: Alice's call wrote her authorised ids to the key
    literally named 'shared-result-id', and Bob's call read them straight back out
    -- Bob receiving a post he had explicitly hidden and never re-running the
    query that would have excluded it.

    Mutation that would fail this: drop `current_user.id` from the cache key (keep
    `result_id` alone) and Bob is served Alice's list again. Verified by doing
    exactly that -- see this branch's report.
    """
    make_instance('cacheleak.example')
    alice = make_user(None, 'cacheleakalice', local=True)
    bob = make_user(None, 'cacheleakbob', local=True)
    community = make_community('cacheleakcomm')
    make_community_member(alice, community)
    make_community_member(bob, community)

    alice_only = make_post(community, alice, 'https://cacheleak.example/posts/1')
    bob_only = make_post(community, bob, 'https://cacheleak.example/posts/2')
    hide_post(alice, bob_only)
    hide_post(bob, alice_only)

    shared_result_id = 'shared-result-id'
    alice_ids = ids_for(app, alice, shared_result_id, [community.id])
    bob_ids = ids_for(app, bob, shared_result_id, [community.id])

    assert alice_ids == [alice_only.id], (
        f'Alice should see only the post she has not hidden; got {alice_ids!r}'
    )
    assert bob_ids == [bob_only.id], (
        f'Bob shared a result_id with Alice and received {bob_ids!r}. Expected '
        f'[{bob_only.id}] -- his own authorised result. Getting '
        f'[{alice_only.id}] means he was served Alice\'s cached feed, including a '
        f'post he had hidden, from a cache key with no user component.'
    )
    assert alice_only.id not in bob_ids
    assert bob_only.id not in alice_ids


def test_each_user_gets_a_separate_cache_key_for_the_same_result_id(
        app, db_session, redis_double):
    """The mechanism behind the test above, asserted on the keys themselves.

    Two authenticated calls with one shared result_id must leave two distinct
    entries in Redis, each named for its own user. Against the unfixed function
    there is exactly ONE key -- the bare result_id -- which is the leak.
    """
    make_instance('cachekeys.example')
    alice = make_user(None, 'cachekeysalice', local=True)
    bob = make_user(None, 'cachekeysbob', local=True)
    community = make_community('cachekeyscomm')
    make_community_member(alice, community)
    make_community_member(bob, community)
    make_post(community, alice, 'https://cachekeys.example/posts/1')

    shared_result_id = 'shared-result-id'
    ids_for(app, alice, shared_result_id, [community.id])
    ids_for(app, bob, shared_result_id, [community.id])

    assert sorted(redis_double.keys('*')) == sorted([
        f'feed:{alice.id}:{shared_result_id}',
        f'feed:{bob.id}:{shared_result_id}',
    ]), (
        f'expected one cache key per user; got {sorted(redis_double.keys("*"))!r}. '
        f'A single key named {shared_result_id!r} means the cache is not scoped '
        f'to a user and both sessions share one entry.'
    )


def test_an_anonymous_call_is_not_served_a_cache_entry_named_by_the_client(
        app, db_session, redis_double):
    """The same leak from the read side, and the poisoning direction with it.

    An anonymous request also carries a client-supplied `?result_id=`
    (`app/main/routes.py:83` only substitutes None when the caller is
    authenticated -- `app/main/routes.py:1257` passes a `gibberish(15)` result_id
    for anyone). Before the fix, an anonymous viewer's read was guarded by `if
    result_id:` alone, so a key named by the client was read and returned to a
    session that had never been authorised for its contents.

    Priming the bare `result_id` with a value no live query could produce is what
    makes this discriminate: if the cache-read still fires for an anonymous
    caller, the stale value comes back and the assertion names it.
    """
    make_instance('anoncache.example')
    author = make_user(None, 'anoncacheauthor', local=True)
    community = make_community('anoncachecomm')
    make_community_member(author, community)
    post = make_post(community, author, 'https://anoncache.example/posts/1')

    result_id = 'attacker-chosen-result-id'
    stale = [-424242]  # not a real post id, and not reachable by any query
    redis_double.set(result_id, json.dumps(stale), ex=86400)

    returned = anonymous_ids(app, result_id, [community.id])

    assert returned == [post.id], (
        f'an anonymous caller was served {returned!r} from the client-named key '
        f'{result_id!r}; expected the live result [{post.id}]'
    )
    assert redis_double.get(result_id) == json.dumps(stale), \
        'the anonymous call should neither read nor overwrite the client-named key'


# --- 2. No wasted write -------------------------------------------------------

def test_an_authenticated_call_with_an_empty_result_id_writes_no_cache_entry(
        app, db_session, redis_double):
    """The read/write asymmetry, closed.

    Before the fix the READ was guarded by `if result_id:` while the WRITE was
    guarded only by `if current_user.is_authenticated:`. An authenticated call
    with an empty result_id -- the caller's own way of saying "do not cache" --
    therefore wrote a 24-hour entry under a key literally named '', which the read
    path could never return. Two real call sites pass a literal '':
    `app/feed/routes.py:719` (show_feed_rss) and `app/topic/routes.py:230`
    (show_topic_rss). Neither carries @login_required, so a logged-in browser
    opening either RSS URL reached exactly this path.

    Deriving one `cache_key` for both guards makes an empty result_id neither read
    nor write. Asserting on the whole keyspace rather than just `exists('')` also
    catches a fix that merely renamed the wasted key.
    """
    make_instance('emptyresultid.example')
    viewer = make_user(None, 'emptyresultidviewer', local=True)
    community = make_community('emptyresultidcomm')
    make_community_member(viewer, community)
    post = make_post(community, viewer, 'https://emptyresultid.example/posts/1')

    assert redis_double.dbsize() == 0
    returned = ids_for(app, viewer, '', [community.id])

    assert returned == [post.id]
    assert redis_double.keys('*') == [], (
        f'an empty result_id means "do not cache", but the call wrote '
        f'{redis_double.keys("*")!r}'
    )


def test_an_anonymous_call_with_a_result_id_writes_no_cache_entry(
        app, db_session, redis_double):
    """The other half of the same derived condition: an unauthenticated caller has
    no user to scope a key to, so it must not write one either. This held before
    the fix (the write was already guarded by `is_authenticated`) and is pinned
    here so the derived `cache_key` cannot silently widen it.
    """
    make_instance('anonwrite.example')
    author = make_user(None, 'anonwriteauthor', local=True)
    community = make_community('anonwritecomm')
    make_community_member(author, community)
    post = make_post(community, author, 'https://anonwrite.example/posts/1')

    assert redis_double.dbsize() == 0
    returned = anonymous_ids(app, uuid.uuid4().hex, [community.id])

    assert returned == [post.id]
    assert redis_double.keys('*') == []


# --- 3. The cache still works -------------------------------------------------

def test_a_users_own_cached_result_is_served_without_re_querying(
        app, db_session, redis_double):
    """The cache-hit path, under the new key format.

    The primed value is deliberately NOT what a live query returns -- a real post
    exists in the community, so falling through to the query would give
    `[post.id]`. Asserting the STALE value comes back is what distinguishes a
    genuine cache hit from a test that would pass either way.

    Mutation that would fail this: delete the `if cache_key and
    redis_client.exists(cache_key): return json.loads(...)` block, and the call
    returns [post.id] instead.
    """
    make_instance('ownhit.example')
    viewer = make_user(None, 'ownhitviewer', local=True)
    community = make_community('ownhitcomm')
    make_community_member(viewer, community)
    post = make_post(community, viewer, 'https://ownhit.example/posts/1')

    result_id = uuid.uuid4().hex
    stale_cached_ids = [-999999]
    redis_double.set(f'feed:{viewer.id}:{result_id}', json.dumps(stale_cached_ids), ex=86400)

    returned = ids_for(app, viewer, result_id, [community.id])

    assert returned == stale_cached_ids, (
        f'expected the primed cache value {stale_cached_ids} to be served as-is; got '
        f'{returned!r}. A live query here would have returned [{post.id}]'
    )
    assert post.id not in returned


def test_a_second_call_by_the_same_user_reuses_the_first_calls_entry(
        app, db_session, redis_double):
    """Round trip: the write the function performs is readable by the read the
    same function performs, for the same user and result_id.

    The discriminator is the row created BETWEEN the two calls. A second live
    query would pick up `later_post`; only a real cache hit returns the first
    call's list. This is what proves read and write agree on the key -- an
    asymmetry (the old defect's shape) makes the second call re-query and see
    `later_post`, failing here.
    """
    make_instance('roundtrip.example')
    viewer = make_user(None, 'roundtripviewer', local=True)
    community = make_community('roundtripcomm')
    make_community_member(viewer, community)
    first_post = make_post(community, viewer, 'https://roundtrip.example/posts/1')

    result_id = uuid.uuid4().hex
    first = ids_for(app, viewer, result_id, [community.id])
    assert first == [first_post.id]

    later_post = make_post(community, viewer, 'https://roundtrip.example/posts/2')
    second = ids_for(app, viewer, result_id, [community.id])

    assert second == [first_post.id], (
        f'expected the cached list {[first_post.id]} again; got {second!r}. '
        f'Seeing {later_post.id} means the second call re-queried, i.e. the read '
        f'and the write disagree about the cache key.'
    )
    assert later_post.id not in second


# --- 4. The five real call sites, end to end ---------------------------------

def login(client, user):
    """Flask-Login's session cookie, set directly -- the same shape
    tests/test_redirect_back.py uses.
    """
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def make_feed(owner, community, name='cacheroutefeed'):
    """A public Feed carrying one community, plus its FeedItem.

    Local (instance_id 1) and public so /f/<name> and /f/<name>.rss both resolve
    it without a login. No factory exists for Feed; this is the minimum
    get_deduped_post_ids's two feed call sites need.
    """
    feed = Feed(
        user_id=owner.id, title=name, name=name, machine_name=name,
        instance_id=1, public=True, num_communities=1, subscriptions_count=0,
        ap_profile_id=f'https://test.piefed.local/f/{name}',
        ap_public_url=f'https://test.piefed.local/f/{name}',
        ap_followers_url=f'https://test.piefed.local/f/{name}/followers',
        ap_domain='test.piefed.local',
    )
    db.session.add(feed)
    db.session.commit()
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    db.session.commit()
    return feed


def make_topic(community, name='cacheroutetopic'):
    """A Topic with one community attached, for /topic/<name> and its .rss."""
    topic = Topic(machine_name=name, name=name, num_communities=1)
    db.session.add(topic)
    db.session.commit()
    community.topic_id = topic.id
    db.session.commit()
    return topic


class TestEveryCallSiteStillRenders:
    """The three web routes and the two RSS routes that call
    get_deduped_post_ids, driven end to end.

    The web routes read `?result_id=` from the query string and echo it back into
    their own pagination links, which is what made a shared page-2 link enough to
    share a cache entry. The RSS routes pass a literal '' and must therefore
    write nothing at all.

    These assert on status codes and on the keyspace -- what the routes render and
    what they leave in Redis -- not on the SQL underneath.
    """

    def test_the_home_route_scopes_its_cache_entry_to_the_logged_in_user(
            self, app, db_session, site, redis_double):
        make_instance('homeroute.example')
        viewer = make_user(None, 'homerouteviewer', local=True)
        community = make_community('homeroutecomm')
        make_community_member(viewer, community)
        make_post(community, viewer, 'https://homeroute.example/posts/1')

        client = app.test_client()
        login(client, viewer)
        response = client.get('/?result_id=shared-result-id')

        assert response.status_code == 200
        assert redis_double.keys('*') == [f'feed:{viewer.id}:shared-result-id']

    def test_the_feed_route_scopes_its_cache_entry_to_the_logged_in_user(
            self, app, db_session, site, redis_double):
        make_instance('feedroute.example')
        viewer = make_user(None, 'feedrouteviewer', local=True)
        community = make_community('feedroutecomm')
        make_community_member(viewer, community)
        make_post(community, viewer, 'https://feedroute.example/posts/1')
        feed = make_feed(viewer, community)

        client = app.test_client()
        login(client, viewer)
        response = client.get(f'/f/{feed.name}?result_id=shared-result-id')

        assert response.status_code == 200
        assert redis_double.keys('*') == [f'feed:{viewer.id}:shared-result-id']

    def test_the_topic_route_scopes_its_cache_entry_to_the_logged_in_user(
            self, app, db_session, site, redis_double):
        make_instance('topicroute.example')
        viewer = make_user(None, 'topicrouteviewer', local=True)
        community = make_community('topicroutecomm')
        make_community_member(viewer, community)
        make_post(community, viewer, 'https://topicroute.example/posts/1')
        topic = make_topic(community)

        client = app.test_client()
        login(client, viewer)
        response = client.get(f'/topic/{topic.machine_name}?result_id=shared-result-id')

        assert response.status_code == 200
        assert redis_double.keys('*') == [f'feed:{viewer.id}:shared-result-id']

    def test_the_feed_rss_route_caches_nothing_for_a_logged_in_reader(
            self, app, db_session, site, redis_double):
        """show_feed_rss passes a literal '' result_id (app/feed/routes.py:719).
        Neither route carries @login_required, so a logged-in browser opening the
        RSS URL used to leave a 24-hour key named '' behind on every request.
        """
        make_instance('feedrss.example')
        viewer = make_user(None, 'feedrssviewer', local=True)
        community = make_community('feedrsscomm')
        make_community_member(viewer, community)
        make_post(community, viewer, 'https://feedrss.example/posts/1')
        feed = make_feed(viewer, community, name='cacheroutersssfeed')

        client = app.test_client()
        login(client, viewer)
        response = client.get(f'/f/{feed.machine_name}.rss')

        assert response.status_code == 200
        assert redis_double.keys('*') == []

    def test_the_topic_rss_route_caches_nothing_for_a_logged_in_reader(
            self, app, db_session, site, redis_double):
        """show_topic_rss passes a literal '' result_id (app/topic/routes.py:230)
        -- the second of the two wasted-write call sites.
        """
        make_instance('topicrss.example')
        viewer = make_user(None, 'topicrssviewer', local=True)
        community = make_community('topicrsscomm')
        make_community_member(viewer, community)
        make_post(community, viewer, 'https://topicrss.example/posts/1')
        topic = make_topic(community, name='cacheroutersstopic')

        client = app.test_client()
        login(client, viewer)
        response = client.get(f'/topic/{topic.machine_name}.rss')

        assert response.status_code == 200
        assert redis_double.keys('*') == []
