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
"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import process_downvote, process_upvote
from app.models import ActivityPubLog, BannedInstances, PostVote, utcnow
from tests.factories import (inbox_activity, make_community, make_instance, make_post, make_site,
                             make_user, make_user_block)
from tests.test_inbox_dispatch_preamble import dispatch


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
