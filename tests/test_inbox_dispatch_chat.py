"""tests/test_inbox_dispatch_chat.py"""
from datetime import timedelta

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import (ActivityPubLog, ChatMessage, Conversation, Notification, Site,
                        User, utcnow)
from tests.factories import (inbox_activity, make_conversation, make_instance,
                             make_instance_block, make_site, make_user, make_user_block)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch


def test_a_brand_new_sender_is_refused(app, db_session, monkeypatch):
    """`created_very_recently()` is `created > utcnow() - timedelta(days=1)`,
    so `created` is seeded to NOW explicitly rather than left at whatever the
    factory set — the assertion must rest on a value this test chose.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow()
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Sender is too new'
    assert db_session.query(ChatMessage).count() == 0


def test_an_old_sender_is_not_refused_for_newness(app, db_session, monkeypatch):
    """The other side of the first conjunct: `created` two days ago. Paired with
    the test above so the conjunct dies in both directions.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_brand_new_sender_from_fediseer_is_exempt(app, db_session, monkeypatch):
    """The second conjunct: `user.ap_domain != 'fediseer.com'`. A brand-new
    sender is normally refused; this one is not, solely because of its domain.
    This is the ONLY test that distinguishes that conjunct.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(host='fediseer.com')
    sender.created = utcnow()
    sender.ap_domain = 'fediseer.com'
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_sender_blocked_by_the_recipient_is_refused(app, db_session, monkeypatch):
    """First disjunct of the block check: a UserBlock row."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    make_user_block(recipient, sender)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Sender blocked by recipient'
    assert db_session.query(ChatMessage).count() == 0


def test_a_sender_on_a_blocked_instance_is_refused(app, db_session, monkeypatch):
    """Second disjunct: an InstanceBlock row and NO UserBlock, so this test and
    the one above kill the two halves separately.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    make_instance_block(recipient, instance)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Sender blocked by recipient'
    assert db_session.query(ChatMessage).count() == 0


def chat_activity(sender, **objfields):
    """A Create whose inner object is a ChatMessage.

    `inbox_activity` applies **fields last, so `object=` replaces its default
    string object -- required, because process_chat is only reached once the
    arm has read `core_activity['object']['type']`.

    Callers pass `to=`, `content=`, `id=` etc. through **objfields, and may
    pass a field explicitly as absent by simply not supplying it -- which is
    how the missing-field tests are written.
    """
    obj = {'type': 'ChatMessage'}
    obj.update(objfields)
    return inbox_activity(sender, activity_type='Create', object=obj)


def seed_chat_pair(host='peer.example', accept=3, trusted=False):
    """A remote sender and a LOCAL recipient — the only shape that reaches the
    acceptance chain, since everything past `recipient.is_local()` requires it.

    `accept` is seeded onto the recipient EXPLICITLY even when it equals the
    column default of 3, because an assertion resting on a declared default
    proves nothing (see this plan's global constraints).

    `make_user(None, name, local=True)` leaves `ap_profile_id` NULL, unlike a
    real local account, and process_chat resolves the recipient by that value —
    so it is stamped here. Recipient resolution itself needed no adjustment:
    `find_actor_or_create_cached` found the seeded row by `ap_profile_id` as
    given, and `User.is_local()` reads `ap_id is None`, which `make_user(...,
    local=True)` already sets -- the docstring's premise held.

    What the brief's own draft did NOT account for: `make_user`'s `sender`
    gets `created = utcnow()` from the column default (app/models.py:987), and
    `process_chat`'s FIRST gate past `recipient.is_local()` is
    `sender.created_very_recently()` (`created > utcnow() - 1 day`), which
    fires True for a freshly-inserted row and returns True/'Sender is too new'
    before the recipient chain this task covers is ever reached. Backdating
    `sender.created` here is not a default-column assertion (nothing asserts
    on this value) -- it is disabling an unrelated guard so the acceptance
    chain under test is reachable at all.
    """
    from flask import current_app
    make_site()
    instance = make_instance(host)
    instance.trusted = trusted
    sender = make_user(instance, 'sender')
    sender.created = utcnow() - timedelta(days=2)
    recipient = make_user(None, 'recipient', local=True)
    recipient.ap_profile_id = f"{current_app.config['SERVER_URL']}/u/recipient".lower()
    recipient.accept_private_messages = accept
    db.session.commit()
    return instance, sender, recipient


@pytest.mark.parametrize('accept', [None, 0])
def test_a_recipient_with_pms_off_refuses(app, db_session, monkeypatch, accept):
    """`accept_private_messages is None or == 0`. Both values are seeded
    explicitly; neither is the column default of 3, so nothing here rests on a
    default. Parametrised because the guard is one condition with two accepted
    spellings of "off".
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(accept=accept)
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Recipient has turned off PMs'
    assert db_session.query(ChatMessage).count() == 0


def test_a_recipient_accepting_only_local_pms_refuses_a_remote_sender(app, db_session, monkeypatch):
    """`accept_private_messages == 1`. The sender is remote, which is the only
    case that reaches process_chat at all from a federated activity.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(accept=1)
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Recipient only accepts local PMs'


def test_a_trusted_instances_recipient_refuses_an_untrusted_sender(app, db_session, monkeypatch):
    """`accept_private_messages == 2 and not sender.instance.trusted` — the
    second conjunct is False here, so the refusal fires.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(accept=2, trusted=False)
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Sender from untrusted instance'


def test_a_trusted_instances_recipient_accepts_a_trusted_sender(app, db_session, monkeypatch):
    """The other side of that conjunct: `Instance.trusted` seeded True (its
    column default is False, so the True is this test's own choice). Paired with
    the test above so the conjunct dies in both directions.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(accept=2, trusted=True)
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_string_recipient_is_accepted_as_a_sole_jsonld_element(app, db_session, monkeypatch):
    """`object['to']` as a bare string. JSON-LD lets a single-element array be
    written as the value alone, which is why the function accepts both shapes.
    Proven by the message landing, not merely by the absence of a failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    assert db_session.query(ChatMessage).filter_by(ap_id='https://peer.example/pm/1').one()
    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_list_recipient_takes_its_first_element(app, db_session, monkeypatch):
    """`object['to']` as an array. The function takes element 0 and ignores the
    rest — the second entry here is a URL no user has, so the message landing
    for the FIRST proves which element was read.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=[recipient.ap_profile_id, 'https://peer.example/u/nobody'],
                           content='hello', id='https://peer.example/pm/1'))

    assert db_session.query(ChatMessage).filter_by(ap_id='https://peer.example/pm/1').one()


@pytest.mark.parametrize('objfields,description', [
    ({}, "no 'to' key at all"),
    ({'to': []}, "'to' is an empty list"),
])
def test_an_unresolvable_recipient_is_refused_and_reports_not_handled(
        app, db_session, monkeypatch, objfields, description):
    """Both shapes leave `recipient_ap_id` None. The RETURN VALUE matters as
    much as the log: False means "not handled, caller continue", and the
    Create/Update arm has a second call site (routes.py:1247) whose fallback
    path branches on it.

    Neither this test nor `dispatch()` can observe that return value directly:
    every activity built here has `object['type'] == 'ChatMessage'`, so the
    arm always takes its direct branch (routes.py:1215-1217), which discards
    `process_chat`'s return and unconditionally returns right after -- the
    1247 call site, and its fallback branching on True/False, is never
    reached by any test in this file. What IS asserted, for both of
    process_chat's two `return False` exits, is the one observable trace
    of "not handled" available from this call site: the failure log and the
    absence of any written ChatMessage row.

    The return value itself IS pinned, in both directions, by
    `test_a_handled_chat_stops_the_arm_from_treating_it_as_content` and
    `test_an_unhandled_chat_lets_the_arm_continue_to_the_domain_check` below --
    those build a `Page`-typed object so the arm reaches the 1247 call site
    instead of this one.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()

    dispatch(chat_activity(sender, content='hello', id='https://peer.example/pm/1', **objfields))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure', description
    assert log.exception_message == 'Chat recipient is invalid', description
    assert db_session.query(ChatMessage).count() == 0, description


def test_a_remote_recipient_is_refused_as_not_local(app, db_session, monkeypatch):
    """`recipient.is_local()` is False, so the whole acceptance chain is skipped
    and the function falls through to its final failure. This is the second of
    the two paths that report "not handled".
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    other_remote = make_user(instance, 'remoterecipient')
    db.session.commit()

    dispatch(chat_activity(sender, to=other_remote.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'ChatMessage target is not local'
    assert db_session.query(ChatMessage).count() == 0


def test_a_handled_chat_stops_the_arm_from_treating_it_as_content(app, db_session, monkeypatch):
    """The arm's fallback path: `find_community` finds nothing, so `process_chat`
    is tried, and a TRUE return means it handled the activity and the arm must
    stop. Proven by `ensure_domains_match` never being reached.

    The object type here is NOT ChatMessage -- it is a Page, so the arm reaches
    the fallback rather than the dedicated ChatMessage branch. That is the only
    call site where the return value is read.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    record_moderation(monkeypatch, 'publish_sse_event')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    calls = record_moderation(monkeypatch, 'ensure_domains_match')

    activity = inbox_activity(sender, activity_type='Create',
                              object={'type': 'Page', 'to': recipient.ap_profile_id,
                                      'content': 'hello', 'id': 'https://peer.example/pm/1'})
    dispatch(activity)

    assert calls['ensure_domains_match'] == []
    assert db_session.query(ChatMessage).filter_by(ap_id='https://peer.example/pm/1').one()


def test_an_unhandled_chat_lets_the_arm_continue_to_the_domain_check(app, db_session, monkeypatch):
    """The mirror: `process_chat` returns FALSE (no resolvable recipient), so the
    arm does NOT stop and goes on to `ensure_domains_match`. Together with the
    test above this pins the return value in both directions -- which no
    assertion inside process_chat's own tests can do.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json: None)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: False)

    activity = inbox_activity(sender, activity_type='Create',
                              object={'type': 'Page', 'content': 'hello',
                                      'id': 'https://peer.example/pm/1'})
    dispatch(activity)

    messages = [l.exception_message for l in ActivityPubLog.query.all()]
    assert 'Domains do not match' in messages
