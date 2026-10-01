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
from tests.factories import (make_community, make_conversation, make_instance, make_user,
                             make_user_block)

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
    site = db.session.get(Site, 1)
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
    """THIS CODEBASE HAS TWO NOTIONS OF ADMIN AND A TEST NEEDS BOTH.

    `User.is_admin()` (app/models.py:1259-1265) is id == 1 or a role literally
    NAMED 'Admin'. `Site.admins()` (app/models.py:4007-4012) and the
    `g.admin_ids` the request hook computes (app/request_hooks.py:99-106) ask
    instead for a role whose ID is `ROLE_ADMIN`, which is the constant 4 -- the
    name is never read. A role satisfying only the first leaves `Site.admins()`
    returning the id-1 seat alone, which is what chat_report notifies.

    So the role here carries both: the name and the id. The id-1 seat is burned
    by the seed, and the assignment row goes through the plain `user_role`
    table -- there is no UserRole model (see tests/factories.py:365-381).
    """
    from app.constants import ROLE_ADMIN
    from app.models import Role, user_role
    role = db.session.get(Role, ROLE_ADMIN)
    if role is None:
        role = Role(id=ROLE_ADMIN, name='Admin', weight=0)
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
    assert db.session.get(ChatMessage, to_alice.id).read is True
    assert db.session.get(ChatMessage, from_alice.id).read is False
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


def test_the_alone_flag_reads_the_joined_rows_not_every_row(app, db_session):
    """The third member is what makes the query's `joined = :state` parameter
    load-bearing. In a two-member conversation with one leaver, the joined rows
    and the left rows both number one, so a mutant flipping the parameter
    agrees with the original and survives. With three members and one leaver
    they are two against one.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    conversation.members.append(carol)
    db.session.commit()
    db.session.execute(db.text(
        "UPDATE conversation_member SET joined = :state WHERE user_id = :person_id "
        "AND conversation_id = :conversation_id"),
        {"state": False, "person_id": bob.id, "conversation_id": conversation.id})
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{conversation.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['alone'] is False


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
    # a second unread elsewhere, so the recount (read=False) and its mutant
    # (read=True) cannot agree: two remain unread, one has just been read
    also_elsewhere = _notification(alice, f'/chat/{other.id}#message_4', carol)
    someone_elses = _notification(bob, f'/chat/{conversation.id}#message_3', alice)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/{conversation.id}')

    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(Notification, mine.id).read is True
    assert db.session.get(Notification, elsewhere.id).read is False
    assert db.session.get(Notification, also_elsewhere.id).read is False
    assert db.session.get(Notification, someone_elses.id).read is False
    assert db.session.get(User, alice.id).unread_notifications == 2


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

    The route therefore falls through to the form. Sending from it rejoins the
    existing conversation rather than creating a second one (D748, below).
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


def test_a_sender_who_has_left_gets_the_form_not_the_redirect(app, db_session):
    """The other half of the repaired guard at routes.py:97.

    find_existing_conversation looks the conversation up regardless of
    `joined`, while the id set comes from a query that filters on it. When the
    SENDER is the one who left, the set holds only the recipient -- so a mutant
    dropping `current_user.id in member_ids` would redirect alice back into a
    conversation she has left, and the original hands her the form.
    """
    instance, alice, bob, carol = _seed()
    _aged(alice)
    conversation = make_conversation(alice, bob)
    db.session.execute(db.text(
        "UPDATE conversation_member SET joined = :state WHERE user_id = :person_id "
        "AND conversation_id = :conversation_id"),
        {"state": False, "person_id": alice.id, "conversation_id": conversation.id})
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{bob.id}/new')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'chat/new_message.html'


@pytest.mark.parametrize('who_left', ['recipient', 'sender'])
def test_messaging_again_after_one_left_reuses_the_conversation(app, db_session, who_left):
    """D748, fixed. Once either member had left, sending from the new-message
    form created a SECOND conversation for the pair. It now reuses the existing
    one and rejoins whoever left, so there is one thread per pair and its
    history is kept (owner ruling 2026-09-30)."""
    instance, alice, bob, carol = _seed()
    _aged(alice)
    conversation = make_conversation(alice, bob)
    leaver = bob if who_left == 'recipient' else alice
    db.session.execute(db.text(
        "UPDATE conversation_member SET joined = :state WHERE user_id = :person_id "
        "AND conversation_id = :conversation_id"),
        {"state": False, "person_id": leaver.id, "conversation_id": conversation.id})
    db.session.commit()
    conversation_id = conversation.id
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with patch('app.chat.routes.render_template', return_value='rendered'), \
         patch('app.chat.util.publish_sse_event'):
        response = client.post(f'/chat/{bob.id}/new',
                               data={'message': 'hello again',
                                     'csrf_token': token, 'submit': 'Send'})

    assert response.status_code == 302
    assert response.headers['Location'] == f'/chat/{conversation_id}#message'
    assert [c.id for c in Conversation.query.all()] == [conversation_id]
    joined = db.session.execute(db.text(
        "SELECT user_id, joined FROM conversation_member WHERE conversation_id = :conversation_id"),
        {"conversation_id": conversation_id}).all()
    assert sorted(joined) == sorted([(alice.id, True), (bob.id, True)])
    assert [(m.conversation_id, m.body) for m in ChatMessage.query.all()] == \
        [(conversation_id, 'hello again')]


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
    assert db.session.get(ChatMessage, to_alice.id).read is True
    assert db.session.get(ChatMessage, from_alice.id).read is False


def test_an_admin_may_refresh_a_conversation_they_are_not_in(app, db_session):
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    client = app.test_client()
    login(client, carol)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/refresh-conversation/{conversation.id}')

    assert response.status_code == 200
    # a mutant dropping the is_admin half returns '' here, which is also a 200 --
    # the render is what separates the two
    assert render.call_args.args[0] == 'chat/_messages.html'
    assert response.get_data(as_text=True) == 'rendered'


def test_the_refresh_gives_a_stranger_an_empty_body(app, db_session):
    """routes.py:263 returns '' rather than falling off the end, which is what
    chat_options and chat_report do in the same position -- and those two are a
    500 for it (D747).
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


# --------------------------------------------------------------------------
# Group B, P2: ban_from_mod showed the VIEWER's ban history, to anyone
# --------------------------------------------------------------------------


def _moderator(user, community):
    from app.models import CommunityMember
    member = CommunityMember(user_id=user.id, community_id=community.id,
                             is_moderator=True)
    db.session.add(member)
    db.session.commit()
    return member


def _mod_log(community, user, action='ban_user', actor=None):
    from app.models import ModLog
    entry = ModLog(community_id=community.id, link='u/' + user.user_name,
                   action=action, user_id=(actor or user).id)
    db.session.add(entry)
    db.session.commit()
    return entry


def test_the_ban_view_shows_the_bans_of_the_user_the_url_names(app, db_session):
    """Before the repair the filter read `'u/' + current_user.user_name`, so
    the page showed the VIEWER's history under someone else's name:

        PROBE b2 status: 200
        PROBE b2 past_bans links: ['u/carol'] (url named bob, viewer is carol)

    Both rows exist here, so a repair that simply returned nothing would fail:
    bob's must appear and the moderator's own must not.
    """
    instance, alice, bob, carol = _seed()
    community = make_community('testcomm')
    _moderator(alice, community)
    _mod_log(community, bob, actor=alice)
    _mod_log(community, alice, actor=alice)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/ban_from_mod/{bob.id}/{community.id}')

    assert response.status_code == 200
    assert [entry.link for entry in render.call_args.kwargs['past_bans'].all()] == \
        ['u/' + bob.user_name]


def test_a_non_moderator_cannot_open_the_ban_view(app, db_session):
    """The route carried nothing but login_required, so carol -- who moderates
    nothing -- got a 200 on any community in the probe above.
    """
    instance, alice, bob, carol = _seed()
    community = make_community('testcomm')
    _mod_log(community, bob, actor=alice)
    client = app.test_client()
    login(client, carol)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/ban_from_mod/{bob.id}/{community.id}')

    assert response.status_code == 401


def test_an_admin_may_open_the_ban_view(app, db_session):
    """The other half of the gate: the moderator arm is not the only true arm,
    so a repair that admitted moderators alone would fail here.
    """
    instance, alice, bob, carol = _seed()
    community = make_community('testcomm')
    _mod_log(community, bob, actor=alice)
    _make_admin(carol)
    client = app.test_client()
    login(client, carol)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/ban_from_mod/{bob.id}/{community.id}')

    assert response.status_code == 200


# --------------------------------------------------------------------------
# Group B, P3: two routes that were a 500 instead of a refusal
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path', ['options', 'report'])
def test_a_stranger_is_refused_rather_than_crashing(app, db_session, path):
    """Before the repair both fell off the end returning None:

        PROBE p4 /chat/1/options exception: TypeError The view function for
        'chat.chat_options' did not return a valid response.

    400 is chat_home's refusal, so the blueprint now has one shape. See fact 306.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, carol)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/{conversation.id}/{path}')

    assert response.status_code == 400


@pytest.mark.parametrize('path', ['options', 'report'])
def test_a_member_still_gets_the_page(app, db_session, path):
    """The other half of P3's inversion: a repair that refused everyone would
    pass the test above.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{conversation.id}/{path}')

    assert response.status_code == 200
    assert render.call_args.kwargs['conversation'].id == conversation.id


# --------------------------------------------------------------------------
# Group B, P4: block_instance without htmx's optional current-url header
# --------------------------------------------------------------------------


def test_blocking_an_instance_without_a_current_url_still_answers(app, db_session):
    """HX-Current-Url is optional and htmx omits it when the page has no url to
    report. Before the repair:

        PROBE b1 exception: TypeError argument of type 'NoneType' is not iterable

    -- and by then the block had already been written, so the user's block was
    applied and the response was a 500. The block is asserted here as well as
    the redirect, since that is the part the crash was hiding.
    """
    from app.models import InstanceBlock
    instance, alice, bob, carol = _seed()
    remote = make_instance('remote.example', software='lemmy')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/chat/{remote.id}/block_instance',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == '/home'
    assert InstanceBlock.query.filter_by(user_id=alice.id,
                                         instance_id=remote.id).count() == 1


def test_blocking_an_instance_from_a_chat_page_sends_the_reader_home(app, db_session):
    instance, alice, bob, carol = _seed()
    remote = make_instance('remote.example', software='lemmy')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/chat/{remote.id}/block_instance',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': 'https://test.piefed.local/chat/1'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == '/home'


def test_blocking_an_instance_from_elsewhere_returns_the_reader_there(app, db_session):
    """The true arm of the same guard: a current url that is not a chat page
    comes back unchanged, which is what keeps the `/chat/` test load-bearing.
    """
    instance, alice, bob, carol = _seed()
    remote = make_instance('remote.example', software='lemmy')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/chat/{remote.id}/block_instance',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': 'https://test.piefed.local/u/bob'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == 'https://test.piefed.local/u/bob'


def test_blocking_an_instance_without_htmx_redirects_to_chat(app, db_session):
    instance, alice, bob, carol = _seed()
    remote = make_instance('remote.example', software='lemmy')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/chat/{remote.id}/block_instance',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert response.headers['Location'] == '/chat'


# --------------------------------------------------------------------------
# chat_report
# --------------------------------------------------------------------------


def _report_post(app, client, conversation_id, **overrides):
    data = {'reasons': ['7'], 'description': 'they will not stop',
            'submit': 'Report'}
    data.update(overrides)
    data['csrf_token'] = csrf(app, client)
    with patch('app.chat.routes.render_template', return_value='rendered'):
        return client.post(f'/chat/{conversation_id}/report', data=data)


def test_the_report_form_arrives_with_the_remote_box_ticked(app, db_session):
    """routes.py:250-251: the GET arm pre-ticks report_remote, which the POST
    arm must not do -- the two are different branches of the same `elif`.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{conversation.id}/report')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'chat/report.html'
    assert render.call_args.kwargs['form'].report_remote.data is True
    assert render.call_args.kwargs['conversation'].id == conversation.id


def test_reporting_a_conversation_writes_the_report_and_notifies_every_admin(app, db_session):
    """Two admins, so the loop at routes.py:233 runs more than once and each
    admin's own counter is visibly incremented -- with one admin, a mutant that
    incremented some other user's counter could still pass.
    """
    from app.constants import REPORT_TYPE_MESSAGE
    from app.models import Report
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    dave = make_user(instance, 'dave', local=True)
    _make_admin(dave)
    client = app.test_client()
    login(client, alice)

    response = _report_post(app, client, conversation.id)

    assert response.status_code == 302
    assert response.headers['Location'] == f'/chat/{conversation.id}'
    report = Report.query.one()
    assert report.reporter_id == alice.id
    assert report.suspect_conversation_id == conversation.id
    assert report.type == REPORT_TYPE_MESSAGE
    assert report.reasons == 'Spam'
    assert report.description == 'they will not stop'
    assert report.targets == {'gen': '0', 'suspect_conversation_id': conversation.id,
                              'reporter_id': alice.id}
    db.session.expire_all()
    notified = Notification.query.filter_by(subtype='chat_conversation_reported').all()
    # user 1 is an admin by id alone (app/request_hooks.py:100), so the seed's
    # burnt seat is notified along with the two roles
    assert sorted(n.user_id for n in notified) == sorted([1, carol.id, dave.id])
    assert all(n.url == '/admin/reports' and n.author_id == alice.id for n in notified)
    assert db.session.get(User, carol.id).unread_notifications == 1
    assert db.session.get(User, dave.id).unread_notifications == 1


def test_reporting_joins_the_reasons_in_the_order_they_were_submitted(app, db_session):
    """reasons_to_string (forms.py:33-39) loops over the SUBMITTED ids
    outermost and the form's choices innermost, so the stored string follows
    the submission's order, not the form's. Submitting them backwards is what
    tells the two apart -- and the first version of this test asserted the
    other way round and failed.
    """
    from app.models import Report
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, alice)

    _report_post(app, client, conversation.id, reasons=['14', '2'])

    assert Report.query.one().reasons == 'Other, Harassment'


def test_a_report_that_fails_validation_writes_nothing(app, db_session):
    """The false arm of validate_on_submit on a POST, which is a different arc
    from the GET at :250. description is capped at 256 characters (forms.py:29).
    """
    from app.models import Report
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, alice)

    data = {'reasons': ['7'], 'description': 'x' * 300, 'submit': 'Report',
            'csrf_token': csrf(app, client)}
    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.post(f'/chat/{conversation.id}/report', data=data)

    assert response.status_code == 200
    assert render.call_args.args[0] == 'chat/report.html'
    assert Report.query.count() == 0
    # the POST arm must NOT re-tick the box, which is the GET arm's job
    assert render.call_args.kwargs['form'].report_remote.data is False


def test_ticking_the_remote_box_changes_nothing_yet(app, db_session):
    """routes.py:245-246 is `if form.report_remote.data: ...` -- a branch whose
    body is a bare Ellipsis. The report is written either way, and that is what
    D761 records: the checkbox the form offers does nothing.
    """
    from app.models import Report
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    client = app.test_client()
    login(client, alice)

    _report_post(app, client, conversation.id, report_remote='y')

    assert Report.query.count() == 1
    assert Notification.query.filter_by(subtype='chat_conversation_reported').count() == 2


def test_an_admin_may_report_a_conversation_they_are_not_in(app, db_session):
    from app.models import Report
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    client = app.test_client()
    login(client, carol)

    response = _report_post(app, client, conversation.id)

    assert response.status_code == 302
    assert Report.query.one().reporter_id == carol.id


def test_reporting_an_unknown_conversation_is_a_404(app, db_session):
    instance, alice, bob, carol = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get('/chat/999/report')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# chat_options, chat_delete, chat_leave
# --------------------------------------------------------------------------


def _joined(conversation, user):
    row = db.session.execute(db.text(
        "SELECT joined FROM conversation_member WHERE user_id = :person_id "
        "AND conversation_id = :conversation_id"),
        {"person_id": user.id, "conversation_id": conversation.id}).first()
    return row[0] if row else None


def test_the_options_page_is_a_404_for_an_unknown_conversation(app, db_session):
    instance, alice, bob, carol = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get('/chat/999/options')

    assert response.status_code == 404


def test_an_admin_may_open_the_options_of_a_conversation_they_are_not_in(app, db_session):
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    client = app.test_client()
    login(client, carol)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{conversation.id}/options')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'chat/chat_options.html'


def test_deleting_a_conversation_takes_its_messages_and_its_reports(app, db_session):
    """routes.py:163 deletes the Report rows naming the conversation before
    deleting the conversation itself, so the fixture needs a report or that
    line proves nothing -- and the messages go with the cascade.
    """
    from app.constants import REPORT_TYPE_MESSAGE
    from app.models import Report
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _message(conversation, bob, alice, 'inside')
    report = Report(reasons='Spam', type=REPORT_TYPE_MESSAGE, reporter_id=alice.id,
                    suspect_conversation_id=conversation.id, source_instance_id=1)
    db.session.add(report)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    response = client.post(f'/chat/{conversation.id}/delete',
                           data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert response.headers['Location'] == '/chat'
    assert Conversation.query.count() == 0
    assert ChatMessage.query.count() == 0
    assert Report.query.count() == 0


def test_an_admin_may_delete_a_conversation_they_are_not_in(app, db_session):
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    client = app.test_client()
    login(client, carol)

    response = client.post(f'/chat/{conversation.id}/delete',
                           data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert Conversation.query.count() == 0


def test_a_stranger_deleting_a_conversation_is_a_silent_no_op(app, db_session):
    """D761: the stranger is redirected exactly like the member, with no flash
    and no deletion --

        PROBE b6 delete status: 302 conversation survives: True

    Recorded as behaviour rather than asserted as correct.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, carol)

    response = client.post(f'/chat/{conversation.id}/delete',
                           data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert Conversation.query.count() == 1


def test_deleting_an_unknown_conversation_is_a_404(app, db_session):
    instance, alice, bob, carol = _seed()
    client = app.test_client()
    login(client, alice)

    response = client.post('/chat/999/delete', data={'csrf_token': csrf(app, client)})

    assert response.status_code == 404


def test_leaving_a_conversation_flips_only_the_leavers_row(app, db_session):
    """The conversation survives because bob is still joined and local, which
    is delete_if_abandoned's keep_convo arm (app/models.py:221-238).
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, alice)

    response = client.post(f'/chat/{conversation.id}/leave',
                           data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert _joined(conversation, alice) is False
    assert _joined(conversation, bob) is True
    assert Conversation.query.count() == 1


def test_the_last_local_member_leaving_deletes_the_conversation(app, db_session):
    """delete_if_abandoned keeps a conversation only while a LOCAL member is
    still joined, so the other party here is remote: alice leaving takes the
    conversation with her.
    """
    instance, alice, bob, carol = _seed()
    remote_instance = make_instance('remote.example', software='lemmy')
    remote = make_user(remote_instance, 'remote', local=False)
    conversation = make_conversation(alice, remote)
    client = app.test_client()
    login(client, alice)

    response = client.post(f'/chat/{conversation.id}/leave',
                           data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert Conversation.query.count() == 0


def test_a_stranger_leaving_a_conversation_changes_nothing(app, db_session):
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    client = app.test_client()
    login(client, carol)

    response = client.post(f'/chat/{conversation.id}/leave',
                           data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert _joined(conversation, alice) is True
    assert _joined(conversation, bob) is True
    assert Conversation.query.count() == 1


def test_leaving_an_unknown_conversation_is_a_404(app, db_session):
    instance, alice, bob, carol = _seed()
    client = app.test_client()
    login(client, alice)

    response = client.post('/chat/999/leave', data={'csrf_token': csrf(app, client)})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# ban_from_mod's remaining arms, and the three template stubs
# --------------------------------------------------------------------------


def test_an_active_ban_hides_the_most_recent_log_entry(app, db_session):
    """routes.py:148-149: with an active ban the newest mod-log row IS that
    ban, so the list of PAST bans skips it with `offset(1)`. The two rows have
    to differ in age or the offset could drop either one.
    """
    from app.models import CommunityBan
    instance, alice, bob, carol = _seed()
    community = make_community('testcomm')
    _moderator(alice, community)
    older = _mod_log(community, bob, action='ban_user', actor=alice)
    newer = _mod_log(community, bob, action='unban_user', actor=alice)
    older.created_at = utcnow() - timedelta(days=2)
    newer.created_at = utcnow()
    db.session.add(CommunityBan(user_id=bob.id, community_id=community.id,
                                banned_by=alice.id))
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/ban_from_mod/{bob.id}/{community.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['active_ban'] is not None
    assert [entry.id for entry in render.call_args.kwargs['past_bans'].all()] == [older.id]


def test_without_an_active_ban_every_log_entry_is_shown(app, db_session):
    """The false arm of the same guard, and the row that keeps the `or_` at
    routes.py:146 load-bearing: one ban_user and one unban_user, both listed.
    """
    instance, alice, bob, carol = _seed()
    community = make_community('testcomm')
    _moderator(alice, community)
    banned = _mod_log(community, bob, action='ban_user', actor=alice)
    unbanned = _mod_log(community, bob, action='unban_user', actor=alice)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/ban_from_mod/{bob.id}/{community.id}')

    assert response.status_code == 200
    assert render.call_args.kwargs['active_ban'] is None
    assert sorted(entry.id for entry in render.call_args.kwargs['past_bans'].all()) == \
        sorted([banned.id, unbanned.id])


def test_the_ban_view_ignores_log_entries_of_other_actions(app, db_session):
    """The `or_` covers ban_user and unban_user and nothing else, so a
    delete_post entry against the same user must not appear.
    """
    instance, alice, bob, carol = _seed()
    community = make_community('testcomm')
    _moderator(alice, community)
    banned = _mod_log(community, bob, action='ban_user', actor=alice)
    _mod_log(community, bob, action='delete_post', actor=alice)
    db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        client.get(f'/chat/ban_from_mod/{bob.id}/{community.id}')

    assert [entry.id for entry in render.call_args.kwargs['past_bans'].all()] == [banned.id]


def test_the_ban_view_is_a_404_for_an_unknown_community(app, db_session):
    instance, alice, bob, carol = _seed()
    _make_admin(alice)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/ban_from_mod/{bob.id}/999')

    assert response.status_code == 404


def test_the_ban_view_is_a_404_for_an_unknown_user(app, db_session):
    instance, alice, bob, carol = _seed()
    community = make_community('testcomm')
    _moderator(alice, community)
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered'):
        response = client.get(f'/chat/ban_from_mod/999/{community.id}')

    assert response.status_code == 404


@pytest.mark.parametrize('path, template', [
    ('denied', 'chat/denied.html'),
    ('blocked', 'chat/blocked.html'),
    ('empty', 'chat/empty.html'),
])
def test_the_template_stubs_render(app, db_session, path, template):
    instance, alice, bob, carol = _seed()
    client = app.test_client()
    login(client, alice)

    with patch('app.chat.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/chat/{path}')

    assert response.status_code == 200
    assert render.call_args.args[0] == template


def test_the_report_loops_already_notified_set_can_never_be_true(app, db_session):
    """THE MODULE'S ONE RESIDUAL ARC, 234->233, PROVED UNREACHABLE.

    routes.py:232-241 reads

        already_notified = set()
        for admin in Site.admins():
            if admin.id not in already_notified:
                ...

    and nothing ever adds to `already_notified` -- the set is created, tested
    and abandoned. The false arm of that `if`, which is the arc that loops back
    to :233 without notifying, therefore requires an id the set already holds,
    and the set is empty on every iteration. D758's shape: a guard that cannot
    fire.

    Demonstrated rather than asserted by construction: three admins produce
    three notifications, one per admin and none skipped, which is what an
    always-true guard means. A duplicate admin id cannot be built -- the ids
    come from a UNION over the user table's primary key
    (app/request_hooks.py:100-105) -- so no fixture can reach the other arm.
    """
    instance, alice, bob, carol = _seed()
    conversation = make_conversation(alice, bob)
    _make_admin(carol)
    dave = make_user(instance, 'dave', local=True)
    _make_admin(dave)
    client = app.test_client()
    login(client, alice)

    _report_post(app, client, conversation.id)

    from app.models import Site
    with app.test_request_context():
        admin_ids = sorted(admin.id for admin in Site.admins())
    notified = Notification.query.filter_by(subtype='chat_conversation_reported').all()
    assert len(admin_ids) == len(set(admin_ids)) == 3
    assert sorted(n.user_id for n in notified) == admin_ids
