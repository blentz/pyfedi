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


def test_a_trusted_instances_recipient_refuses_a_sender_with_no_instance(app, db_session, monkeypatch):
    """D124, fixed. `sender.instance.trusted` was read with no check that the
    nullable `instance` exists, so such a sender raised AttributeError. A
    sender with no instance is not from a trusted one, and is refused.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair(accept=2, trusted=True)
    activity = chat_activity(sender, to=recipient.ap_profile_id,
                             content='hello', id='https://peer.example/pm/1')
    sender.instance_id = None
    db.session.commit()

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Sender from untrusted instance'
    assert db_session.query(ChatMessage).count() == 0


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


def test_a_message_containing_a_blocked_phrase_is_refused(app, db_session, monkeypatch):
    """`blocked_phrases()` reads newline-separated `Site.blocked_phrases`, and
    `make_site()` sets it to '' — so this test writes the column itself. The
    refusal message embeds the matched phrase, so the assertion pins the whole
    string rather than just the failure.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db_session.get(Site, 1).blocked_phrases = 'buymynft\nspamword'
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello buymynft friend', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Blocked because phrase buymynft'
    assert db_session.query(ChatMessage).count() == 0


def test_a_message_containing_no_blocked_phrase_is_delivered(app, db_session, monkeypatch):
    """The other side: the site HAS blocked phrases configured, but this message
    matches none of them. Paired with the test above so the filter cannot be
    removed without a failure — a test with no phrases configured would pass
    either way.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db_session.get(Site, 1).blocked_phrases = 'buymynft\nspamword'
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='an ordinary message', id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_an_existing_conversation_is_reused_rather_than_duplicated(app, db_session, monkeypatch):
    """`find_existing_conversation` joins `conversation_member` twice, so a
    conversation is only found when BOTH parties are members — which is exactly
    what `make_conversation` builds. Reuse is asserted by the total conversation
    count staying at 1, not merely by the message landing.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    existing = make_conversation(sender, recipient)
    existing_id = existing.id
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    db.session.expire_all()
    assert db_session.query(Conversation).count() == 1
    message = db_session.query(ChatMessage).one()
    assert message.conversation_id == existing_id


def test_a_first_message_creates_the_conversation(app, db_session, monkeypatch):
    """No conversation exists, so one is created with both parties as members —
    asserted through `find_existing_conversation` so the association rows are
    proven written, not just the Conversation row.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    db.session.expire_all()
    assert db_session.query(Conversation).count() == 1
    assert Conversation.find_existing_conversation(recipient=recipient, sender=sender) is not None


def test_a_new_message_is_stored_with_both_body_forms_and_notifies(app, db_session, monkeypatch):
    """The create path. `body_html` keeps the sent markup and `body` is its
    text rendering via `html_to_text`, so both are asserted — storing only one
    would lose either formatting or searchability.

    `publish_sse_event` is doubled because it reaches an external event stream;
    its call is asserted rather than merely allowed, since a silent failure to
    publish would leave a live client showing nothing.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    recipient.unread_notifications = 0
    db.session.commit()
    recipient_id = recipient.id
    calls = record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='<p>hello there</p>', id='https://peer.example/pm/1'))

    db.session.expire_all()
    message = db_session.query(ChatMessage).one()
    assert message.body_html == '<p>hello there</p>'
    assert 'hello there' in message.body
    assert message.sender_id == sender.id and message.recipient_id == recipient_id

    assert len(calls['publish_sse_event']) == 1
    notification = db_session.query(Notification).one()
    assert notification.user_id == recipient_id
    assert notification.title.startswith('New message from')
    assert db_session.get(User, recipient_id).unread_notifications == 1

    log = ActivityPubLog.query.one()
    assert log.result == 'success'


def test_a_repeat_ap_id_updates_the_existing_message_and_says_so(app, db_session, monkeypatch):
    """The update path, selected by an `ap_id` that already exists. The
    notification title distinguishes it ('Updated message from'), and the
    message count staying at 1 proves an update rather than a second row.

    `read` is seeded True beforehand so its reset to False is evidence of the
    write rather than a default sitting there.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    conversation = make_conversation(sender, recipient)
    existing = ChatMessage(sender_id=sender.id, recipient_id=recipient.id,
                           conversation_id=conversation.id, body='old', body_html='<p>old</p>',
                           ap_id='https://peer.example/pm/1', read=True)
    db.session.add(existing)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='<p>new text</p>', id='https://peer.example/pm/1'))

    db.session.expire_all()
    assert db_session.query(ChatMessage).count() == 1
    message = db_session.query(ChatMessage).one()
    assert message.body_html == '<p>new text</p>'
    assert message.read is False

    notification = db_session.query(Notification).one()
    assert notification.title.startswith('Updated message from')


def test_an_encrypted_flag_is_carried_through_and_defaults_to_none(app, db_session, monkeypatch):
    """`encrypted` is read with a membership check — the same as `content` and
    `id`, guarded twenty lines above this read (routes.py:2566-2571) since the
    Task 9 fix. Both halves are covered here: supplied, and absent -- the
    second dispatch's `encrypted is None` is process_chat's own `else None`
    (routes.py:2588), not the ChatMessage column's declared default, because
    it is explicitly passed to the constructor either way.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()
    record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id, content='hello',
                           id='https://peer.example/pm/1', encrypted='pgp'))

    assert db_session.query(ChatMessage).filter_by(
        ap_id='https://peer.example/pm/1').one().encrypted == 'pgp'

    dispatch(chat_activity(sender, to=recipient.ap_profile_id, content='hello again',
                           id='https://peer.example/pm/2'))

    assert db_session.query(ChatMessage).filter_by(
        ap_id='https://peer.example/pm/2').one().encrypted is None


def test_a_chat_message_with_no_content_is_refused_not_crashed(app, db_session, monkeypatch):
    """Task 9 fix for defect 1. `core_activity['object']['content']` used to be
    read with no membership check, first by the blocked-phrase filter
    (routes.py:2567) and again when building the body (routes.py:2587-2588).
    `content` is peer-controlled, so any peer could raise a KeyError out of a
    Celery task. Now a missing `content` is refused through the arm's normal
    idiom -- logged and returned -- rather than raised.

    The contrast is a few lines above in the same function: `object['to']` IS
    checked for membership AND for both plausible JSON-LD shapes, and
    `object['encrypted']` is read with an `in` guard. This closes the gap
    between those two reads.

    The sender is aged past `created_very_recently()` and the recipient left at
    an accepting setting, so the new guard is reached rather than
    short-circuited by an earlier refusal.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           id='https://peer.example/pm/1'))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert 'content' in log.exception_message.lower()
    assert db_session.query(ChatMessage).count() == 0


def test_a_chat_message_with_no_id_is_refused_not_crashed(app, db_session, monkeypatch):
    """Task 9 fix for defect 2, the sibling unguarded read.
    `core_activity['object']['id']` used to be read with no membership check,
    used for the existing-message lookup (routes.py:2589) and for the new
    row's `ap_id` (routes.py:2596). Now a missing `id` is refused the same way
    a missing `content` is.

    `content` IS supplied here, so this test is refused for its own reason
    rather than for the previous test's -- without that, both tests would pass
    on a single missing-field guard and neither would pin its own defect.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    db.session.commit()

    dispatch(chat_activity(sender, to=recipient.ap_profile_id, content='hello'))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'ChatMessage has no id'
    assert db_session.query(ChatMessage).count() == 0


def test_the_inner_is_local_check_can_never_be_false(app, db_session, monkeypatch):
    """PINS defect 3, an equivalent mutant that Task 9 (commit `d0c8d13f`)
    removed. The SSE event and notification used to sit under a second
    `if recipient.is_local():` check, nested inside the block already entered
    via `if recipient and recipient.is_local():` (routes.py:2548). That is
    not the same call on the same object twice: `recipient` is reassigned at
    routes.py:2549 to a row fetched by `session.query(User).get(recipient.id)`
    -- a different object from a different session than the one
    `find_actor_or_create_cached` returned at routes.py:2547, representing the
    same row by id. The comment on routes.py:2549 exists precisely to flag
    that distinction ("for some reason find_actor_or_create_cached was giving
    me a user from the wrong DB session"). Nothing between that reassignment
    and the removed inner check ever wrote `recipient.ap_id` or
    `recipient.ap_profile_id` (the fields `User.is_local()` reads), so the
    inner check could never observe a different truth value than the outer
    one already established.

    This test cannot observe the inner guard directly; what it establishes is
    that every accepted message notifies, so there is no reachable case where
    the outer check passes and the inner one does not. Task 9 removed the
    inner guard, and this test kept passing unchanged -- that is the proof it
    was dead.

    Same class as D95 (Remove's dead `if proceed:`), D96 (Block's dead Mastodon
    isinstance) and D103 (the site-ban already_banned guard).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance, sender, recipient = seed_chat_pair()
    sender.created = utcnow() - timedelta(days=2)
    recipient.unread_notifications = 0
    db.session.commit()
    recipient_id = recipient.id
    calls = record_moderation(monkeypatch, 'publish_sse_event')

    dispatch(chat_activity(sender, to=recipient.ap_profile_id,
                           content='hello', id='https://peer.example/pm/1'))

    db.session.expire_all()
    assert len(calls['publish_sse_event']) == 1
    assert db_session.query(Notification).count() == 1
    assert db_session.get(User, recipient_id).unread_notifications == 1
