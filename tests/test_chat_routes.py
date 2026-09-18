"""app/chat/routes.py's conversation surface: chat_home, new_message and
chat_conversation.

MEASUREMENT BASIS. Before this file existed the three carried 60 missing
statements and 34 missing arcs on the full-suite --cov=app run at f694a7dd4,
and app/chat/routes.py stood at 22.3%.

Three defects are pinned here and repaired together, all three in chat_home
and new_message:

  P1  chat_home's POST path ran before any membership check, so a verified
      non-member could write a message into any conversation by id. The GET
      path has checked since before the campaign started (routes.py:50).
  P2  new_message compared ints against the Row tuples a raw execute().all()
      returns, so the duplicate-conversation guard could never be true and a
      second conversation was created for a pair that already had one.
  P3  chat_home is registered on /chat as well as /chat/<id>, and the POST
      path passed conversation_id=None into send_message, where
      Conversation.query.get(None) returns None and .id raised.

Facts 292-294 apply: render_template is patched for anything that renders,
POST routes need a real CSRF token, and the site fixture supplies g.site.
"""
from datetime import timedelta

import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import NOTIF_MESSAGE
from app.models import ChatMessage, Conversation, Notification, Site, User, utcnow
from tests.factories import make_conversation, make_instance, make_user, make_user_block

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def _aged(user, days=30):
    """new_message is decorated with trustworthy_account_required
    (app/utils.py:1903-1911), which redirects to /auth/not_trustworthy unless
    current_user.trustworthy() -- false for an account created within 7 days
    with reputation under 100 (app/models.py:1286-1291). A fixture user is
    created now, so every new_message test has to age its sender or it measures
    the decorator instead of the route.

    can_send_pm_to (app/models.py:1654-1664) refuses a sender created within
    the DAY, so the same ageing satisfies both.
    """
    user.created = utcnow() - timedelta(days=days)
    db.session.commit()
    return user


def _seed():
    """Three local users and the id-1 seat burned.

    Fixtures hard-code instance_id=1 and user_id=1 (facts 21, 61, 89), so the
    first user built here is a throwaway that takes the id-1 seat and is never
    used again.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    carol = make_user(instance, 'carol', local=True)
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob, carol


# --------------------------------------------------------------------------
# P1: the POST path's missing membership check
# --------------------------------------------------------------------------


def test_a_non_member_cannot_post_into_a_conversation(app, db_session):
    """Before the repair this returned 302 and the message landed:

        PROBE p1 status: 302 /chat/1#message
        PROBE p1 messages in foreign conversation: [(4, 'i am not a member')]

    carol is verified and unbanned, so the only guard she met was the one on
    her own standing at :27. The message was committed, given an ap_id under
    this server's SERVER_URL, and then announced to both real members by
    send_message.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, carol)
    token = csrf(app, client)

    with patch('app.chat.routes.render_template', return_value='rendered'), \
         patch('app.chat.util.publish_sse_event'):
        response = client.post(f'/chat/{conversation.id}',
                               data={'message': 'i am not a member',
                                     'csrf_token': token, 'submit': 'Reply'})

    assert response.status_code == 400
    assert ChatMessage.query.filter_by(conversation_id=conversation.id).count() == 0


def test_a_member_can_still_post(app, db_session):
    """The other half of P1's inversion. A repair that refused everyone would
    pass the test above; this is what makes the guard's true arm load-bearing.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.chat.routes.render_template', return_value='rendered'), \
         patch('app.chat.util.publish_sse_event'):
        response = client.post(f'/chat/{conversation.id}',
                               data={'message': 'i am a member',
                                     'csrf_token': token, 'submit': 'Reply'})

    assert response.status_code == 302
    assert response.headers['Location'].endswith('#message')
    sent = ChatMessage.query.filter_by(conversation_id=conversation.id).all()
    assert [(m.sender_id, m.body) for m in sent] == [(alice.id, 'i am a member')]


def test_an_admin_who_is_not_a_member_cannot_post(app, db_session):
    """The design's decision: the POST path is STRICTER than the read path.

    routes.py:50 lets an admin read a foreign conversation, which is a
    moderation need; writing into one produces a message attributed to the
    admin inside someone else's thread, which nothing asks for and which
    existed only because nothing was checked at all.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    client = app.test_client()
    login(client, carol)
    token = csrf(app, client)

    with patch('app.chat.routes.render_template', return_value='rendered'), \
         patch('app.chat.util.publish_sse_event'):
        response = client.post(f'/chat/{conversation.id}',
                               data={'message': 'admin writing in',
                                     'csrf_token': token, 'submit': 'Reply'})

    assert response.status_code == 400
    assert ChatMessage.query.filter_by(conversation_id=conversation.id).count() == 0


def _make_admin(user):
    """User.is_admin() (app/models.py:1259-1265) is id == 1 or a role literally
    named 'Admin'. The id-1 seat is burned by the seed, so an admin here is the
    role, written through the plain `user_role` table -- there is no UserRole
    model (see tests/factories.py:365-381).
    """
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def test_posting_to_an_unknown_conversation_is_a_404(app, db_session):
    """The guard has to load the conversation to ask about membership, so an
    unknown id is now a 404 on POST as it already was on GET -- rather than the
    AttributeError the old path gave.
    """
    instance, alice, bob, carol = _seed()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.chat.routes.render_template', return_value='rendered'), \
         patch('app.chat.util.publish_sse_event'):
        response = client.post('/chat/999',
                               data={'message': 'nowhere', 'csrf_token': token,
                                     'submit': 'Reply'})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# P2: the duplicate-conversation guard that could never fire
# --------------------------------------------------------------------------


def test_new_message_redirects_to_an_existing_conversation(app, db_session):
    """Before the repair the guard read

        if current_user.id in members and recipient.id in members

    where members came from execute(...).all() and so held Row tuples. The
    probe:

        PROBE p2 rows: [(2,), (3,)] alice.id in rows: False
        PROBE p2 conversations after post: 2

    Both halves are asserted: the redirect names the EXISTING conversation, and
    no second row appears. A repair that stopped creating rows without
    redirecting would leave the user on a dead form and pass a count-only test.
    """
    instance, alice, bob, carol = _seed()
    _aged(alice)
    _aged(bob)
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/{bob.id}/new')

    assert response.status_code == 302
    assert response.headers['Location'] == f'/chat/{conversation.id}#message'
    assert Conversation.query.count() == 1


# --------------------------------------------------------------------------
# P3: a POST to /chat with no conversation id
# --------------------------------------------------------------------------


def test_posting_without_a_conversation_id_redirects_to_empty(app, db_session):
    """chat_home is registered on /chat as well as /chat/<int:conversation_id>
    (routes.py:17-18). Before the repair:

        PROBE p3 exception: AttributeError 'NoneType' object has no attribute 'id'

    -- Conversation.query.get(None) returns None and send_message reads .id off
    it (util.py:13-14). The redirect goes where :37 already sends a user with
    no conversations.
    """
    instance, alice, bob, carol = _seed()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.chat.routes.render_template', return_value='rendered'), \
         patch('app.chat.util.publish_sse_event'):
        response = client.post('/chat', data={'message': 'no conversation',
                                              'csrf_token': token,
                                              'submit': 'Reply'})

    assert response.status_code == 302
    assert response.headers['Location'] == '/chat/empty'
    assert ChatMessage.query.count() == 0


# --------------------------------------------------------------------------
# chat_home: the POST path's standing guard
# --------------------------------------------------------------------------


def _message(conversation, sender, recipient, body='hello', read=False):
    """A ChatMessage INSIDE a conversation.

    tests/factories.py's make_chat_message leaves conversation_id NULL on
    purpose (the Undo/Delete restore path never reads it), and every assertion
    here is about messages the conversation owns, so they are built locally.
    """
    message = ChatMessage(sender_id=sender.id, recipient_id=recipient.id,
                          conversation_id=conversation.id, body=body,
                          body_html=f'<p>{body}</p>', read=read)
    db.session.add(message)
    db.session.commit()
    return message


def _post_reply(app, client, conversation_id, body='a reply'):
    token = csrf(app, client)
    with patch('app.chat.routes.render_template', return_value='rendered'), \
         patch('app.chat.util.publish_sse_event'):
        return client.post(f'/chat/{conversation_id}',
                           data={'message': body, 'csrf_token': token,
                                 'submit': 'Reply'})


@pytest.mark.parametrize('attribute, value', [
    ('banned', True),
    ('verified', False),
    ('can_send_pm', False),
])
def test_a_sender_who_may_not_send_is_denied(app, db_session, attribute, value):
    """routes.py:27 is a three-way `or`, and one row would leave two of its
    operands load-bearing for nothing. Each of the three is a member of the
    conversation, so the refusal measured here is the standing guard and not
    the membership check below it.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    setattr(alice, attribute, value)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    response = _post_reply(app, client, conversation.id)

    assert response.status_code == 302
    assert response.headers['Location'] == '/chat/denied'
    assert ChatMessage.query.count() == 0


# --------------------------------------------------------------------------
# chat_home: the GET path without a conversation id
# --------------------------------------------------------------------------


def test_the_chat_index_opens_the_most_recently_updated_conversation(app, db_session):
    """The redirect follows `order_by(desc(Conversation.updated_at))`, so the
    two conversations are given different timestamps IN THE WRONG ORDER -- the
    older one is created second. Without that, the arrival order would satisfy
    the assertion on its own and the sort would be untested.
    """
    instance, alice, bob, carol = _seed()
    recent = make_conversation(alice, bob)
    stale = make_conversation(alice, carol)
    recent.updated_at = utcnow() - timedelta(minutes=1)
    stale.updated_at = utcnow() - timedelta(days=2)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get('/chat')

    assert response.status_code == 302
    assert response.headers['Location'] == f'/chat/{recent.id}'


def test_the_chat_index_with_no_conversations_redirects_to_empty(app, db_session):
    instance, alice, bob, carol = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get('/chat')

    assert response.status_code == 302
    assert response.headers['Location'] == '/chat/empty'


# --------------------------------------------------------------------------
# chat_home: reading a conversation
# --------------------------------------------------------------------------


def test_reading_a_conversation_marks_only_the_readers_own_messages_read(app, db_session):
    """routes.py:55 reads `if message.recipient_id == current_user.id`, so the
    fixture needs a message in each direction: the one addressed to the reader
    becomes read, the one they sent does not. A single-message fixture would
    pass whatever that line said.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    to_alice = _message(conversation, bob, alice, 'for alice')
    from_alice = _message(conversation, alice, bob, 'from alice')
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{conversation.id}')

    assert response.status_code == 200
    db.session.expire_all()
    assert ChatMessage.query.get(to_alice.id).read is True
    assert ChatMessage.query.get(from_alice.id).read is False
    assert render.call_args.kwargs['conversation'].id == conversation.id


def test_an_admin_may_read_a_conversation_they_are_not_in(app, db_session):
    """The read path's admin arm (routes.py:50), which the POST path
    deliberately does not have -- see test_an_admin_who_is_not_a_member_cannot_post.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    client = app.test_client()
    login(client, carol)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/{conversation.id}')

    assert response.status_code == 200


def test_a_stranger_cannot_read_a_conversation(app, db_session):
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, carol)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/{conversation.id}')

    assert response.status_code == 400


def test_a_member_who_has_left_sees_no_messages(app, db_session):
    """routes.py:52's `if conversations:` false arm.

    `is_member` reads the association rows regardless of `joined`
    (app/models.py:208-212), while the conversation list at :40 filters on
    `joined == True`. So a member who has left passes the membership check and
    still gets an EMPTY list, and the route hands the template `messages = []`
    although the conversation has messages in it.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _message(conversation, bob, alice, 'you will not see this')
    db.session.execute(db.text(
        "UPDATE conversation_member SET joined = :state WHERE user_id = :person_id "
        "AND conversation_id = :conversation_id"),
        {"state": False, "person_id": alice.id, "conversation_id": conversation.id})
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{conversation.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['messages'] == []


@pytest.mark.parametrize('leavers, alone', [
    ([], False),
    (['bob'], True),
])
def test_the_alone_flag_counts_joined_members(app, db_session, leavers, alone):
    """`alone` is `len(members) == 1` over the JOINED rows (routes.py:63-66),
    so the two rows differ only in whether bob has left.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    for name in leavers:
        leaver = User.query.filter_by(user_name=name).one()
        db.session.execute(db.text(
            "UPDATE conversation_member SET joined = :state WHERE user_id = :person_id "
            "AND conversation_id = :conversation_id"),
            {"state": False, "person_id": leaver.id, "conversation_id": conversation.id})
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{conversation.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['alone'] is alone


def _notification(user, url, author):
    notification = Notification(title='New message', url=url, user_id=user.id,
                                author_id=author.id, notif_type=NOTIF_MESSAGE,
                                subtype='chat_message', read=False)
    db.session.add(notification)
    user.unread_notifications = Notification.query.filter_by(user_id=user.id,
                                                             read=False).count()
    db.session.commit()
    return notification


def test_opening_a_conversation_clears_only_that_conversations_notifications(app, db_session):
    """The sweep at routes.py:68 matches on `url LIKE '/chat/<id>%'` AND
    `user_id`, then recounts what is left. Three notifications make all three
    parts load-bearing: this conversation's is cleared, another conversation's
    is not, and one belonging to a different user is not -- and the recount
    afterwards is what the user's badge shows.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    other = make_conversation(alice, carol)
    mine = _notification(alice, f'/chat/{conversation.id}#message_1', bob)
    elsewhere = _notification(alice, f'/chat/{other.id}#message_2', carol)
    someone_elses = _notification(bob, f'/chat/{conversation.id}#message_3', alice)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/{conversation.id}')

    assert response.status_code == 200
    db.session.expire_all()
    assert Notification.query.get(mine.id).read is True
    assert Notification.query.get(elsewhere.id).read is False
    assert Notification.query.get(someone_elses.id).read is False
    assert User.query.get(alice.id).unread_notifications == 1


# --------------------------------------------------------------------------
# new_message
# --------------------------------------------------------------------------


def test_a_sender_who_may_not_send_pms_is_denied(app, db_session):
    """routes.py:86 asks can_send_pm_to, which refuses an account created
    within the DAY (app/models.py:1654-1664). alice is aged past
    trustworthy_account_required's seven days but her reputation is put below
    -10, so the decorator passes and the route's own guard is what refuses --
    otherwise this would measure the decorator.
    """
    instance, alice, bob, carol = _seed()
    _aged(alice)
    alice.reputation = -20
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/{bob.id}/new')

    assert response.status_code == 302
    assert response.headers['Location'] == '/chat/denied'


@pytest.mark.parametrize('direction', ['recipient_blocked_sender', 'sender_blocked_recipient'])
def test_a_block_in_either_direction_stops_a_new_message(app, db_session, direction):
    """routes.py:89 is an `or` over the two directions, and one row would leave
    the other operand load-bearing for nothing.
    """
    instance, alice, bob, carol = _seed()
    _aged(alice)
    if direction == 'recipient_blocked_sender':
        make_user_block(bob, alice)
    else:
        make_user_block(alice, bob)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/{bob.id}/new')

    assert response.status_code == 302
    assert response.headers['Location'] == '/chat/blocked'
    assert Conversation.query.count() == 0


def test_the_new_message_form_renders_when_there_is_no_conversation(app, db_session):
    instance, alice, bob, carol = _seed()
    _aged(alice)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{bob.id}/new')

    assert response.status_code == 200
    assert render.call_args.kwargs['recipient'].id == bob.id
    assert render.call_args.args[0] == 'chat/new_message.html'
    assert Conversation.query.count() == 0


def test_sending_a_first_message_creates_the_conversation(app, db_session):
    """Both members are appended (routes.py:103-104), so the assertion is on
    the membership rows rather than on the row count: a conversation missing
    either row is invisible to find_existing_conversation and to the chat index.
    """
    instance, alice, bob, carol = _seed()
    _aged(alice)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.chat.routes.render_template', return_value='rendered'), \
         patch('app.chat.util.publish_sse_event'):
        response = client.post(f'/chat/{bob.id}/new',
                               data={'message': 'first contact',
                                     'csrf_token': token, 'submit': 'Send'})

    conversation = Conversation.query.one()
    assert response.status_code == 302
    assert response.headers['Location'] == f'/chat/{conversation.id}#message'
    assert conversation.user_id == alice.id
    assert sorted(member.id for member in conversation.members) == sorted([alice.id, bob.id])
    assert [(m.sender_id, m.recipient_id, m.body) for m in ChatMessage.query.all()] == \
        [(alice.id, bob.id, 'first contact')]


def test_a_recipient_who_has_left_still_takes_the_redirect(app, db_session):
    """find_existing_conversation (app/models.py:254-273) joins the membership
    rows without looking at `joined`, and the id set at :96 comes from a query
    that DOES filter on it -- so a conversation the recipient has left is found
    but its member set holds only the sender, and the guard at :97 is false.

    The route therefore falls through to the form and a SECOND conversation is
    created for the pair. Recorded as behaviour rather than asserted as
    correct: see D744.
    """
    instance, alice, bob, carol = _seed()
    _aged(alice)
    conversation = make_conversation(alice, bob)
    db.session.execute(db.text(
        "UPDATE conversation_member SET joined = :state WHERE user_id = :person_id "
        "AND conversation_id = :conversation_id"),
        {"state": False, "person_id": bob.id, "conversation_id": conversation.id})
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{bob.id}/new')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'chat/new_message.html'


def test_a_new_message_to_an_unknown_user_is_a_404(app, db_session):
    instance, alice, bob, carol = _seed()
    _aged(alice)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get('/chat/999/new')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# chat_conversation, the htmx refresh
# --------------------------------------------------------------------------


def test_the_refresh_marks_only_the_readers_own_messages_read(app, db_session):
    """P4, found by this test rather than by the scoping probe: the route set
    `read = True` and never committed, so every mark it made was discarded at
    teardown and the htmx refresh -- whose whole job is to show a conversation
    the reader has just opened -- left the messages unread. chat_home makes the
    same assignment and survives only because the notification sweep below it
    commits (routes.py:70).

    Before the repair this failed at the first assertion:
    `assert False is True where False = <ChatMessage 1>.read`.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    to_alice = _message(conversation, bob, alice, 'for alice')
    from_alice = _message(conversation, alice, bob, 'from alice')
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/refresh-conversation/{conversation.id}')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'chat/_messages.html'
    db.session.expire_all()
    assert ChatMessage.query.get(to_alice.id).read is True
    assert ChatMessage.query.get(from_alice.id).read is False


def test_an_admin_may_refresh_a_conversation_they_are_not_in(app, db_session):
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    client = app.test_client()
    login(client, carol)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/refresh-conversation/{conversation.id}')

    assert response.status_code == 200


def test_the_refresh_gives_a_stranger_an_empty_body(app, db_session):
    """routes.py:263 returns '' rather than falling off the end, which is what
    chat_options and chat_report do in the same position -- and those two are a
    500 for it (D745).
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _message(conversation, bob, alice, 'private')
    client = app.test_client()
    login(client, carol)

    response = client.get(f'/chat/refresh-conversation/{conversation.id}')

    assert response.status_code == 200
    assert response.get_data(as_text=True) == ''


def test_refreshing_an_unknown_conversation_is_a_404(app, db_session):
    instance, alice, bob, carol = _seed()
    client = app.test_client()
    login(client, alice)

    response = client.get('/chat/refresh-conversation/999')

    assert response.status_code == 404
