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


# --- Step 3: the two unguarded reads -- observations, not fixes ---


def test_an_ordered_collection_without_ordered_items(app, db_session, monkeypatch):
    """routes.py:909 -- `request_json['object']['orderedItems']` is reached
    on `type == 'OrderedCollection'` alone, with no membership check on
    'orderedItems' itself first.

    OBSERVED: with object={'type': 'OrderedCollection'} (no 'orderedItems'
    key), the `for obj in request_json['object']['orderedItems']:` line
    raises `KeyError: 'orderedItems'` immediately, uncaught inside
    process_inbox_request's own try block, propagating through routes.py's
    `except Exception: session.rollback(); raise` and out of dispatch() -- a
    real, uncaught 500-shaped failure, not a logged refusal. No
    ActivityPubLog row is written.
    """
    instance, community = _seed_announcing_community()

    activity = inbox_activity(community, activity_type='Announce',
                              object={'type': 'OrderedCollection'})

    with pytest.raises(KeyError, match='orderedItems'):
        dispatch(activity)


def test_an_announce_whose_inner_object_has_no_actor(app, db_session, monkeypatch):
    """routes.py:915 -- `request_json['object']['actor']`, unguarded, on a
    peer-supplied inner object, reached whenever `feed` is falsy (914).

    OBSERVED: with object={} (a dict, so it skips the str/list/
    OrderedCollection arms above it, but carries no 'actor' key), the line
    raises `KeyError: 'actor'` immediately, uncaught inside
    process_inbox_request's own try block -- the same uncaught 500-shaped
    failure as the probe above, not a fall to the 920-922 refusal. No
    ActivityPubLog row is written.
    """
    instance, community = _seed_announcing_community()

    activity = inbox_activity(community, activity_type='Announce', object={})

    with pytest.raises(KeyError, match='actor'):
        dispatch(activity)


# --- Step 4: the inner-actor walk and what it sets ---


def test_an_announce_whose_inner_actor_is_banned_is_refused(app, db_session, monkeypatch):
    """routes.py:916-919.

    Reaching this branch through find_actor_or_create_cached's ORDINARY path
    is impossible: find_actor_by_url (app/activitypub/actor.py) filters a
    banned actor out at the lookup itself (`if actor.banned and not
    allow_banned: return False`), which find_actor_or_create turns into a
    plain None before routes.py ever sees an object to call `.banned` on --
    see the discovery noted in test_an_announce_of_a_list_processes_every_element
    above, where a banned inner actor lands on 920-922 instead. The ONLY way
    routes.py can observe `user.banned == True` here is a STALE hit in
    find_actor_or_create_cached's Redis ID cache (app/activitypub/util.py:
    313-321): the fast path at lines 355-360 re-fetches the model by primary
    key with `db.session.get`, which applies NO banned filter at all, once
    `_find_actor_id_cached` has already returned a hit. That is a real
    production scenario (a user resolved while in good standing, then banned
    within the cache's 10-minute window) but is UNREACHABLE in this suite's
    own test config, where CACHE_TYPE is 'NullCache' (tests/conftest.py) --
    `_find_actor_id_cached` never actually caches, so it always re-runs the
    filtering lookup fresh and never returns a stale hit.

    So this test simulates a warm cache by monkeypatching
    `_find_actor_id_cached` itself (a private collaborator inside
    app/activitypub/util.py, not process_inbox_request) to return this one
    banned user's (id, 'User') for exactly this actor's URL -- reproducing
    the shape a real warm cache would hand back -- while delegating every
    other URL to the real function unchanged. This is not doubling the
    function under test: process_inbox_request and find_actor_or_create_cached
    both run for real; only the innermost cache lookup is puppeted to have
    the one stale entry a live Redis cache could have produced.
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

    assert ActivityPubLog.query.one().exception_message == f'{banned_user.ap_id} is banned'


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
                              object={'actor': banned_user.ap_profile_id, 'type': 'Like'})

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
