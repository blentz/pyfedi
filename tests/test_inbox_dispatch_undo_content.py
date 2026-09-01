"""Sub-project 5d, Task 4 -- Undo/Delete: the restore path.

`app/activitypub/routes.py:1723-1749`, inside `if core_activity['type'] ==
'Undo':`, under `if core_activity['object']['type'] == 'Delete':`. Restores a
previously-deleted local Post or PostReply that `find_liked_object` resolves
by ActivityPub id, or -- when nothing is found -- restores a PM instead
(Task 5's PM branch, not covered here).

Three outcomes on the content-found side:
  - found, already restored (`not to_restore.deleted` is False) -> IGNORED,
    nothing called.
  - found, still deleted -> `restore_post_or_comment(restorer, to_restore,
    store_ap_json, request_json, reason)`, then `announce_activity_to_followers`
    unless the activity arrived wrapped in an Announce.
  - not found at all -> falls to the PM branch (Task 5), not this file.

`find_liked_object` is deliberately left un-doubled: it resolves the seeded
Post from the database by `ap_id`, which is what makes these tests exercise
the real lookup rather than a stand-in for it.
"""
import contextlib

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, ChatMessage, utcnow
from tests.factories import (inbox_activity, make_chat_message, make_community,
                             make_instance, make_post, make_post_reply, make_user,
                             seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def install_lock_only_redis(monkeypatch):
    """Replace `app.redis_client` with a double whose only capability is
    `.lock(...)` as a no-op context manager.

    Undo/ChooseAnswer (routes.py:1876) wraps its write in `with
    redis_client.lock(...)`, resolved via `from app import redis_client`
    INSIDE process_inbox_request's body (routes.py:846) -- so patching the
    single `app.redis_client` attribute is what takes effect, per
    tests/conftest.py's redis_double docstring (~394-450). fakeredis in this
    environment has no Lua scripting, which redis-py's real `Lock.release()`
    needs (EVALSHA on `__exit__`), so `redis_double` itself cannot serve a
    lock -- confirmed by tests/test_inbox_dispatch_votes.py's
    `_RedisLockOnlyDouble` / `redis_lock_only_double`, whose shape this
    copies. Do NOT change the redis_double fixture; the limitation is in
    fakeredis, not in what it patches.
    """

    class LockOnlyRedis:
        def lock(self, *args, **kwargs):
            return contextlib.nullcontext()

    monkeypatch.setattr('app.redis_client', LockOnlyRedis())


def undo_activity(actor, inner_type, inner_object, **inner):
    """An Undo wrapping an inner activity of `inner_type` about `inner_object`.

    `inbox_activity` applies **fields last, so `object=` replaces its default
    string object -- required, because the arm dispatches on
    `core_activity['object']['type']`.
    """
    obj = {'type': inner_type, 'object': inner_object}
    obj.update(inner)
    return inbox_activity(actor, activity_type='Undo', object=obj)


def test_undo_delete_restores_a_deleted_post_and_announces_it(app, db_session, monkeypatch):
    """The main restore path. `restore_post_or_comment` and
    `announce_activity_to_followers` are doubled at their binding site on the
    routes module -- routes imports the first by name and defines the second
    itself, and both patch the same way.

    The activity is NOT announced (no Announce wrapper), so `if not announced:`
    is true and the follower announce fires.
    `test_undo_delete_inside_an_announce_does_not_announce_again` below proves
    the other side of the same guard, by sending the same Undo inside an
    Announce.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    post.deleted = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'restore_post_or_comment',
                              'announce_activity_to_followers')

    dispatch(undo_activity(author, 'Delete', post.ap_id))

    assert len(calls['restore_post_or_comment']) == 1
    args, kwargs = calls['restore_post_or_comment'][0]
    restorer_arg, to_restore_arg = args[0], args[1]
    assert restorer_arg.id == author.id
    assert to_restore_arg.id == post.id
    assert len(calls['announce_activity_to_followers']) == 1


def test_undo_delete_inside_an_announce_does_not_announce_again(app, db_session, monkeypatch):
    """The FALSE side of `if not announced:` -- the community that delivered
    the Announce has already fanned it out to its own followers, so
    `announce_activity_to_followers` must not fire a second time.
    `restore_post_or_comment` still runs; only the announce is suppressed.

    Unlike every other test in this file, the OUTER actor here must be a
    Community, not a User: the preamble resolves an Announce's outer actor
    via a community_only lookup (routes.py:862,
    `find_actor_or_create_cached(actor_id, community_only=True,
    create_if_not_found=False)`) before the Undo/Delete arm ever runs, and
    only then walks to the INNER object's own 'actor' (routes.py:915,
    `find_actor_or_create_cached(request_json['object']['actor'])`) to find
    `user` -- confirmed by reading process_inbox_request's preamble rather
    than assumed. `community.ap_fetched_at` is stamped for the same reason
    tests/test_inbox_dispatch_announce.py's `_seed_announcing_community`
    stamps it: `make_community` leaves it unset, and an unset
    `ap_fetched_at` on a non-local actor makes `schedule_actor_refresh`
    (app/activitypub/actor.py) fire a real, unmocked
    `refresh_community_profile` fetch.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    community.ap_fetched_at = utcnow()
    post = make_post(community, author, 'https://peer.example/post/1')
    post.deleted = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'restore_post_or_comment',
                              'announce_activity_to_followers')

    inner_undo = undo_activity(author, 'Delete', post.ap_id)
    activity = inbox_activity(community, activity_type='Announce', object=inner_undo)

    dispatch(activity)

    assert len(calls['restore_post_or_comment']) == 1
    args, kwargs = calls['restore_post_or_comment'][0]
    restorer_arg, to_restore_arg = args[0], args[1]
    assert restorer_arg.id == author.id
    assert to_restore_arg.id == post.id
    assert calls['announce_activity_to_followers'] == []


def test_undo_delete_of_content_that_is_not_deleted_is_ignored(app, db_session, monkeypatch):
    """`if not to_restore.deleted:` -- `deleted` is seeded explicitly False
    rather than left at its column default, so the IGNORED outcome is evidence
    about the guard rather than about the default.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    post.deleted = False
    db.session.commit()

    calls = record_moderation(monkeypatch, 'restore_post_or_comment',
                              'announce_activity_to_followers')

    dispatch(undo_activity(author, 'Delete', post.ap_id))

    assert calls['restore_post_or_comment'] == []
    assert calls['announce_activity_to_followers'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert log.exception_message == 'Activity about local content which is already restored'


def test_undo_delete_passes_the_summary_as_the_reason(app, db_session, monkeypatch):
    """`reason` comes from the INNER object's 'summary', not the outer
    activity's -- asserted here by setting only the inner one.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    post.deleted = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'restore_post_or_comment',
                              'announce_activity_to_followers')

    dispatch(undo_activity(author, 'Delete', post.ap_id, summary='a good reason'))

    args, kwargs = calls['restore_post_or_comment'][0]
    assert args[4] == 'a good reason'


def test_undo_delete_without_a_summary_passes_an_empty_reason(app, db_session, monkeypatch):
    """The `else ''` half of the same conditional. Paired with the test above
    so neither half can be dropped without a failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    post.deleted = True
    db.session.commit()

    calls = record_moderation(monkeypatch, 'restore_post_or_comment',
                              'announce_activity_to_followers')

    dispatch(undo_activity(author, 'Delete', post.ap_id))

    args, kwargs = calls['restore_post_or_comment'][0]
    assert args[4] == ''


def test_undo_delete_restores_a_deleted_private_message(app, db_session, monkeypatch):
    """The `else:` branch reached when `find_liked_object` finds no post or
    comment for the ap_id at all -- it falls through to check ChatMessage.
    `deleted` is seeded True so flipping it to False is a real observation,
    not proof of the column's declared default.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    recipient = make_user(None, 'recipient', local=True)
    message = make_chat_message(sender, recipient, 'https://peer.example/pm/1', deleted=True)
    message_id = message.id

    dispatch(undo_activity(sender, 'Delete', 'https://peer.example/pm/1'))

    db.session.expire_all()
    assert db.session.get(ChatMessage, message_id).deleted is False
    log = ActivityPubLog.query.one()
    assert log.result == 'success'
    assert 'https://peer.example/pm/1' in log.exception_message


def test_undo_delete_does_not_restore_a_private_message_sent_by_someone_else(app, db_session, monkeypatch):
    """`session.query(ChatMessage).filter_by(ap_id=ap_id, sender_id=restorer.id)`
    -- `sender_id` is the half that matters here. An interloper undoing a
    Delete for the same ap_id must not find (and so must not restore) a
    message somebody else sent: the message stays deleted, and because
    `updated_message` is falsy the code never reaches its `log_incoming_ap`
    call at all, so nothing is logged on this path -- not even a failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    interloper = make_user(instance, 'interloper')
    recipient = make_user(None, 'recipient', local=True)
    message = make_chat_message(sender, recipient, 'https://peer.example/pm/1', deleted=True)
    message_id = message.id

    dispatch(undo_activity(interloper, 'Delete', 'https://peer.example/pm/1'))

    db.session.expire_all()
    assert db.session.get(ChatMessage, message_id).deleted is True  # untouched
    assert ActivityPubLog.query.count() == 0


def test_undo_delete_reads_the_kbin_dict_object_shape(app, db_session, monkeypatch):
    """`ap_id` comes from either a bare string (lemmy) or a dict's 'id' key
    (kbin) -- `isinstance(core_activity['object']['object'], str)` picks
    which. Every other test in this file uses the lemmy string shape via
    `post.ap_id`; this one sends a kbin-shaped dict instead, so the `else`
    branch (`ap_id = core_activity['object']['object']['id']`) is exercised
    too.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    recipient = make_user(None, 'recipient', local=True)
    message = make_chat_message(sender, recipient, 'https://peer.example/pm/1', deleted=True)
    message_id = message.id

    dispatch(undo_activity(sender, 'Delete', {'id': 'https://peer.example/pm/1',
                                              'type': 'Note'}))

    db.session.expire_all()
    assert db.session.get(ChatMessage, message_id).deleted is False


def test_undo_like_calls_undo_vote_with_both_objects_none_and_announces_batchable(app, db_session, monkeypatch):
    """`post = comment = None` immediately before the call, so the delegate
    receives two Nones and resolves the target itself from the ap_id. The
    announce is made with can_batch=True, which is asserted rather than merely
    counted because it is a federation-behaviour choice, not an implementation
    detail.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    voter = make_user(instance, 'voter')
    community = make_community(host='peer.example')
    post = make_post(community, voter, 'https://peer.example/post/1')

    calls = record_moderation(monkeypatch, 'announce_activity_to_followers')
    monkeypatch.setattr(activitypub_routes, 'undo_vote',
                        lambda comment, post_, target_ap_id, user: post)

    dispatch(undo_activity(voter, 'Like', 'https://peer.example/post/1'))

    assert len(calls['announce_activity_to_followers']) == 1
    args, kwargs = calls['announce_activity_to_followers'][0]
    assert kwargs.get('can_batch') is True

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_dislike_takes_the_same_branch_as_undo_like(app, db_session, monkeypatch):
    """The guard is `== 'Like' or == 'Dislike'`. This test covers the second
    disjunct; the test above covers the first, so dropping either fails one.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    voter = make_user(instance, 'voter')
    community = make_community(host='peer.example')
    post = make_post(community, voter, 'https://peer.example/post/1')

    record_moderation(monkeypatch, 'announce_activity_to_followers')
    monkeypatch.setattr(activitypub_routes, 'undo_vote',
                        lambda comment, post_, target_ap_id, user: post)

    dispatch(undo_activity(voter, 'Dislike', 'https://peer.example/post/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_like_of_an_unfound_object_logs_failure_with_the_uri(app, db_session, monkeypatch):
    """`undo_vote` returning None takes the else. The target uri is
    concatenated into the message, so the assertion pins it.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    voter = make_user(instance, 'voter')

    calls = record_moderation(monkeypatch, 'announce_activity_to_followers')
    monkeypatch.setattr(activitypub_routes, 'undo_vote',
                        lambda comment, post_, target_ap_id, user: None)

    dispatch(undo_activity(voter, 'Like', 'https://peer.example/post/404'))

    assert calls['announce_activity_to_followers'] == []
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unfound object https://peer.example/post/404'


def test_undo_announce_undoes_a_boost_via_the_outer_actor(app, db_session, monkeypatch):
    """The arm's own comment insists the actor comes from the SIGNED outer
    activity, never the inner object. The inner object here carries a
    DIFFERENT actor, and the assertion is that `undo_boost` receives the outer
    one -- which is the whole point of that comment.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    booster = make_user(instance, 'booster')
    impostor = make_user(instance, 'impostor')
    community = make_community(host='peer.example')
    post = make_post(community, booster, 'https://peer.example/post/1')

    seen = {}

    def fake_undo_boost(target_ap_id, user):
        seen['target'] = target_ap_id
        seen['user_id'] = user.id
        return post

    monkeypatch.setattr(activitypub_routes, 'undo_boost', fake_undo_boost)
    monkeypatch.setattr(activitypub_routes, 'announce_target_uri',
                        lambda activity: 'https://peer.example/post/1')

    # `undo_activity`'s first positional parameter is itself named `actor`
    # (the OUTER actor), so passing `actor=impostor.ap_profile_id` as an
    # extra keyword collides with it (TypeError: multiple values for
    # 'actor') rather than landing in **inner as intended. Build the outer
    # activity normally, then stamp the inner object's 'actor' directly --
    # same resulting JSON shape, without the collision.
    activity = undo_activity(booster, 'Announce', 'https://peer.example/post/1')
    activity['object']['actor'] = impostor.ap_profile_id

    dispatch(activity)

    assert seen['user_id'] == booster.id      # outer actor, not the impostor
    assert seen['target'] == 'https://peer.example/post/1'
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_announce_of_an_unfound_post_is_ignored(app, db_session, monkeypatch):
    """`undo_boost` returning None takes the else, which logs IGNORED (not
    FAILURE, unlike the vote path a few lines above -- the two thin arms differ
    here and the pair of tests pins the difference).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    booster = make_user(instance, 'booster')

    monkeypatch.setattr(activitypub_routes, 'undo_boost', lambda target_ap_id, user: None)
    monkeypatch.setattr(activitypub_routes, 'announce_target_uri',
                        lambda activity: 'https://peer.example/post/404')

    dispatch(undo_activity(booster, 'Announce', 'https://peer.example/post/404'))

    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert 'https://peer.example/post/404' in log.exception_message


def test_undo_choose_answer_clears_the_answer_flag(app, db_session, monkeypatch):
    """`answer` is seeded True so clearing it to False is a real write, not
    proof of the column's declared default.

    The arm wraps the write in `with redis_client.lock(...)`. This suite's
    fakeredis instance cannot serve a redis-py lock (tests/conftest.py's
    redis_double docstring, ~394-450), so `app.redis_client` is replaced with
    `install_lock_only_redis`'s narrower double -- shaped after
    tests/test_inbox_dispatch_votes.py's `_RedisLockOnlyDouble`, whose
    `.lock(...)` is a genuine no-op context manager. Do NOT change the
    redis_double fixture -- it patches the right attribute; the limitation is
    in fakeredis.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    install_lock_only_redis(monkeypatch)
    instance = seed_community_owner('peer.example')
    author = make_user(instance, 'author')
    community = make_community(host='peer.example')
    post = make_post(community, author, 'https://peer.example/post/1')
    reply = make_post_reply(post, author)
    reply.ap_id = 'https://peer.example/comment/1'
    reply.answer = True
    db.session.commit()
    reply_id = reply.id

    dispatch(undo_activity(author, 'ChooseAnswer', 'https://peer.example/comment/1'))

    db.session.expire_all()
    assert db.session.get(type(reply), reply_id).answer is False
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_undo_choose_answer_for_an_unknown_reply_logs_nothing(app, db_session, monkeypatch):
    """`if post_reply:` is false, so the arm returns having logged nothing --
    asserted with LOG_ACTIVITYPUB_TO_DB explicitly True so the zero count is
    a real observation about the guard, not an artifact of logging being off.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    install_lock_only_redis(monkeypatch)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')

    dispatch(undo_activity(author, 'ChooseAnswer', 'https://peer.example/comment/404'))

    assert ActivityPubLog.query.count() == 0


def test_an_undo_of_an_unrecognised_type_falls_through_to_monitor(app, db_session, monkeypatch):
    """The eighth path: an inner type matching none of the arm's sub-types
    reaches its final `log_incoming_ap(..., APLOG_MONITOR, APLOG_PROCESSING,
    ..., 'Unmatched activity')`. 'Move' is chosen deliberately -- it is a
    real activity type this dispatcher handles at the TOP level (as a sibling
    of 'Undo' in process_inbox_request's own if/elif chain), so this test
    proves the Undo arm does not accidentally fall into the outer
    dispatcher's handling for it; instead it logs its own generic
    'Unmatched activity' message with a PROCESSING result.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    actor = make_user(instance, 'actor')

    dispatch(undo_activity(actor, 'Move', 'https://peer.example/u/someone'))

    log = ActivityPubLog.query.one()
    assert log.result == 'processing'
    assert log.exception_message == 'Unmatched activity'


def test_a_string_inner_object_cannot_reach_choose_answer_at_all(app, db_session, monkeypatch):
    """FIX 4's justification. The arm selects ChooseAnswer by reading
    `core_activity['object']['type']`, so a STRING inner object raises
    TypeError before any sub-type is chosen -- which is why the
    `isinstance(core_activity['object'], str)` branch inside ChooseAnswer was
    unreachable. Same equivalent-mutant class as D95 and D96.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')

    activity = inbox_activity(author, activity_type='Undo',
                              object='https://peer.example/comment/1')

    with pytest.raises(TypeError):
        dispatch(activity)
