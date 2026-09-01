"""tests/test_inbox_dispatch_create_update.py"""
from app import db
from app.activitypub import routes as activitypub_routes
from app.models import ActivityPubLog
from tests.factories import (inbox_activity, make_community, make_instance, make_post,
                             make_user, seed_community_owner)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def create_activity(actor, obj, *, activity_type='Create', **outer):
    """A Create (or Update) whose `object` is `obj`.

    `inbox_activity` applies **fields last, so passing `object=` replaces its
    default string object. `obj` may be a dict OR a bare string -- the arm's
    first branch exists precisely to handle the string form.
    """
    return inbox_activity(actor, activity_type=activity_type, object=obj, **outer)


def test_an_unverifiable_string_object_logs_the_refusal_reason(app, db_session, monkeypatch):
    """`isinstance(core_activity['object'], str)` sends the activity to
    `verify_object_from_source`, which returns `(None, reason)` on refusal. The
    arm puts that reason into the log rather than a generic sentence, which is
    the whole point of the delegate returning it -- so the assertion pins the
    reason, not merely the failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'verify_object_from_source',
                        lambda activity: (None, 'host mismatch'))

    dispatch(create_activity(author, 'https://peer.example/objects/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Could not verify unsigned request from source: host mismatch'


def test_a_verified_string_object_continues_into_the_normal_path(app, db_session, monkeypatch):
    """On success `verify_object_from_source` returns the activity with its
    `object` replaced by the fetched document, and processing continues. The
    double returns a ChatMessage object so the continuation is observable via
    `process_chat` without also exercising the content path.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')

    def fake_verify(activity):
        activity['object'] = {'type': 'ChatMessage', 'id': 'https://peer.example/pm/1'}
        return activity, None

    monkeypatch.setattr(activitypub_routes, 'verify_object_from_source', fake_verify)
    calls = record_moderation(monkeypatch, 'process_chat')

    dispatch(create_activity(author, 'https://peer.example/objects/1'))

    assert len(calls['process_chat']) == 1


def test_a_chat_message_object_delegates_to_process_chat_and_returns(app, db_session, monkeypatch):
    """The `ChatMessage` branch delegates and returns immediately. `user` is the
    signed outer actor, and `session` is the dispatcher's own task session --
    both asserted positionally here, because the delegate's signature is
    `process_chat(user, store_ap_json, core_activity, session)` and a later
    reader cannot otherwise tell which argument is which.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('peer.example')
    author = make_user(instance, 'author')
    calls = record_moderation(monkeypatch, 'process_chat', 'process_new_content')

    activity = create_activity(author, {'type': 'ChatMessage', 'id': 'https://peer.example/pm/1'})
    dispatch(activity)

    assert len(calls['process_chat']) == 1
    args, kwargs = calls['process_chat'][0]
    from sqlalchemy import inspect as sa_inspect
    assert sa_inspect(args[0]).identity[0] == author.id
    assert args[2] is activity
    assert calls['process_new_content'] == []
