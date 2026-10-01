"""The private-message API: reading a conversation, sending to it, leaving
it, and reporting it.

Sub-project 84, slice A -- `app/api/alpha/utils/private_message.py`. Three
defects, all measured:

* `post_private_message_conversation_report` tested `if not (conversation or
  conversation.is_member(user) or user_access(...))` -- **`or`** where each
  arm was meant to be required -- so a conversation that exists made the whole
  disjunction true and NOTHING was refused. Any account could report any
  conversation by id, and the admin report list hands back that
  conversation's message history (D1167);
* `get_private_message_conversation` read `data['person_id']` ten lines above
  the `if 'person_id' in data` meant to guard it, so every call that did not
  pass one -- including the `conversation_id` form the endpoint documents --
  was `KeyError: 'person_id'` (D1168);
* both report-resolve endpoints read `.targets` and
  `.suspect_conversation_id` off a `db.session.get` that answers None
  (D1169).
"""
import pytest
from flask import g

from app import db
from app.constants import (NOTIF_MESSAGE, REPORT_STATE_NEW,
                           REPORT_STATE_RESOLVED, REPORT_TYPE_MESSAGE)
from app.models import (ChatMessage, Conversation, Notification, Report, Role,
                        RolePermission, Site, User, user_role)
from tests.factories import make_chat_message, make_conversation, make_user


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def make_admin(user):
    """`user_access('administer all users', id)` reads role_permission joined
    to the `user_role` association table; there is no UserRole model.

    `Site.admins()` answers from `g.admin_ids` whenever that is set -- which
    the fixture sets to [] so `private_message_view` can run -- so the new
    admin has to be added there too or nobody is notified (fact 548).
    """
    role = Role(name=f'admin-{user.id}', weight=10)
    db.session.add(role)
    db.session.commit()
    db.session.add(RolePermission(role_id=role.id,
                                  permission='administer all users'))
    db.session.execute(user_role.insert().values(user_id=user.id,
                                                 role_id=role.id))
    db.session.commit()
    g.admin_ids = list(getattr(g, 'admin_ids', [])) + [user.id]
    return user


def a_message(sender, recipient, conversation, body='SECRETBODY', **columns):
    message = make_chat_message(
        sender, recipient,
        f'https://test.piefed.local/m/{sender.id}-{recipient.id}-{body}')
    message.conversation_id = conversation.id
    message.body = body
    message.body_html = f'<p>{body}</p>'
    for column, value in columns.items():
        setattr(message, column, value)
    db.session.commit()
    return message


@pytest.fixture
def env(app, api_baseline):
    """`alice` and `bob` are talking; `stranger` is not in the conversation,
    which is what both D1167 and the membership filters are about.

    `private_message_view` reads `g.admin_ids`, which a real request gets from
    `before_request` and a direct call to the util function does not.
    """
    g.admin_ids = []
    alice = api_baseline.user1
    bob = api_baseline.user2
    stranger = api_baseline.user3
    conversation = make_conversation(alice, bob)
    message = a_message(alice, bob, conversation)
    return alice, bob, stranger, conversation, message


# --------------------------------------------------------------------------
# D1167 -- the report that showed admins somebody else's conversation
# --------------------------------------------------------------------------


def test_a_stranger_may_not_report_a_conversation(app, env):
    """D1167. `or` where each arm was meant to be required: a conversation
    that exists made the disjunction true, so `not` was false and nothing was
    refused. Measured: `PROBE ba1 outcome: accepted | reports filed: 1`."""
    from app.api.alpha.utils.private_message import \
        post_private_message_conversation_report

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception) as refused:
        post_private_message_conversation_report(
            token(stranger),
            {'conversation_id': conversation.id, 'reason': 'because'})

    assert 'not a part of this conversation' in str(refused.value)
    assert Report.query.count() == 0


def test_a_strangers_report_does_not_reach_the_administrators(app, env):
    """D1167's consequence, which is what makes it serious: the conversation
    report list hands an admin the message history of every reported
    conversation. Measured: `PROBE ba2 1 reports, bodies: ['SECRETBODY']`."""
    from app.api.alpha.utils.private_message import (
        get_private_message_conversation_report_list,
        post_private_message_conversation_report)

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))

    with pytest.raises(Exception):
        post_private_message_conversation_report(
            token(stranger),
            {'conversation_id': conversation.id, 'reason': 'because'})

    listed = get_private_message_conversation_report_list(token(admin), {})

    assert listed['conversation_reports'] == []


def api_baseline_admin(app):
    admin = make_user(None, 'theadmin', local=True)
    admin.verified = True
    db.session.commit()
    return admin


def test_a_member_may_report_their_own_conversation(app, env):
    """The feature the guard must leave working, and what it does: a report
    row, and every admin told."""
    from app.api.alpha.utils.private_message import \
        post_private_message_conversation_report

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))

    post_private_message_conversation_report(
        token(alice), {'conversation_id': conversation.id,
                       'reason': 'they are being unpleasant'})

    report = Report.query.one()
    assert report.suspect_conversation_id == conversation.id
    assert report.reporter_id == alice.id
    assert report.type == REPORT_TYPE_MESSAGE
    assert Notification.query.filter_by(user_id=admin.id).count() == 1
    assert admin.unread_notifications == 1


def test_an_administrator_may_report_any_conversation(app, env):
    """The third arm: somebody who administers all users is not a member and
    is allowed anyway."""
    from app.api.alpha.utils.private_message import \
        post_private_message_conversation_report

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))

    post_private_message_conversation_report(
        token(admin), {'conversation_id': conversation.id, 'reason': 'spam'})

    assert Report.query.count() == 1


def test_a_conversation_that_does_not_exist_cannot_be_reported(app, env):
    """The other half of D1167's expression: with no conversation,
    `conversation.is_member` was an AttributeError on None."""
    from app.api.alpha.utils.private_message import \
        post_private_message_conversation_report

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception) as refused:
        post_private_message_conversation_report(
            token(alice), {'conversation_id': 999999, 'reason': 'because'})

    assert 'not a part of this conversation' in str(refused.value)


# --------------------------------------------------------------------------
# D1168 -- reading a conversation
# --------------------------------------------------------------------------


def test_a_conversation_can_be_read_by_its_id(app, env):
    """D1168. `person_id = int(data['person_id'])` stood ten lines above the
    `if 'person_id' in data` meant to guard it, so this documented form was
    `KeyError: 'person_id'` for everybody. Measured: `PROBE ba3 outcome:
    KeyError: 'person_id'`."""
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation

    alice, bob, stranger, conversation, message = env

    answer = get_private_message_conversation(
        token(alice), {'conversation_id': conversation.id})

    assert [m['private_message']['content']
            for m in answer['private_messages']] == ['SECRETBODY']


def test_a_conversation_can_be_read_by_the_other_person(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation

    alice, bob, stranger, conversation, message = env

    answer = get_private_message_conversation(token(alice),
                                              {'person_id': bob.id})

    assert len(answer['private_messages']) == 1


def test_asking_for_nothing_answers_nothing(app, env):
    """Neither id given: an empty list, not an exception. Measured before the
    fix as `PROBE ba6 outcome: KeyError: 'person_id'`."""
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation

    alice, bob, stranger, conversation, message = env

    assert get_private_message_conversation(token(alice), {}) == {
        'private_messages': [], 'next_page': None}


def test_a_stranger_reads_nothing_of_somebody_elses_conversation(app, env):
    """The person_id form collects every conversation that person is in, and
    the membership filter is what keeps a stranger out of them."""
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation

    alice, bob, stranger, conversation, message = env

    answer = get_private_message_conversation(token(stranger),
                                              {'person_id': alice.id})

    assert answer['private_messages'] == []


def test_a_stranger_may_not_read_a_conversation_by_its_id(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception) as refused:
        get_private_message_conversation(token(stranger),
                                         {'conversation_id': conversation.id})

    assert 'not a member of this conversation' in str(refused.value)


def test_a_conversation_id_that_does_not_resolve_is_refused(app, env):
    """`db.session.get` answers None, and the line after it read `.id`."""
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception) as refused:
        get_private_message_conversation(token(alice),
                                         {'conversation_id': 999999})

    assert 'not a member of this conversation' in str(refused.value)


def test_somebody_who_has_left_reads_nothing_more_of_it(app, env):
    """`joined = false` is what leaving writes, and both filters read it."""
    from app.api.alpha.utils.private_message import (
        get_private_message_conversation, post_leave_conversation)

    alice, bob, stranger, conversation, message = env
    post_leave_conversation(token(alice), {'conversation_id': conversation.id})

    with pytest.raises(Exception) as refused:
        get_private_message_conversation(token(alice),
                                         {'conversation_id': conversation.id})

    assert 'not a member of this conversation' in str(refused.value)


def test_a_long_conversation_is_paged(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation

    alice, bob, stranger, conversation, message = env
    for number in range(4):
        a_message(alice, bob, conversation, body=f'message {number}')

    first = get_private_message_conversation(
        token(alice), {'conversation_id': conversation.id, 'limit': 2})

    assert len(first['private_messages']) == 2
    assert first['next_page'] == '2'


def test_the_page_length_is_capped(app, env):
    """A caller asking for more than PAGE_LENGTH gets PAGE_LENGTH."""
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation

    alice, bob, stranger, conversation, message = env
    for number in range(4):
        a_message(alice, bob, conversation, body=f'message {number}')

    with pytest.MonkeyPatch.context() as patched:
        patched.setitem(app.config, 'PAGE_LENGTH', 2)
        answer = get_private_message_conversation(
            token(alice), {'conversation_id': conversation.id, 'limit': 100})

    assert len(answer['private_messages']) == 2


# --------------------------------------------------------------------------
# The message list
# --------------------------------------------------------------------------


def test_the_message_list_carries_both_ends_of_a_conversation(app, env):
    from app.api.alpha.utils.private_message import get_private_message_list

    alice, bob, stranger, conversation, message = env
    a_message(bob, alice, conversation, body='and hello back')

    answer = get_private_message_list(token(alice), {})

    assert {m['private_message']['content']
            for m in answer['private_messages']} == {'SECRETBODY',
                                                     'and hello back'}


def test_the_unread_list_carries_only_what_was_received(app, env):
    from app.api.alpha.utils.private_message import get_private_message_list

    alice, bob, stranger, conversation, message = env
    a_message(bob, alice, conversation, body='unread one', read=False)

    answer = get_private_message_list(token(alice), {'unread_only': True})

    assert [m['private_message']['content']
            for m in answer['private_messages']] == ['unread one']


def test_a_conversation_that_was_left_is_out_of_the_list(app, env):
    from app.api.alpha.utils.private_message import (get_private_message_list,
                                                     post_leave_conversation)

    alice, bob, stranger, conversation, message = env
    post_leave_conversation(token(alice), {'conversation_id': conversation.id})

    answer = get_private_message_list(token(alice), {})

    assert answer['private_messages'] == []


def test_the_message_list_is_paged(app, env):
    from app.api.alpha.utils.private_message import get_private_message_list

    alice, bob, stranger, conversation, message = env
    for number in range(4):
        a_message(alice, bob, conversation, body=f'message {number}')

    answer = get_private_message_list(token(alice), {'limit': 2, 'page': 1})

    assert len(answer['private_messages']) == 2
    assert answer['next_page'] == '2'


# --------------------------------------------------------------------------
# Sending, editing, deleting, leaving
# --------------------------------------------------------------------------


def test_a_message_starts_a_conversation_when_there_is_none(app, env):
    from app.api.alpha.utils.private_message import post_private_message

    alice, bob, stranger, conversation, message = env

    answer = post_private_message(token(alice),
                                  {'recipient_id': stranger.id,
                                   'content': 'hello there'})

    assert answer['private_message_view']['private_message']['content'] == \
        'hello there'
    assert Conversation.query.count() == 2


def test_a_message_joins_the_conversation_that_already_exists(app, env):
    from app.api.alpha.utils.private_message import post_private_message

    alice, bob, stranger, conversation, message = env

    answer = post_private_message(token(alice), {'recipient_id': bob.id,
                                                 'content': 'hello again'})

    assert answer['private_message_view']['conversation_id'] == conversation.id
    assert Conversation.query.count() == 1


def test_somebody_who_may_not_send_is_refused(app, env):
    from unittest.mock import patch

    from app.api.alpha.utils.private_message import post_private_message

    alice, bob, stranger, conversation, message = env

    with patch.object(User, 'can_send_pm_to', return_value=False):
        with pytest.raises(Exception) as refused:
            post_private_message(token(alice), {'recipient_id': bob.id,
                                                'content': 'hello'})

    assert 'not permitted to send a private message' in str(refused.value)
    assert ChatMessage.query.count() == 1


def test_only_the_sender_may_edit_a_message(app, env):
    from app.api.alpha.utils.private_message import put_private_message

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception):
        put_private_message(token(bob), {'private_message_id': message.id,
                                         'content': 'something else'})

    assert message.body == 'SECRETBODY'


def test_the_sender_may_edit_their_own_message(app, env):
    from unittest.mock import patch

    from app.api.alpha.utils.private_message import put_private_message

    alice, bob, stranger, conversation, message = env

    with patch('app.chat.util.update_message') as told:
        answer = put_private_message(token(alice),
                                     {'private_message_id': message.id,
                                      'content': 'something **else**'})

    assert message.body == 'something **else**'
    assert '<strong>else</strong>' in message.body_html
    assert answer['private_message_view']['private_message']['content'] == \
        'something **else**'
    told.assert_called_once()


def test_a_deleted_message_cannot_be_edited(app, env):
    from app.api.alpha.utils.private_message import put_private_message

    alice, bob, stranger, conversation, message = env
    message.deleted = True
    db.session.commit()

    with pytest.raises(Exception):
        put_private_message(token(alice), {'private_message_id': message.id,
                                           'content': 'something else'})


def test_only_the_sender_may_delete_a_message(app, env):
    from app.api.alpha.utils.private_message import post_private_message_delete

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception):
        post_private_message_delete(token(bob),
                                    {'private_message_id': message.id,
                                     'deleted': True})

    assert message.deleted is False


def test_deleting_and_restoring_a_message_is_federated(app, env):
    from unittest.mock import patch

    from app.api.alpha.utils.private_message import post_private_message_delete

    alice, bob, stranger, conversation, message = env

    with patch('app.api.alpha.utils.private_message.task_selector') as task:
        deleted = post_private_message_delete(token(alice),
                                              {'private_message_id': message.id,
                                               'deleted': True})
        assert task.call_args.args[0] == 'delete_pm'
        restored = post_private_message_delete(token(alice),
                                               {'private_message_id': message.id,
                                                'deleted': False})
        assert task.call_args.args[0] == 'restore_pm'

    assert deleted['private_message_view']['private_message']['content'] == \
        'Deleted by author'
    assert restored['private_message_view']['private_message']['content'] == \
        'SECRETBODY'


def test_leaving_a_conversation_nobody_is_left_in_deletes_it(app, env):
    from app.api.alpha.utils.private_message import post_leave_conversation

    alice, bob, stranger, conversation, message = env

    post_leave_conversation(token(alice), {'conversation_id': conversation.id})
    post_leave_conversation(token(bob), {'conversation_id': conversation.id})

    assert db.session.get(Conversation, conversation.id) is None


def test_a_stranger_cannot_leave_a_conversation_they_are_not_in(app, env):
    from app.api.alpha.utils.private_message import post_leave_conversation

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception) as refused:
        post_leave_conversation(token(stranger),
                                {'conversation_id': conversation.id})

    assert 'not a part of this conversation' in str(refused.value)


def test_leaving_a_conversation_that_does_not_exist(app, env):
    from app.api.alpha.utils.private_message import post_leave_conversation

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception) as refused:
        post_leave_conversation(token(alice), {'conversation_id': 999999})

    assert 'not a part of this conversation' in str(refused.value)


# --------------------------------------------------------------------------
# Marking as read
# --------------------------------------------------------------------------


def a_notification(user, message, conversation, read=False):
    notification = Notification(title='New message', url='/chat/1',
                                user_id=user.id, author_id=message.sender_id,
                                notif_type=NOTIF_MESSAGE,
                                subtype='chat_message', read=read,
                                targets={'gen': '0',
                                         'conversation_id': conversation.id,
                                         'message_id': message.id})
    db.session.add(notification)
    user.unread_notifications = 1 if not read else 0
    db.session.commit()
    return notification


def test_reading_a_message_clears_its_notification(app, env):
    from app.api.alpha.utils.private_message import \
        post_private_message_mark_as_read

    alice, bob, stranger, conversation, message = env
    received = a_message(bob, alice, conversation, body='for alice')
    notification = a_notification(alice, received, conversation)

    answer = post_private_message_mark_as_read(
        token(alice), {'private_message_id': received.id, 'read': True})

    assert answer['private_message_view']['private_message']['read'] is True
    assert notification.read is True
    assert alice.unread_notifications == 0


def test_marking_a_message_unread_brings_the_notification_back(app, env):
    from app.api.alpha.utils.private_message import \
        post_private_message_mark_as_read

    alice, bob, stranger, conversation, message = env
    received = a_message(bob, alice, conversation, body='for alice', read=True)
    notification = a_notification(alice, received, conversation, read=True)

    post_private_message_mark_as_read(token(alice),
                                      {'private_message_id': received.id,
                                       'read': False})

    assert notification.read is False
    assert alice.unread_notifications == 1


def test_a_message_with_no_notification_is_still_marked(app, env):
    from app.api.alpha.utils.private_message import \
        post_private_message_mark_as_read

    alice, bob, stranger, conversation, message = env
    received = a_message(bob, alice, conversation, body='for alice')

    answer = post_private_message_mark_as_read(
        token(alice), {'private_message_id': received.id, 'read': True})

    assert answer['private_message_view']['private_message']['read'] is True


def test_only_the_recipient_may_mark_a_message_read(app, env):
    from app.api.alpha.utils.private_message import \
        post_private_message_mark_as_read

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception):
        post_private_message_mark_as_read(token(stranger),
                                          {'private_message_id': message.id,
                                           'read': True})


# --------------------------------------------------------------------------
# Reporting one message
# --------------------------------------------------------------------------


def test_only_the_recipient_may_report_a_message(app, env):
    from app.api.alpha.utils.private_message import \
        post_private_message_report

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception):
        post_private_message_report(token(stranger),
                                    {'private_message_id': message.id,
                                     'reason': 'spam'})

    assert Report.query.count() == 0


def test_the_recipient_reports_a_message(app, env):
    from app.api.alpha.utils.private_message import \
        post_private_message_report

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))

    answer = post_private_message_report(token(bob),
                                         {'private_message_id': message.id,
                                          'reason': 'spam'})

    assert message.reported is True
    report = Report.query.one()
    assert report.targets['suspect_message_id'] == message.id
    assert answer['private_message_report_view']['private_message']['id'] == \
        message.id
    assert Notification.query.filter_by(user_id=admin.id).count() == 1


# --------------------------------------------------------------------------
# The report lists, and resolving
# --------------------------------------------------------------------------


def a_message_report(reporter, message):
    report = Report(reasons='spam', description='', type=REPORT_TYPE_MESSAGE,
                    reporter_id=reporter.id,
                    suspect_conversation_id=message.conversation_id,
                    source_instance_id=1,
                    targets={'gen': '0',
                             'suspect_conversation_id': message.conversation_id,
                             'reporter_id': reporter.id,
                             'suspect_message_id': message.id})
    db.session.add(report)
    db.session.commit()
    return report


def a_conversation_report(reporter, conversation):
    report = Report(reasons='spam', type=REPORT_TYPE_MESSAGE,
                    reporter_id=reporter.id,
                    suspect_conversation_id=conversation.id,
                    source_instance_id=1,
                    targets={'gen': '0',
                             'suspect_conversation_id': conversation.id,
                             'reporter_id': reporter.id})
    db.session.add(report)
    db.session.commit()
    return report


def test_the_message_report_list_needs_an_administrator(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_report_list

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception) as refused:
        get_private_message_report_list(token(bob), {})

    assert str(refused.value) == 'incorrect login'


def test_the_message_reports_are_listed(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_report_list

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    a_message_report(bob, message)

    listed = get_private_message_report_list(token(admin), {})

    assert len(listed['private_message_reports']) == 1


def test_the_message_reports_can_be_narrowed_to_one_message(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_report_list

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    other = a_message(alice, bob, conversation, body='another')
    a_message_report(bob, message)
    a_message_report(bob, other)

    listed = get_private_message_report_list(
        token(admin), {'private_message_id': message.id})

    assert len(listed['private_message_reports']) == 1


def test_the_message_reports_can_be_narrowed_to_one_conversation(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_report_list

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    a_message_report(bob, message)

    listed = get_private_message_report_list(
        token(admin), {'conversation_id': conversation.id})

    assert len(listed['private_message_reports']) == 1


def test_resolved_message_reports_are_left_out_by_default(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_report_list

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    report = a_message_report(bob, message)
    report.status = REPORT_STATE_RESOLVED
    db.session.commit()

    assert get_private_message_report_list(
        token(admin), {})['private_message_reports'] == []
    assert len(get_private_message_report_list(
        token(admin), {'unresolved_only': False})['private_message_reports']) == 1


def test_the_conversation_report_list_needs_an_administrator(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation_report_list

    alice, bob, stranger, conversation, message = env

    with pytest.raises(Exception) as refused:
        get_private_message_conversation_report_list(token(bob), {})

    assert str(refused.value) == 'incorrect login'


def test_a_conversation_report_carries_its_recent_messages(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation_report_list

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    a_conversation_report(alice, conversation)

    listed = get_private_message_conversation_report_list(token(admin), {})

    assert len(listed['conversation_reports']) == 1
    assert [m['private_message']['content']
            for m in listed['conversation_reports'][0]['message_history']] == \
        ['SECRETBODY']


def test_the_message_history_is_capped_at_what_was_asked_for(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation_report_list

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    for number in range(4):
        a_message(alice, bob, conversation, body=f'message {number}')
    a_conversation_report(alice, conversation)

    listed = get_private_message_conversation_report_list(
        token(admin), {'message_history_limit': 2})

    assert len(listed['conversation_reports'][0]['message_history']) == 2


def test_the_conversation_reports_can_be_narrowed_to_one(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation_report_list

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    a_conversation_report(alice, conversation)
    other = make_conversation(alice, stranger)
    a_message(alice, stranger, other, body='elsewhere')
    a_conversation_report(alice, other)

    listed = get_private_message_conversation_report_list(
        token(admin), {'conversation_id': conversation.id})

    assert len(listed['conversation_reports']) == 1


def test_resolved_conversation_reports_are_left_out_by_default(app, env):
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation_report_list

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    report = a_conversation_report(alice, conversation)
    report.status = REPORT_STATE_RESOLVED
    db.session.commit()

    assert get_private_message_conversation_report_list(
        token(admin), {})['conversation_reports'] == []
    assert len(get_private_message_conversation_report_list(
        token(admin), {'unresolved_only': False})['conversation_reports']) == 1


def test_resolving_a_message_report_needs_an_administrator(app, env):
    from app.api.alpha.utils.private_message import \
        put_private_message_report_resolve

    alice, bob, stranger, conversation, message = env
    report = a_message_report(bob, message)

    with pytest.raises(Exception) as refused:
        put_private_message_report_resolve(token(bob),
                                           {'report_id': report.id,
                                            'resolved': True})

    assert str(refused.value) == 'incorrect login'


def test_a_message_report_is_resolved_and_reopened(app, env):
    from app.api.alpha.utils.private_message import \
        put_private_message_report_resolve

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    report = a_message_report(bob, message)

    answer = put_private_message_report_resolve(token(admin),
                                                {'report_id': report.id,
                                                 'resolved': True})
    assert report.status == REPORT_STATE_RESOLVED
    assert answer['private_message_report_view']['private_message']['id'] == \
        message.id

    put_private_message_report_resolve(token(admin), {'report_id': report.id,
                                                      'resolved': False})
    assert report.status == REPORT_STATE_NEW


def test_a_report_that_does_not_exist_cannot_be_resolved(app, env):
    """D1169. `db.session.get` answers None for an id that does not resolve,
    and the line below read `.targets` off it. Measured: `PROBE ba7 outcome:
    AttributeError: 'NoneType' object has no attribute 'targets'`."""
    from app.api.alpha.utils.private_message import \
        put_private_message_report_resolve

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))

    with pytest.raises(Exception) as refused:
        put_private_message_report_resolve(token(admin),
                                           {'report_id': 999999,
                                            'resolved': True})

    assert str(refused.value) == 'invalid target of resolution'


def test_a_conversation_report_is_not_a_message_report(app, env):
    from app.api.alpha.utils.private_message import \
        put_private_message_report_resolve

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    report = a_conversation_report(alice, conversation)

    with pytest.raises(Exception) as refused:
        put_private_message_report_resolve(token(admin),
                                           {'report_id': report.id,
                                            'resolved': True})

    assert str(refused.value) == 'invalid target of resolution'


def test_resolving_a_conversation_report_needs_an_administrator(app, env):
    from app.api.alpha.utils.private_message import \
        put_private_message_conversation_report_resolve

    alice, bob, stranger, conversation, message = env
    report = a_conversation_report(alice, conversation)

    with pytest.raises(Exception) as refused:
        put_private_message_conversation_report_resolve(
            token(bob), {'report_id': report.id, 'resolved': True})

    assert str(refused.value) == 'incorrect login'


def test_a_conversation_report_is_resolved_and_reopened(app, env):
    from app.api.alpha.utils.private_message import \
        put_private_message_conversation_report_resolve

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    report = a_conversation_report(alice, conversation)

    put_private_message_conversation_report_resolve(
        token(admin), {'report_id': report.id, 'resolved': True})
    assert report.status == REPORT_STATE_RESOLVED

    put_private_message_conversation_report_resolve(
        token(admin), {'report_id': report.id, 'resolved': False})
    assert report.status == REPORT_STATE_NEW


def test_a_conversation_report_that_does_not_exist(app, env):
    """D1169's twin, on `.suspect_conversation_id`."""
    from app.api.alpha.utils.private_message import \
        put_private_message_conversation_report_resolve

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))

    with pytest.raises(Exception) as refused:
        put_private_message_conversation_report_resolve(
            token(admin), {'report_id': 999999, 'resolved': True})

    assert str(refused.value) == 'invalid target of resolution'


def test_a_message_report_is_not_a_conversation_report(app, env):
    from app.api.alpha.utils.private_message import \
        put_private_message_conversation_report_resolve

    alice, bob, stranger, conversation, message = env
    admin = make_admin(api_baseline_admin(app))
    report = a_message_report(bob, message)
    report.suspect_conversation_id = None
    db.session.commit()

    with pytest.raises(Exception) as refused:
        put_private_message_conversation_report_resolve(
            token(admin), {'report_id': report.id, 'resolved': True})

    assert str(refused.value) == 'invalid target of resolution'


def test_the_history_helper_takes_an_id_or_a_conversation(app, env):
    """`_get_single_conversation_history` accepts either, because its callers
    have one or the other."""
    from app.api.alpha.utils.private_message import \
        _get_single_conversation_history

    alice, bob, stranger, conversation, message = env

    assert [m.id for m in _get_single_conversation_history(conversation.id)] == \
        [m.id for m in _get_single_conversation_history(conversation)]


def test_the_message_list_page_length_is_capped(app, env):
    from app.api.alpha.utils.private_message import get_private_message_list

    alice, bob, stranger, conversation, message = env
    for number in range(4):
        a_message(alice, bob, conversation, body=f'message {number}')

    with pytest.MonkeyPatch.context() as patched:
        patched.setitem(app.config, 'PAGE_LENGTH', 2)
        answer = get_private_message_list(token(alice), {'limit': 100})

    assert len(answer['private_messages']) == 2


def test_a_notification_about_another_message_is_left_alone(app, env):
    """The loop walks every unread chat notification and stops at the one
    naming this message; one that names a different message is not touched."""
    from app.api.alpha.utils.private_message import \
        post_private_message_mark_as_read

    alice, bob, stranger, conversation, message = env
    received = a_message(bob, alice, conversation, body='for alice')
    another = a_message(bob, alice, conversation, body='also for alice')
    elsewhere = a_notification(alice, another, conversation)

    post_private_message_mark_as_read(token(alice),
                                      {'private_message_id': received.id,
                                       'read': True})

    assert elsewhere.read is False


def test_a_counter_already_at_zero_is_not_taken_below_it(app, env):
    """`unread_notifications` is unsigned in spirit: reading a message when
    the counter already says zero leaves it there."""
    from app.api.alpha.utils.private_message import \
        post_private_message_mark_as_read

    alice, bob, stranger, conversation, message = env
    received = a_message(bob, alice, conversation, body='for alice')
    a_notification(alice, received, conversation)
    alice.unread_notifications = 0
    db.session.commit()

    post_private_message_mark_as_read(token(alice),
                                      {'private_message_id': received.id,
                                       'read': True})

    assert alice.unread_notifications == 0


def test_a_message_that_has_not_been_addressed_yet_is_not_shown(app, env):
    """`send_message` writes the row first and sets `recipient_id` once it
    knows who the other member is, so a conversation can momentarily hold a
    message addressed to nobody. The reader is not shown it."""
    from app.api.alpha.utils.private_message import \
        get_private_message_conversation

    alice, bob, stranger, conversation, message = env
    unaddressed = a_message(alice, bob, conversation, body='half written')
    unaddressed.recipient_id = None
    db.session.commit()

    answer = get_private_message_conversation(
        token(alice), {'conversation_id': conversation.id})

    assert [m['private_message']['content']
            for m in answer['private_messages']] == ['SECRETBODY']
