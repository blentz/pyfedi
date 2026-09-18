"""app/chat/routes.py's conversation surface: chat_home, new_message and
chat_conversation.

MEASUREMENT BASIS. Before this file existed the three carried 60 missing
statements and 34 missing arcs on the full-suite --cov=app run at f694a7dd4,
and app/chat/routes.py stood at 22.3%.

Three defects are pinned here and repaired together, all three in chat_home
and new_message:

  P1  chat_home's POST path ran before any membership check, so a verified
      non-member could write a message into any conversation by id. The GET
      path has checked since before the campaign started (routes.py:41).
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
from app.models import ChatMessage, Conversation, Site, User, utcnow
from tests.factories import make_conversation, make_instance, make_user

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
    her own standing at :23. The message was committed, given an ap_id under
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

    routes.py:41 lets an admin read a foreign conversation, which is a
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
