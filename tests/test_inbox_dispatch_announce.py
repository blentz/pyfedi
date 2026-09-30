"""Sub-project 5a, Task 4 -- the Announce unwrap, routes.py:894-933.

Six outcomes off the same `if request_json['type'] == 'Announce':` block:

  1. object is a bare string (896-900)      -> delegates to process_announce_of_uri
  2. object is a list (901-907)             -> recurses once per element
  3. object is an OrderedCollection (908-913) -> recurses once per orderedItems entry
  4. inner actor is banned (916-919)        -> refused
  5. inner actor is unfound/blocked (920-922) -> refused
  6. otherwise (914, 923-930)               -> `user` is set (or cleared for a
     feed) and `announced`/`core_activity` are set for every arm downstream

Every test resolves the OUTER actor first, exactly the way routes.py:861-870
requires before line 894 is ever reached: a Community via the community_only
lookup (`_seed_announcing_community`) for the tests where the inner-actor walk
must run (feed is None there, so line 914's `if not feed:` is True), or a Feed
via the feed_only lookup (`_seed_announcing_feed`) for the one test that needs
the walk SKIPPED.

The two recursion tests (Step 2) let `process_inbox_request` call itself for
real -- monkeypatching the dispatcher itself was explicitly ruled out by the
brief (it would double the function under test) -- and prove both elements
were processed via two independent ActivityPubLog rows, distinguished by
their `exception_message` text (each inner actor's own `ap_id`).
"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub import util as activitypub_util
from app.models import ActivityPubLog, Community, utcnow
from tests.factories import inbox_activity, make_community, make_feed, make_instance, make_site, \
    make_user
from tests.test_inbox_dispatch_preamble import dispatch


def _seed_announcing_community(host='peer.example'):
    """A Community resolvable as the OUTER Announce actor (routes.py:862).

    Returns (instance, community) -- the instance is handed back so callers
    can seed additional inner-object actors (bob, carol, ...) on the same
    host without a second make_instance() call. Mirrors Task 1/2's pattern
    (tests/test_inbox_dispatch_preamble.py) exactly: owner + instance seeded
    first because make_community() hardcodes owner user_id=1/instance_id=1,
    and ap_fetched_at is stamped so find_actor_or_create_cached's
    schedule_actor_refresh does not fire a real fetch inline under eager
    Celery.
    """
    make_site()
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    community = make_community(host=host)
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return instance, community


def _seed_announcing_feed(host='peer.example'):
    """A Feed resolvable as the OUTER Announce actor (routes.py:864), for the
    one test that needs `feed` truthy so the inner-actor walk (914-922) is
    skipped. Only ever called with its default host, so make_feed(instance)
    (tests/factories.py) seeds the same row.
    """
    make_site()
    instance = make_instance(host)
    feed = make_feed(instance)
    return instance, feed


# --- Step 1: the bare-string object (routes.py:896-900) ---


def test_an_announce_of_a_bare_uri_delegates_to_process_announce_of_uri(
        app, db_session, monkeypatch):
    """routes.py:896-900. process_announce_of_uri logs its own outcome on
    every path (its docstring says so), so this asserts the call and all
    four of its arguments with a recorder, not a log row -- proving the
    exact call routes.py:899 makes: `process_announce_of_uri(request_json,
    community, id, store_ap_json)`.
    """
    instance, community = _seed_announcing_community()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_announce_of_uri',
                         lambda *args: calls.append(args))

    activity = inbox_activity(community, activity_type='Announce',
                              object_uri='https://peer.example/objects/1')

    dispatch(activity, store_ap_json=True)

    assert len(calls) == 1
    request_json_arg, community_arg, id_arg, store_ap_json_arg = calls[0]
    assert request_json_arg is activity
    assert community_arg is not None and community_arg.id == community.id
    assert id_arg == activity['id']
    assert store_ap_json_arg is True


# --- Step 2: the two recursive arms, running for real ---


def test_an_announce_of_a_list_processes_every_element(app, db_session, monkeypatch):
    """routes.py:901-907. Each element is rebuilt into its own single-object
    Announce (same 'id' and 'actor' as the parent, per the `request_json.copy()`
    at 904) and passed back into process_inbox_request.

    The recursion RUNS: process_inbox_request is never monkeypatched here.
    Two DIFFERENT banned inner actors are used as the two elements, each
    already seeded in the database, so each recursive call resolves its own
    actor with no HTTP fetch and reaches a refusal naming that element's own
    actor -- proof both elements were independently processed, not just that
    the loop ran twice.

    NOTE (discovered while writing this test, not asserted by the brief):
    a banned actor here refuses via routes.py:920-922 ('Blocked or unfound
    user...'), NOT via 916-919's own `if user.banned:` check. find_actor_by_url
    (app/activitypub/actor.py) filters banned actors out at the lookup itself
    -- `if actor.banned and not allow_banned: return False`, which
    find_actor_or_create turns into a plain None -- so find_actor_or_create_cached
    at line 915 never hands routes.py a banned User object through this
    ordinary path; `user` is None and control falls to 920-922 instead,
    observably identical to a URL that resolves to nothing at all. See
    test_an_announce_whose_inner_actor_is_banned_is_refused below for how
    916-919 is actually reached (only via a stale ID-cache hit -- unreachable
    in this suite's own NullCache test config through any ordinary lookup).
    Banned actors (rather than plain unknown URLs) are used here purely so
    the lookup is a database read with no HTTP fetch involved at all.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community = _seed_announcing_community()
    bob = make_user(instance, 'bob')
    bob.banned = True
    bob.ap_fetched_at = utcnow()
    carol = make_user(instance, 'carol')
    carol.banned = True
    carol.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(community, activity_type='Announce',
                              object=[{'actor': bob.ap_profile_id},
                                      {'actor': carol.ap_profile_id}])

    dispatch(activity)

    logs = ActivityPubLog.query.all()
    assert len(logs) == 2
    messages = {log.exception_message for log in logs}
    assert messages == {
        'Blocked or unfound user for Announce object actor ' + bob.ap_profile_id,
        'Blocked or unfound user for Announce object actor ' + carol.ap_profile_id}


def test_an_announce_of_an_ordered_collection_processes_every_item(
        app, db_session, monkeypatch):
    """routes.py:908-913, the same shape over `orderedItems`. Identical
    mechanism and assertion strategy to the list test above -- two distinct
    banned inner actors, two distinct ActivityPubLog rows -- because the
    recursive call for each item is byte-for-byte the same
    `process_inbox_request(fake_activity, store_ap_json)` call the list arm
    makes. See that test's docstring for why this lands on 920-922 rather
    than 916-919.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community = _seed_announcing_community()
    bob = make_user(instance, 'bob')
    bob.banned = True
    bob.ap_fetched_at = utcnow()
    carol = make_user(instance, 'carol')
    carol.banned = True
    carol.ap_fetched_at = utcnow()
    db.session.commit()

    activity = inbox_activity(
        community, activity_type='Announce',
        object={'type': 'OrderedCollection',
                'orderedItems': [{'actor': bob.ap_profile_id},
                                 {'actor': carol.ap_profile_id}]})

    dispatch(activity)

    logs = ActivityPubLog.query.all()
    assert len(logs) == 2
    messages = {log.exception_message for log in logs}
    assert messages == {
        'Blocked or unfound user for Announce object actor ' + bob.ap_profile_id,
        'Blocked or unfound user for Announce object actor ' + carol.ap_profile_id}


# --- Step 3: the two formerly unguarded reads (D51, D52, fixed) ---


def test_an_ordered_collection_without_ordered_items_is_refused(app, db_session, monkeypatch):
    """D51, fixed. The OrderedCollection unwrap was chosen on
    `type == 'OrderedCollection'` alone and then iterated
    `request_json['object']['orderedItems']` with no check that the key
    exists, so a collection without it raised KeyError, uncaught, with no
    log row. It is now refused and logged before any recursion.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community = _seed_announcing_community()

    activity = inbox_activity(community, activity_type='Announce',
                              object={'type': 'OrderedCollection'})

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Announced OrderedCollection has no orderedItems list'


def test_an_announce_whose_inner_object_has_no_actor_is_refused(app, db_session, monkeypatch):
    """D52, fixed. When `feed` is falsy the unwrap reads the inner object's
    'actor' to find the user. An inner dict with no 'actor' key (object={},
    which skips the str/list/OrderedCollection arms) raised KeyError there,
    uncaught, with no log row. It is now refused and logged.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community = _seed_announcing_community()

    activity = inbox_activity(community, activity_type='Announce', object={})

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Announce object has no actor'


@pytest.mark.parametrize('missing', ['id', 'type'])
def test_an_announce_whose_inner_activity_has_no_id_or_type_is_refused(
        app, db_session, monkeypatch, missing):
    """D127, fixed. The gate checks the outer activity's id and type, but the
    unwrap handed the inner object to every arm as `core_activity` without
    re-checking its own, and the arms read `core_activity['type']` and
    `core_activity['id']` unguarded -- an Announce{Create{ChatMessage}} whose
    inner Create had no id reached process_chat and raised KeyError. The
    unwrap now refuses an inner activity missing either key, once, instead
    of every arm having to guard its own read. Nothing is dispatched.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community = _seed_announcing_community()
    bob = make_user(instance, 'bob')
    bob.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_chat', lambda *args: calls.append(args))

    inner = {'id': 'https://peer.example/activities/create/1', 'type': 'Create',
             'actor': bob.ap_profile_id,
             'object': {'id': 'https://peer.example/chat/1', 'type': 'ChatMessage',
                        'attributedTo': bob.ap_profile_id, 'content': 'hi'}}
    del inner[missing]
    activity = inbox_activity(community, activity_type='Announce', object=inner)

    dispatch(activity)

    assert calls == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Announce object has no id or type'


# --- Step 4: the inner-actor walk and what it sets ---


def test_an_announce_whose_inner_actor_is_banned_is_refused(app, db_session, monkeypatch):
    """A banned inner actor served from a STALE ID-cache entry is refused at
    the lookup itself (D59, fixed).

    find_actor_or_create_cached's Redis fast path caches only (id, type) and
    re-fetches by primary key. Before D59 that re-fetch applied no banned
    filter, so an actor resolved in good standing and banned within the
    cache's 10-minute window came back as a live User, and only routes.py's
    own `if user.banned:` backstop stopped it. The cache hit now applies the
    same checks as the uncached path, so the refusal happens in the lookup and
    routes.py logs the "Blocked or unfound user" arm. The backstop stays as
    defence in depth.

    This suite's CACHE_TYPE is 'NullCache', so a warm cache is simulated by
    monkeypatching `_find_actor_id_cached` itself to return this one banned
    user's (id, 'User') for exactly this actor's URL, delegating every other
    URL to the real function. process_inbox_request and
    find_actor_or_create_cached both run for real.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community = _seed_announcing_community()
    banned_user = make_user(instance, 'bob')
    banned_user.ap_fetched_at = utcnow()
    db.session.commit()
    user_id = banned_user.id
    user_url = banned_user.ap_profile_id

    real_find_actor_id_cached = activitypub_util._find_actor_id_cached

    def stale_cache_hit(actor_url, community_only, feed_only):
        if actor_url == user_url and not community_only and not feed_only:
            return (user_id, 'User')
        return real_find_actor_id_cached(actor_url, community_only, feed_only)

    monkeypatch.setattr(activitypub_util, '_find_actor_id_cached', stale_cache_hit)

    # Ban AFTER seeding the (simulated) cache hit, so the cache is "stale"
    # relative to the ban -- the scenario 916-919 exists to catch.
    banned_user.banned = True
    db.session.commit()

    activity = inbox_activity(community, activity_type='Announce',
                              object={'actor': user_url})

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == \
        f'Blocked or unfound user for Announce object actor {user_url}'


def test_an_announce_whose_inner_actor_is_unfound_is_refused(
        app, db_session, http_mock, monkeypatch):
    """routes.py:920-922. The inner actor URL is well-formed and not blocked
    (validate_remote_actor passes), but no row exists for it, so
    find_actor_or_create_cached's default create_if_not_found=True attempts
    a real fetch (routes.py:915 passes no kwargs) -- served here as a 404 via
    http_mock so the failure is genuine ("not found"), not an infrastructure
    kill from a blocked, unmocked request (the standing rule from Task 2:
    respx.models.AllMockedAssertionError would be an infrastructure kill,
    not a behavioural one). A 404 makes fetch_remote_actor_data
    (app/activitypub/actor.py) return None, so create_actor_from_remote and
    thus find_actor_or_create_cached return None, and routes.py falls to the
    'Blocked or unfound user' refusal at 921 -- the same message this
    function logs for a genuinely blocked actor, since both paths return
    None from the same call.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community = _seed_announcing_community()

    ghost_url = 'https://peer.example/u/ghost'
    http_mock.get(ghost_url).respond(404)

    activity = inbox_activity(community, activity_type='Announce',
                              object={'actor': ghost_url})

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == \
        'Blocked or unfound user for Announce object actor ' + ghost_url


def test_an_announce_whose_inner_actor_is_a_community_is_refused(
        app, db_session, monkeypatch):
    """routes.py:916's compound guard has an untested, non-equivalent half.

    `if user and isinstance(user, User):` -- unlike the preamble's
    `actor and isinstance(actor, User)` / `actor and isinstance(actor,
    Community)` at routes.py:872/874 (D61: genuine equivalent mutants,
    because those two lookups are NARROWED with community_only=True /
    feed_only=True before this point), the lookup that feeds line 916
    (`user = find_actor_or_create_cached(request_json['object']['actor'])`,
    routes.py:915) passes no such kwargs -- it is UNNARROWED, so an inner
    Announce object whose 'actor' resolves to a Community (or a Feed) is a
    live, reachable input to this guard, not dead code.

    Seeding a second, non-banned Community as the inner actor exercises
    exactly that: `find_actor_or_create_cached` returns the Community, the
    `isinstance(user, User)` half of the guard is False, so control falls to
    the `else` at 920-922 and the Announce is refused as 'Blocked or unfound
    user for Announce object actor ...' -- even though `user` (bound to the
    Community) was, in fact, found.

    MUTATION: routes.py:916 was temporarily changed from
    `if user and isinstance(user, User):` to `if user:`, with the rest of
    the guard's body (917-922) left untouched, and the full file's tests
    (including this one) re-run. Under the mutant, the truthy Community
    takes the True branch instead of the False one: `user.banned` is False
    (Community also has a `banned` column, so no AttributeError masks the
    mutant on that line), so no refusal is logged at 920-922 and execution
    falls through to `announced = True; core_activity = request_json['object']`
    -- this test's inner object is `{'actor': ...}` with no `'type'` key
    (there is no refusal left to stop it), so the dispatcher's next check,
    `if core_activity['type'] == 'Follow':` (routes.py:935), raises
    `KeyError: 'type'`, uncaught, and this test fails on that exception
    rather than on its own assertion (OBSERVED: `1 failed, 9 passed`,
    `KeyError: 'type'` at routes.py:935 -- not the `NoResultFound` on
    `ActivityPubLog.query.one()` a graceful zero-rows outcome would have
    produced). The mutant is still killed either way: this test fails under
    it and passes against the real guard. Restored immediately after,
    verified via `git diff --stat app/` producing no output.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, community = _seed_announcing_community()
    inner_community = make_community(name='innerclub', host='peer.example')
    inner_community.ap_fetched_at = utcnow()
    db.session.commit()

    assert isinstance(inner_community, Community)

    activity = inbox_activity(community, activity_type='Announce',
                              object={'actor': inner_community.ap_profile_id})

    dispatch(activity)

    assert ActivityPubLog.query.one().exception_message == \
        'Blocked or unfound user for Announce object actor ' + inner_community.ap_profile_id


def test_an_announce_from_a_feed_skips_the_inner_actor_walk(
        app, db_session, monkeypatch):
    """routes.py:914 and :923-924 -- `if not feed:` guards the whole walk
    (915-922), and the feed path sets `user = None` instead of running it.

    The inner object's actor is a BANNED user that would trip 917-919's
    refusal if the walk ran -- so reaching process_upvote at all (rather
    than the banned refusal) is direct evidence the walk did not run, and
    `user` arriving as None is evidence of the 924 assignment specifically
    (not just a skip that left `user` as whatever it was before, which
    would also be None here since the preamble never sets it for a feed
    actor -- see routes.py:858).
    """
    instance, feed = _seed_announcing_feed()
    banned_user = make_user(instance, 'bob')
    banned_user.banned = True
    banned_user.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote',
                         lambda *args: calls.append(args))

    activity = inbox_activity(feed, activity_type='Announce',
                              object={'id': 'https://peer.example/activities/like/1',
                                      'actor': banned_user.ap_profile_id, 'type': 'Like'})

    dispatch(activity)

    assert len(calls) == 1
    user_arg, store_ap_json_arg, request_json_arg, announced_arg = calls[0]
    assert user_arg is None
    assert announced_arg is True


def test_an_announce_sets_core_activity_to_the_inner_object(app, db_session, monkeypatch):
    """routes.py:928-930 -- `announced = True` and `core_activity` becomes
    the inner object, which is what every arm downstream dispatches on.

    Asserted by sending an Announce of a Like and observing the Like arm's
    outcome: process_upvote (routes.py:1329) is called with
    `(user, store_ap_json, request_json, announced)`. Without the 929
    assignment, `core_activity` would still be the outer Announce
    (routes.py:932's else-branch shape), and `core_activity['type'] ==
    'Like'` at line 1328 would be False -- the dispatcher would fall through
    every remaining arm and process_upvote would never be called at all.
    `request_json_arg is activity` proves the OUTER activity (not the inner
    Like object) is what process_upvote actually receives, exactly as
    routes.py:1329 passes `request_json`, not `core_activity`.
    """
    instance, community = _seed_announcing_community()
    inner_actor = make_user(instance, 'inner_liker')
    inner_actor.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote',
                         lambda *args: calls.append(args))

    activity = inbox_activity(
        community, activity_type='Announce',
        object={'actor': inner_actor.ap_profile_id, 'type': 'Like',
                'id': 'https://peer.example/activities/like-1',
                'object': 'https://peer.example/objects/1'})

    dispatch(activity)

    assert len(calls) == 1
    user_arg, store_ap_json_arg, request_json_arg, announced_arg = calls[0]
    assert user_arg is not None and user_arg.id == inner_actor.id
    assert announced_arg is True
    assert request_json_arg is activity
