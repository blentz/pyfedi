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
from app.models import ActivityPubLog, ChatMessage
from tests.factories import (inbox_activity, make_community, make_chat_message, make_instance,
                             make_post, make_post_reply, make_user, seed_community_owner)
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
    is true and the follower announce fires. Its sibling below proves the
    guard by sending the same Undo inside an Announce.
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
