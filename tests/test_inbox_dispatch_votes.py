"""Sub-project 5a, Task 5 -- the vote arms (routes.py:1328-1334) and their two
delegates, process_upvote (:2387-2410) and process_downvote (:2413-2433).

Step 1's two tests drive the dispatcher itself (via `dispatch`, Task 1's
helper), proving Like/EmojiReact and Dislike land on the right delegate with
the right four arguments. Every other test in this file calls process_upvote
/ process_downvote directly -- there is no actor-resolution concern left to
prove once Step 1 has shown the dispatcher's own arms wire correctly, and a
direct call keeps each test's seeded rows scoped to exactly the guard it
means to exercise.

Step 3's two tests pin a real asymmetry between the two delegates: an inner
`if` that blocks an upvote (blocked_users or over VOTE_QUOTA) has NO `else`
(routes.py:2404-2408), so it logs nothing at all, while the identical
downvote guard (:2413-2433) has an `else` at :2430-2431 that logs
'Cannot downvote this' / APLOG_IGNORED. Registered as a finding for Task 9,
not fixed here.

Step 4 drops each half of the two vote guards one mutation at a time:

    outer:  can_upvote(user, liked.community) and not instance_banned(user.instance.domain)
    inner:  isinstance(liked, (Post, PostReply)) and user.id not in blocked_users(liked.author.id)
            and votes_cast_today(user.id) <= current_app.config['VOTE_QUOTA']

Four of the five halves get a distinct killer test below (a bot voter, a
banned instance, an author's block of the voter, and an over-quota voter --
the last one being Step 3's own asymmetry test, since it exercises the exact
same guard). The fifth half, `isinstance(liked, (Post, PostReply))`, is an
EQUIVALENT MUTANT: find_liked_object's own return type is `Post | PostReply |
None` (app/activitypub/util.py:2024), and `liked is None` is already handled
by the early return two lines above (:2400-2402) -- by the time execution
reaches the inner `if`, `liked` can only ever be a Post or a PostReply, so
`isinstance(liked, (Post, PostReply))` is always True and dropping it changes
nothing reachable from process_upvote's own call sites. Verified empirically
(see task-5-report.md for the mutation run and its outcome), not merely
argued -- following the precedent set by
tests/test_inbox_dispatch_preamble.py's Task 3 equivalent-mutant findings.
Registered for Task 9; not fixed here.

All mutations were run one at a time against `app/activitypub/routes.py` and
restored immediately after, verified via `git diff --stat app/` producing no
output before this file's own change was committed.

Task 6 adds the other two vote delegates, process_poll_vote (:2436-2460) and
process_question_answer (:2463-2496).

process_question_answer does `from app import redis_client` INSIDE the
function body (:2475), then `redis_client.lock(...)`. This is exactly
tests/conftest.py:394's `redis_double` fixture's SECOND documented mechanism,
not its first: the fixture's docstring distinguishes `get_redis_connection`
(imported as `from app.utils import get_redis_connection` at four separate
module-level binding sites, each bound once at import time, so each needs its
own patch) from `app.redis_client` (imported as `from app import
redis_client` INSIDE a function body at ~14 call sites, re-executed on every
call, so patching the single `app.redis_client` attribute redirects all of
them). `grep -n 'from app import.*redis_client'
app/activitypub/routes.py` confirms process_question_answer's import at
:2475 is one of those in-function sites, not a module-level one -- so
`redis_double` alone (no additional patching) is confirmed sufficient here.
Every process_question_answer test below that reaches the success path (and
therefore the `with redis_client.lock(...)` block) needs `app.redis_client`
patched by SOME double; the refusal-only tests never reach the lock and do
not need one.

CORRECTION discovered while running the success-path tests: `redis_double`
itself (a real `fakeredis.FakeRedis` instance) cannot be used here, even
though the ATTRIBUTE it patches (`app.redis_client`) is exactly right per
the paragraph above. Confirmed by direct probe:
`fakeredis.FakeRedis().eval('return 1', 0)` raises `ResponseError: unknown
command 'eval'` -- this environment's pinned fakeredis (2.37.1, no `lupa`
installed, per `requirements-test.txt`) implements no Lua scripting AT ALL.
redis-py's `Lock.release()` needs a Lua script (called via EVALSHA) to
atomically check its token before deleting the key, so
`with redis_client.lock(...):` against `redis_double`'s fakeredis instance
ACQUIRES cleanly (plain SET NX PX, no Lua needed) but raises
`redis.exceptions.ResponseError: unknown command 'evalsha'` on `__exit__`,
every single time -- five failures, reproduced first as an actual test run
before this paragraph was written. That gap is in this environment's Redis
stand-in, not in process_question_answer's own logic, and mutual-exclusion
semantics are not what these tests are trying to prove -- only that the
delegate's own branching, Notification, and logging are correct. So the
success-path tests below use `redis_lock_only_double` (defined just below
the imports), which patches that SAME single `app.redis_client` attribute
to a narrower double whose `.lock(...)` is a genuine no-op context manager,
instead of `redis_double`.

Step 2's `test_a_poll_vote_without_choice_text` is an OBSERVATION probe per
the task brief, not a design choice: routes.py:2440 reads
`request_json['choice_text']` (for a non-announced activity) with no `.get`
and no prior guard, immediately after the `ap_id` line and BEFORE
`Post.get_by_ap_id` is ever called -- so a peer that sends a Vote/Note
activity missing `choice_text` raises an unhandled KeyError out of
process_poll_vote, regardless of whether the target post exists. Verified by
running it: the probe test's `pytest.raises(KeyError, match='choice_text')`
passes against the unmodified function. Registered as a finding for Task 9;
not fixed here.

Step 4 drops each piece of process_question_answer's permission guard,
routes.py:2474:

    (not instance_banned(user.instance.domain)) and (
        post_reply.user_id == post_reply.post.user_id
        or post_reply.community.is_moderator(user)
        or post_reply.author.is_instance_admin()
    )

Four pieces, four distinct killers below: the `instance_banned` conjunct
(banned instance + alternative 1 already true, so only the conjunct's own
removal flips the outcome), and each of the three OR-alternatives in
isolation (alternative 1 true alone, alternative 2 true alone, alternative 3
true alone) -- deliberately three DIFFERENT subjects, per the task brief:
alternative 1 compares the reply's author to the POST's author (identity
between two other people, independent of who is acting); alternative 2 asks
whether the ACTING user moderates the community; alternative 3 asks whether
the REPLY's author (not the acting user) is an instance admin. All four
mutations were run one at a time against `app/activitypub/routes.py` and
restored immediately after, verified via `git diff --stat app/` producing no
output before this file's own change was committed. All four were BEHAVIOURAL
kills (a genuine `AssertionError` from this file's own assertions), not
`respx.models.AllMockedAssertionError` infrastructure kills -- there is no
network fetch anywhere on process_question_answer's call path in these
tests (no dispatch/actor-resolution, the community is always local, and
announce_activity_to_followers is monkeypatched to a no-op in every killer
test). The process_poll_vote instance_banned guard (routes.py:2448) was
also mutation-tested the same way (`if not instance_banned(...)` -> `if
True`), killed behaviourally by `test_poll_vote_blocked_by_a_banned_instance`,
restored the same way.
"""
import contextlib

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import process_downvote, process_poll_vote, process_question_answer, process_upvote
from app.constants import NOTIF_ANSWER
from app.models import (ActivityPubLog, BannedInstances, InstanceRole, Notification, Poll, PollChoice,
                        PollChoiceVote, PostReply, PostVote, utcnow)
from tests.factories import (inbox_activity, make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_site, make_user, make_user_block)
from tests.test_inbox_dispatch_preamble import dispatch


class _RedisLockOnlyDouble:
    """A minimal `app.redis_client` double covering ONLY `.lock(...)` used as
    a context manager. See the module docstring's CORRECTION paragraph for
    why `redis_double` itself cannot be used for process_question_answer's
    success path: this environment's fakeredis has no Lua scripting, which
    redis-py's real `Lock.release()` requires.
    """

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


def _seed_vote_scenario(host='peer.example'):
    """A local-owned community (make_community()'s hardcoded owner user_id=1 /
    instance_id=1, so an instance and a user are seeded first to occupy those
    ids -- same pattern as tests/test_inbox_dispatch_preamble.py), an author,
    a voter, and one Post by that author. Returns (voter, post).

    Neither actor needs `ap_fetched_at` stamped here: process_upvote and
    process_downvote take `user` as an already-resolved argument, so no
    find_actor_or_create_cached lookup (and therefore no schedule_actor_refresh)
    ever runs on this path. That stamping only matters for Step 1's dispatch
    tests below, which go through the real preamble.

    The community is local (make_community() never sets ap_id, so
    Community.is_local() is True regardless of `host` -- see
    test_inbox_dispatch_preamble.py's _seed_remote_group_community docstring
    for the same fact) and has no followers, so announce_activity_to_followers
    -- when left unpatched -- reaches its own `following_instances(...)` call,
    finds an empty list, and returns having sent nothing: no network traffic,
    no need for http_mock in tests that don't monkeypatch it away.
    """
    make_site()
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    community = make_community(host=host)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
    post = make_post(community, author, ap_id=f'https://{host}/objects/1')
    db.session.commit()
    return voter, post


# --- Step 1: the dispatcher's two vote arms, routes.py:1328-1334 ---

@pytest.mark.parametrize('activity_type', ['Like', 'EmojiReact'])
def test_like_and_emojireact_dispatch_to_process_upvote(app, db_session, monkeypatch, activity_type):
    """routes.py:1328-1330 -- Like and EmojiReact share one arm into
    process_upvote, called with (user, store_ap_json, request_json, announced).
    Parametrised over both types rather than duplicated, since the arm's
    condition (`core_activity['type'] == 'Like' or core_activity['type'] ==
    'EmojiReact'`) treats them identically.
    """
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote',
                         lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(actor, activity_type=activity_type)

    dispatch(activity)

    assert len(calls) == 1
    user_arg, store_ap_json_arg, request_json_arg, announced_arg = calls[0]
    assert user_arg is not None and user_arg.id == actor.id
    assert store_ap_json_arg is True
    assert request_json_arg == activity
    assert announced_arg is False


def test_dislike_dispatches_to_process_downvote(app, db_session, monkeypatch):
    """routes.py:1332-1334 -- Dislike has its own arm into process_downvote,
    called with the same four arguments as the upvote arm.
    """
    make_site()
    instance = make_instance('peer.example')
    actor = make_user(instance, 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_downvote',
                         lambda *args, **kwargs: calls.append(args))

    activity = inbox_activity(actor, activity_type='Dislike')

    dispatch(activity)

    assert len(calls) == 1
    user_arg, store_ap_json_arg, request_json_arg, announced_arg = calls[0]
    assert user_arg is not None and user_arg.id == actor.id
    assert store_ap_json_arg is True
    assert request_json_arg == activity
    assert announced_arg is False


# --- Step 2: process_upvote end to end, routes.py:2387-2410 ---

def test_upvote_of_an_unfound_object_logs_failure(app, db_session, monkeypatch):
    """routes.py:2400-2402 -- find_liked_object misses, logged as
    APLOG_LIKE/APLOG_FAILURE with the ap_id folded into the message, and the
    function returns before either vote guard runs.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    voter, _post = _seed_vote_scenario()

    ap_id = 'https://peer.example/objects/does-not-exist'
    request_json = {'id': 'https://peer.example/activities/1', 'object': ap_id}

    process_upvote(voter, True, request_json, False)

    row = ActivityPubLog.query.one()
    assert row.result == 'failure'
    assert row.exception_message == f'Unfound object {ap_id}'
    assert PostVote.query.count() == 0


def test_upvote_success_votes_logs_and_announces_only_when_not_announced(app, db_session, monkeypatch):
    """routes.py:2405-2408 -- the happy path: liked.vote() runs for real
    (a genuine PostVote row, not a mocked call), APLOG_LIKE/APLOG_SUCCESS is
    logged, and announce_activity_to_followers is called -- monkeypatched
    here as a recorder so its own arguments can be asserted -- specifically
    BECAUSE `announced` is False (the `if not announced:` guard at :2408).
    Its args are `(liked.community, user, request_json)` with
    `can_batch=True`.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    voter, post = _seed_vote_scenario()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers',
                         lambda *args, **kwargs: calls.append((args, kwargs)))

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id}

    process_upvote(voter, True, request_json, False)

    vote = PostVote.query.filter_by(user_id=voter.id, post_id=post.id).one()
    assert vote.effect == 1
    assert ActivityPubLog.query.one().result == 'success'
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0].id == post.community_id
    assert args[1].id == voter.id
    assert args[2] is request_json
    assert kwargs == {'can_batch': True}


def test_upvote_announced_reads_the_nested_object_and_does_not_re_announce(app, db_session, monkeypatch):
    """routes.py:2390 -- for an announced activity, ap_id comes from
    `request_json['object']['object']` rather than the top-level `object`.
    The vote still succeeds through that nested id, but
    announce_activity_to_followers is never called, because `announced` is
    True and :2408's `if not announced:` guard is what gates that call --
    the boost is already being distributed by the Announce that wrapped this
    activity.
    """
    voter, post = _seed_vote_scenario()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers',
                         lambda *args, **kwargs: calls.append((args, kwargs)))

    request_json = {
        'id': 'https://peer.example/activities/1',
        'object': {'object': post.ap_id},
    }

    process_upvote(voter, True, request_json, True)

    vote = PostVote.query.filter_by(user_id=voter.id, post_id=post.id).one()
    assert vote.effect == 1
    assert len(calls) == 0


def test_upvote_emoji_read_from_top_level_content_when_not_announced(app, db_session, monkeypatch):
    """routes.py:2391-2392 -- for a non-announced activity, emoji comes from
    the top-level `content` key when present.
    """
    voter, post = _seed_vote_scenario()
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id,
                    'content': '\U0001F44D'}

    process_upvote(voter, True, request_json, False)

    vote = PostVote.query.filter_by(user_id=voter.id, post_id=post.id).one()
    assert vote.emoji == '\U0001F44D'


def test_upvote_emoji_read_from_nested_content_when_announced(app, db_session, monkeypatch):
    """routes.py:2393-2394 -- for an announced activity, emoji comes from
    `request_json['object']['content']` instead of the top level.
    """
    voter, post = _seed_vote_scenario()
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    request_json = {
        'id': 'https://peer.example/activities/1',
        'object': {'object': post.ap_id, 'content': '\U0001F389'},
    }

    process_upvote(voter, True, request_json, True)

    vote = PostVote.query.filter_by(user_id=voter.id, post_id=post.id).one()
    assert vote.emoji == '\U0001F389'


def test_upvote_unwraps_a_dict_object_with_an_id_key(app, db_session, monkeypatch):
    """routes.py:2397-2398 -- when the resolved `ap_id` is itself a dict
    carrying an 'id' key (Lemmy/kbin-shaped object), it is unwrapped to that
    inner string before find_liked_object is called. Proven by the vote
    succeeding at all: an un-unwrapped dict would never match a Post's
    string `ap_id` column and would fall into the unfound-object refusal
    instead.
    """
    voter, post = _seed_vote_scenario()
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    request_json = {'id': 'https://peer.example/activities/1', 'object': {'id': post.ap_id}}

    process_upvote(voter, True, request_json, False)

    vote = PostVote.query.filter_by(user_id=voter.id, post_id=post.id).one()
    assert vote.effect == 1


# --- Step 3: the upvote/downvote asymmetry, routes.py:2404-2410 vs :2413-2433 ---

def test_an_upvote_blocked_by_the_vote_quota_logs_nothing(app, db_session, monkeypatch):
    """process_upvote, routes.py:2404-2408 -- there is no `else` on the inner
    `if`, so this path is silent. The equivalent downvote (routes.py:2431)
    logs IGNORED. Asserted as `ActivityPubLog.query.count() == 0` WITH logging
    enabled, which is the assertion that would fail if a log call were ever
    added.

    Registered by Task 9 as an asymmetry, not fixed here.

    VOTE_QUOTA is dropped to -1 (rather than writing a Redis
    `votes_cast_{today}_{user_id}` key) so that votes_cast_today's default
    (0, no key set) already exceeds it -- `0 <= -1` is False, which is all
    the inner conjunction's third clause needs to fail. This is also this
    guard's MUTATION killer for Step 4: dropping
    `votes_cast_today(user.id) <= current_app.config['VOTE_QUOTA'] and`
    from the inner conjunction would let this vote through, which this
    test's `PostVote.query.count() == 0` assertion would catch.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'VOTE_QUOTA', -1)
    voter, post = _seed_vote_scenario()

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id}

    process_upvote(voter, True, request_json, False)

    assert PostVote.query.count() == 0
    assert ActivityPubLog.query.count() == 0


def test_a_downvote_blocked_by_the_vote_quota_logs_ignored(app, db_session, monkeypatch):
    """routes.py:2431, the same input through the other delegate."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'VOTE_QUOTA', -1)
    voter, post = _seed_vote_scenario()

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id}

    process_downvote(voter, True, request_json, False)

    assert PostVote.query.count() == 0
    row = ActivityPubLog.query.one()
    assert row.result == 'ignored'
    assert row.exception_message == 'Cannot downvote this'


# --- Step 4: dropping each half of the two vote guards, one mutation at a time ---
#
# outer: can_upvote(user, liked.community) and not instance_banned(user.instance.domain)
# inner: isinstance(liked, (Post, PostReply)) and user.id not in blocked_users(liked.author.id)
#        and votes_cast_today(user.id) <= current_app.config['VOTE_QUOTA']
#
# Five halves total. Four get a distinct killer test below (a bot voter, a
# banned instance, an author's block of the voter, and -- reusing Step 3's
# own test above, since it is the exact same guard -- an over-quota voter).
# The fifth, `isinstance(liked, (Post, PostReply))`, is an EQUIVALENT MUTANT:
# see the module docstring for why, and task-5-report.md for the mutation
# run that confirmed it empirically.

def test_upvote_blocked_by_a_bot_voter_via_can_upvote(app, db_session, monkeypatch):
    """process_upvote's outer guard -- `can_upvote(user, liked.community)`
    (app/utils.py:2465) returns False for a bot account, so the vote is
    refused and 'Cannot upvote this' / APLOG_IGNORED is logged (the OUTER
    `else`, routes.py:2409-2410 -- unlike the inner `if`'s missing `else`
    pinned in Step 3, this refusal DOES log).

    MUTATION killer: dropping the `can_upvote(...) and` half of the outer
    conjunction (leaving only `not instance_banned(...)`) makes this vote
    succeed under the mutation, which this test's
    `PostVote.query.count() == 0` assertion catches. Distinct domain from
    the banned-instance test below: this row is the VOTER (bot=True); that
    one is the voter's INSTANCE (a BannedInstances row).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    voter, post = _seed_vote_scenario()
    voter.bot = True
    db.session.commit()

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id}

    process_upvote(voter, True, request_json, False)

    assert PostVote.query.count() == 0
    row = ActivityPubLog.query.one()
    assert row.result == 'ignored'
    assert row.exception_message == 'Cannot upvote this'


def test_upvote_blocked_by_a_banned_instance(app, db_session, monkeypatch):
    """process_upvote's outer guard -- `not instance_banned(user.instance.domain)`
    (app/utils.py:2320) is False once the voter's instance has a
    BannedInstances row, so the vote is refused the same way as the bot-voter
    test above.

    MUTATION killer: dropping the `not instance_banned(...) and` half of the
    outer conjunction (leaving only `can_upvote(...)`) makes this vote
    succeed under the mutation, caught by the same
    `PostVote.query.count() == 0` assertion.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    voter, post = _seed_vote_scenario()
    db.session.add(BannedInstances(domain=voter.instance.domain))
    db.session.commit()

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id}

    process_upvote(voter, True, request_json, False)

    assert PostVote.query.count() == 0
    row = ActivityPubLog.query.one()
    assert row.result == 'ignored'
    assert row.exception_message == 'Cannot upvote this'


def test_upvote_blocked_by_the_authors_block_of_the_voter(app, db_session, monkeypatch):
    """process_upvote's inner conjunction -- `user.id not in
    blocked_users(liked.author.id)` (app/utils.py:1728) is False once the
    post's author has blocked the voter, so the vote is silently refused: no
    else on this inner `if` (see the Step 3 asymmetry tests above), hence no
    log assertion here -- only that no vote was cast.

    MUTATION killer: dropping the `user.id not in blocked_users(...) and`
    half of the inner conjunction (leaving isinstance(...) and the quota
    check) makes this vote succeed under the mutation, caught by
    `PostVote.query.count() == 0`. Distinct domain from the over-quota test
    (Step 3 above): this row is a UserBlock from the post's AUTHOR; that one
    needs no row at all, only a lowered VOTE_QUOTA.
    """
    voter, post = _seed_vote_scenario()
    make_user_block(post.author, voter)

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id}

    process_upvote(voter, True, request_json, False)

    assert PostVote.query.count() == 0


# --- Task 6: process_poll_vote, routes.py:2436-2460 ---

def _seed_poll_scenario(host='peer.example'):
    """A local-owned community (same id=1 / instance_id=1 seeding trick as
    _seed_vote_scenario above), an author, a voter, and one Post with a Poll
    carrying a single PollChoice ('yes'). Returns (voter, post, choice).
    """
    make_site()
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    community = make_community(host=host)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    voter = make_user(instance, 'voter')
    post = make_post(community, author, ap_id=f'https://{host}/objects/1')
    poll = Poll(post_id=post.id, mode='single', local_only=False)
    db.session.add(poll)
    choice = PollChoice(post_id=post.id, choice_text='yes', sort_order=0)
    db.session.add(choice)
    db.session.commit()
    return voter, post, choice


def test_poll_vote_of_an_unfound_post_logs_failure(app, db_session, monkeypatch):
    """routes.py:2445-2447 -- Post.get_by_ap_id misses, logged as
    APLOG_RATE/APLOG_FAILURE with the ap_id folded into the message, and the
    function returns before instance_banned or the choice lookup ever runs.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _voter, _post, _choice = _seed_poll_scenario()

    ap_id = 'https://peer.example/objects/does-not-exist'
    request_json = {'id': 'https://peer.example/activities/1', 'object': ap_id, 'choice_text': 'yes'}

    process_poll_vote(_voter, True, request_json, False)

    row = ActivityPubLog.query.one()
    assert row.result == 'failure'
    assert row.exception_message == f'Unfound object {ap_id}'
    assert PollChoiceVote.query.count() == 0


def test_poll_vote_success_votes_logs_and_announces_only_when_not_announced(app, db_session, monkeypatch):
    """routes.py:2453-2456 -- the happy path: poll.vote_for_choice() runs for
    real (a genuine PollChoiceVote row and an incremented PollChoice.num_votes,
    not a mocked call), APLOG_RATE/APLOG_SUCCESS is logged, and
    announce_activity_to_followers is called -- monkeypatched here as a
    recorder -- BECAUSE `announced` is False. Its args are `(post.community,
    user, request_json)` with NO `can_batch` kwarg, unlike the vote
    delegates' `can_batch=True` call above.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    voter, post, choice = _seed_poll_scenario()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers',
                         lambda *args, **kwargs: calls.append((args, kwargs)))

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id, 'choice_text': 'yes'}

    process_poll_vote(voter, True, request_json, False)

    vote = PollChoiceVote.query.filter_by(user_id=voter.id, choice_id=choice.id).one()
    assert vote.post_id == post.id
    assert PollChoice.query.get(choice.id).num_votes == 1
    assert ActivityPubLog.query.one().result == 'success'
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0].id == post.community_id
    assert args[1].id == voter.id
    assert args[2] is request_json
    assert kwargs == {}


def test_poll_vote_announced_reads_the_nested_object_and_choice_text(app, db_session, monkeypatch):
    """routes.py:2439-2440 -- for an announced activity, BOTH `ap_id` and
    `choice_text` come from inside `request_json['object']`, not the
    top-level keys used when not announced. The vote still succeeds through
    that nested pair, but announce_activity_to_followers is never called,
    because `announced` is True and :2456's `if not announced:` guard is what
    gates that call.
    """
    voter, post, choice = _seed_poll_scenario()

    calls = []
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers',
                         lambda *args, **kwargs: calls.append((args, kwargs)))

    request_json = {
        'id': 'https://peer.example/activities/1',
        'object': {'object': post.ap_id, 'choice_text': 'yes'},
    }

    process_poll_vote(voter, True, request_json, True)

    vote = PollChoiceVote.query.filter_by(user_id=voter.id, choice_id=choice.id).one()
    assert vote.post_id == post.id
    assert len(calls) == 0


def test_poll_vote_unwraps_a_dict_object_with_an_id_key(app, db_session, monkeypatch):
    """routes.py:2441-2442 -- when the resolved `ap_id` is itself a dict
    carrying an 'id' key, it is unwrapped to that inner string before
    Post.get_by_ap_id is called. Proven by the vote succeeding at all: an
    un-unwrapped dict would never match a Post's string `ap_id` column and
    would fall into the unfound-object refusal instead.
    """
    voter, post, choice = _seed_poll_scenario()
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    request_json = {'id': 'https://peer.example/activities/1', 'object': {'id': post.ap_id},
                    'choice_text': 'yes'}

    process_poll_vote(voter, True, request_json, False)

    vote = PollChoiceVote.query.filter_by(user_id=voter.id, choice_id=choice.id).one()
    assert vote.post_id == post.id


def test_poll_vote_of_an_unfound_choice_logs_failure(app, db_session, monkeypatch):
    """routes.py:2458 -- the post is found but no PollChoice matches
    `choice_text`, logged as APLOG_RATE/APLOG_FAILURE with the choice text
    folded into the message. No PollChoiceVote is created.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    voter, post, _choice = _seed_poll_scenario()

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id,
                    'choice_text': 'does-not-exist'}

    process_poll_vote(voter, True, request_json, False)

    row = ActivityPubLog.query.one()
    assert row.result == 'failure'
    assert row.exception_message == 'Unfound poll choice does-not-exist'
    assert PollChoiceVote.query.count() == 0


def test_poll_vote_blocked_by_a_banned_instance(app, db_session, monkeypatch):
    """routes.py:2460 -- `instance_banned(user.instance.domain)` is True once
    the voter's instance has a BannedInstances row, so the vote is refused
    with APLOG_RATE/APLOG_IGNORED / 'Cannot rate this', even though the post
    and choice both exist.

    MUTATION killer: dropping the `not instance_banned(...)` guard entirely
    (always taking the `if` branch) would let this vote through, which this
    test's `PollChoiceVote.query.count() == 0` assertion catches. Run against
    a real `git diff --stat app/`-restored mutation: killed by this test's
    own assertions (not an AllMockedAssertionError -- no network fetch is on
    this path at all, since the community is local and voter/author/post are
    all seeded rows).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    voter, post, _choice = _seed_poll_scenario()
    db.session.add(BannedInstances(domain=voter.instance.domain))
    db.session.commit()

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id, 'choice_text': 'yes'}

    process_poll_vote(voter, True, request_json, False)

    assert PollChoiceVote.query.count() == 0
    row = ActivityPubLog.query.one()
    assert row.result == 'ignored'
    assert row.exception_message == 'Cannot rate this'


def test_a_poll_vote_without_choice_text(app, db_session, monkeypatch):
    """routes.py:2440 -- `request_json['choice_text']` is read unguarded from
    a peer-supplied activity. Establish and assert the observed behaviour.

    OBSERVED: for a non-announced activity missing the `choice_text` key
    entirely, this line raises an unhandled `KeyError` straight out of
    process_poll_vote -- BEFORE `Post.get_by_ap_id` is even called, since the
    `choice_text` read sits directly after the `ap_id` read and above the
    post lookup. A real peer omitting this field (or a client library that
    treats it as optional) crashes activity processing rather than being
    refused gracefully. Registered as a finding for Task 9; not fixed here.
    """
    voter, post, _choice = _seed_poll_scenario()

    request_json = {'id': 'https://peer.example/activities/1', 'object': post.ap_id}

    with pytest.raises(KeyError, match='choice_text'):
        process_poll_vote(voter, True, request_json, False)


# --- Task 6: process_question_answer, routes.py:2463-2496 ---

def _seed_qa_scenario(host='peer.example', same_author=False, local_reply_author=True):
    """A local-owned community, a post, and a PostReply on it. Returns
    (acting_user, reply).

    By default (same_author=False, local_reply_author=True) the reply's
    author and the post's author are two DIFFERENT people, the acting user is
    a third person with no moderator/admin standing, and the reply's author
    is local -- so all three alternatives of process_question_answer's
    permission guard are False and the Notification/unread_notifications
    side effect (:2478) is reachable whenever a test flips the guard True.

    same_author=True makes post_reply.user_id == post_reply.post.user_id
    (the guard's first alternative) by having one user author both the post
    and the reply on it.

    local_reply_author controls whether the reply's author is local
    (ap_id=None) or remote, independently of the permission guard -- used by
    the local-author-only Notification guard test.
    """
    make_site()
    instance = make_instance(host)
    make_user(instance, 'community_owner')
    community = make_community(host=host)
    community.ap_fetched_at = utcnow()
    post_author = make_user(instance, 'post_author', local=(local_reply_author if same_author else False))
    reply_author = post_author if same_author else make_user(instance, 'reply_author', local=local_reply_author)
    acting_user = make_user(instance, 'acting_user')
    post = make_post(community, post_author, ap_id=f'https://{host}/objects/1')
    reply = make_post_reply(post, reply_author)
    reply.ap_id = f'https://{host}/objects/1/comment/1'
    db.session.commit()
    return acting_user, reply


def test_question_answer_of_an_unfound_reply_logs_failure(app, db_session, monkeypatch):
    """routes.py:2470-2472 -- PostReply.get_by_ap_id misses, logged as
    APLOG_QA/APLOG_FAILURE with the ap_id folded into the message, and the
    function returns before the permission guard or the redis lock ever run.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user, _reply = _seed_qa_scenario()

    ap_id = 'https://peer.example/objects/does-not-exist'
    request_json = {'id': 'https://peer.example/activities/1', 'object': ap_id}

    process_question_answer(user, True, request_json, False)

    row = ActivityPubLog.query.one()
    assert row.result == 'failure'
    assert row.exception_message == f'Unfound object {ap_id}'
    assert PostReply.query.one().answer is False


def test_question_answer_success_sets_answer_notifies_and_announces(app, db_session, monkeypatch, redis_lock_only_double):
    """routes.py:2477-2492 -- the happy path, via the first permission
    alternative (post_reply.user_id == post_reply.post.user_id, using
    same_author=True). `post_reply.answer` is set True, a Notification row
    is created (title 'Answer was chosen', addressed to the reply's author,
    authored by the acting user, notif_type NOTIF_ANSWER), the reply
    author's unread_notifications is incremented, APLOG_QA/APLOG_SUCCESS is
    logged, and -- because `announced` is False -- announce_activity_to_followers
    is called with `(post_reply.community, user, request_json)` and no
    `can_batch` kwarg.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user, reply = _seed_qa_scenario(same_author=True)

    calls = []
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers',
                         lambda *args, **kwargs: calls.append((args, kwargs)))

    request_json = {'id': 'https://peer.example/activities/1', 'object': reply.ap_id}

    process_question_answer(user, True, request_json, False)

    assert PostReply.query.get(reply.id).answer is True
    notif = Notification.query.one()
    assert notif.title == 'Answer was chosen'
    assert notif.user_id == reply.user_id
    assert notif.author_id == user.id
    assert notif.notif_type == NOTIF_ANSWER
    assert notif.subtype == 'answer_chosen'
    assert reply.author.unread_notifications == 1
    assert ActivityPubLog.query.one().result == 'success'
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0].id == reply.community_id
    assert args[1].id == user.id
    assert args[2] is request_json
    assert kwargs == {}


def test_question_answer_skips_notification_for_a_remote_reply_author(app, db_session, monkeypatch, redis_lock_only_double):
    """routes.py:2478 -- the Notification/unread_notifications side effect is
    gated on `post_reply.author.is_local()`. With a remote reply author (and
    the same first-alternative permission grant as the success test above),
    `answer` is still set True and the success path still logs and announces,
    but no Notification row is ever created and unread_notifications never
    moves off its default.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user, reply = _seed_qa_scenario(same_author=True, local_reply_author=False)
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    request_json = {'id': 'https://peer.example/activities/1', 'object': reply.ap_id}

    process_question_answer(user, True, request_json, False)

    assert PostReply.query.get(reply.id).answer is True
    assert Notification.query.count() == 0
    assert reply.author.unread_notifications == 0
    assert ActivityPubLog.query.one().result == 'success'


def test_question_answer_refused_by_default_logs_cannot_set_answer(app, db_session, monkeypatch):
    """routes.py:2496 -- with none of the three permission alternatives
    satisfied (default _seed_qa_scenario: different post/reply authors, a
    non-moderator/non-admin acting user), the guard is False and the refusal
    logs APLOG_QA/APLOG_IGNORED / 'Cannot set answer'. `answer` is never set.
    No redis_double needed: the guard's False branch never reaches the lock.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user, reply = _seed_qa_scenario()

    request_json = {'id': 'https://peer.example/activities/1', 'object': reply.ap_id}

    process_question_answer(user, True, request_json, False)

    assert PostReply.query.get(reply.id).answer is False
    row = ActivityPubLog.query.one()
    assert row.result == 'ignored'
    assert row.exception_message == 'Cannot set answer'


# --- Step 4: dropping each piece of process_question_answer's permission guard ---
#
#   (not instance_banned(user.instance.domain)) and (
#       post_reply.user_id == post_reply.post.user_id
#       or post_reply.community.is_moderator(user)
#       or post_reply.author.is_instance_admin()
#   )
#
# Four pieces, four distinct killers: the instance_banned conjunct, and each
# of the three OR-alternatives in isolation. Each test below seeds ONLY the
# single condition it targets true, leaving the other two alternatives (and,
# for the three alternative tests, the banned check) false -- so a single
# test satisfying two conditions at once, which would leave one alternative
# permanently untested, is deliberately avoided.

def test_question_answer_granted_by_alternative_one_same_author(app, db_session, monkeypatch, redis_lock_only_double):
    """Alternative 1 -- `post_reply.user_id == post_reply.post.user_id` --
    granted alone (acting user is neither a moderator nor is the reply
    author an admin). This is `test_question_answer_success_sets_answer_notifies_and_announces`
    above in miniature, kept separate because THAT test also exercises the
    Notification side effect; this one is the dedicated Step 4 killer.

    MUTATION killer: dropping `post_reply.user_id == post_reply.post.user_id
    or` from the guard leaves only the two other (false) alternatives, so the
    guard flips to False and the refusal fires instead -- caught by this
    test's `PostReply.query.get(reply.id).answer is True` assertion (it
    would be False under the mutation).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user, reply = _seed_qa_scenario(same_author=True)
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    request_json = {'id': 'https://peer.example/activities/1', 'object': reply.ap_id}

    process_question_answer(user, True, request_json, False)

    assert PostReply.query.get(reply.id).answer is True
    assert ActivityPubLog.query.one().result == 'success'


def test_question_answer_granted_by_alternative_two_moderator(app, db_session, monkeypatch, redis_lock_only_double):
    """Alternative 2 -- `post_reply.community.is_moderator(user)` -- granted
    alone: the ACTING user (not the reply's author, not the post's author)
    is a moderator of the reply's community. Default scenario keeps
    alternative 1 false (different post/reply authors) and alternative 3
    false (reply author is not an admin).

    MUTATION killer: dropping `post_reply.community.is_moderator(user) or`
    from the guard leaves only the two other (false) alternatives, flipping
    the guard False and the refusal fires instead of the grant -- caught by
    the same `answer is True` assertion.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user, reply = _seed_qa_scenario()
    make_community_member(user, reply.community, is_moderator=True)
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    request_json = {'id': 'https://peer.example/activities/1', 'object': reply.ap_id}

    process_question_answer(user, True, request_json, False)

    assert PostReply.query.get(reply.id).answer is True
    assert ActivityPubLog.query.one().result == 'success'


def test_question_answer_granted_by_alternative_three_reply_author_is_admin(app, db_session, monkeypatch,
                                                                            redis_lock_only_double):
    """Alternative 3 -- `post_reply.author.is_instance_admin()` -- granted
    alone: the REPLY'S author (not the acting user) holds an admin
    InstanceRole on their own instance. Default scenario keeps alternative 1
    false (different post/reply authors) and alternative 2 false (acting
    user is not a moderator).

    MUTATION killer: dropping `post_reply.author.is_instance_admin()` from
    the guard (the trailing alternative, no `or` after it) leaves only the
    two other (false) alternatives, flipping the guard False and the refusal
    fires instead of the grant -- caught by the same `answer is True`
    assertion.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user, reply = _seed_qa_scenario()
    db.session.add(InstanceRole(instance_id=reply.author.instance_id, user_id=reply.author.id, role='admin'))
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *a, **k: None)

    request_json = {'id': 'https://peer.example/activities/1', 'object': reply.ap_id}

    process_question_answer(user, True, request_json, False)

    assert PostReply.query.get(reply.id).answer is True
    assert ActivityPubLog.query.one().result == 'success'


def test_question_answer_refused_for_a_banned_instance_despite_alternative_one(app, db_session, monkeypatch):
    """The `instance_banned` conjunct -- seeded with alternative 1 ALSO true
    (same_author=True) so that only the conjunct's own presence explains the
    refusal: with instance_banned False this exact row would be granted (see
    the alternative-one test above), so a banned instance overriding it to a
    refusal proves the conjunct is doing real work.

    MUTATION killer: dropping the `(not instance_banned(...)) and` conjunct
    (leaving only the OR of the three alternatives) would let this row
    through as a grant, since alternative 1 is true -- caught by this test's
    `answer is False` / 'ignored' assertions, which would fail under that
    mutation. No redis_double needed: the guard is False, so the lock is
    never reached.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    user, reply = _seed_qa_scenario(same_author=True)
    db.session.add(BannedInstances(domain=user.instance.domain))
    db.session.commit()

    request_json = {'id': 'https://peer.example/activities/1', 'object': reply.ap_id}

    process_question_answer(user, True, request_json, False)

    assert PostReply.query.get(reply.id).answer is False
    row = ActivityPubLog.query.one()
    assert row.result == 'ignored'
    assert row.exception_message == 'Cannot set answer'
