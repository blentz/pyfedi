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
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog, ChatMessage, utcnow
from tests.factories import (inbox_activity, make_chat_message, make_community,
                             make_instance, make_post, make_user, seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


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
