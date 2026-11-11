"""Each member's own state in a conversation (spec 2026-11-10-pixelfed-app-design §6, rulings 2026-11-11): a message
request until the recipient accepts or answers, unless they follow the sender; a hidden conversation, back in view
with the next message; a muted one, whose messages notify nobody who muted it, whatever sent them."""
import pytest

from app import db
from app.chat import membership
from app.chat.util import send_message
from app.models import Notification
from tests.factories import make_conversation, make_follow, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def people(app):
    local = make_instance('test.piefed.local', software='piefed')
    make_user(local, 'burnseat', local=True)   # user 1 is the site's
    sender, recipient = make_user(local, 'sends', local=True), make_user(local, 'receives', local=True)
    return sender, recipient


def _notifications(user):
    return Notification.query.filter_by(user_id=user.id, subtype='chat_message').count()


def _send(app, conversation, user, body='hello'):
    with app.test_request_context():
        return send_message(body, conversation.id, user=user)


def test_a_first_message_from_someone_not_followed_is_a_request(app, people):
    sender, recipient = people
    conversation = membership.start_conversation(sender, recipient)
    assert membership.member(conversation.id, sender.id).state == membership.ACTIVE
    assert membership.member(conversation.id, recipient.id).state == membership.REQUEST
    _send(app, conversation, sender)
    assert _notifications(recipient) == 0   # a request notifies nobody (Pixelfed's own behaviour)


def test_one_from_someone_followed_is_not(app, people):
    sender, recipient = people
    make_follow(recipient, sender)
    conversation = membership.start_conversation(sender, recipient)
    assert membership.member(conversation.id, recipient.id).state == membership.ACTIVE
    _send(app, conversation, sender)
    assert _notifications(recipient) == 1


def test_a_pending_follow_is_not_a_follow(app, people):
    sender, recipient = people
    make_follow(recipient, sender, is_accepted=None)
    conversation = membership.start_conversation(sender, recipient)
    assert membership.member(conversation.id, recipient.id).state == membership.REQUEST


def test_answering_a_request_accepts_it(app, people):
    sender, recipient = people
    conversation = membership.start_conversation(sender, recipient)
    _send(app, conversation, sender)
    _send(app, conversation, recipient, 'hi back')
    assert membership.member(conversation.id, recipient.id).state == membership.ACTIVE
    _send(app, conversation, sender, 'and again')
    assert _notifications(recipient) == 1   # from now on its messages notify


def test_accepting_makes_it_active(app, people):
    sender, recipient = people
    conversation = membership.start_conversation(sender, recipient)
    membership.update_member(conversation.id, recipient.id, state=membership.ACTIVE)
    assert membership.member(conversation.id, recipient.id).state == membership.ACTIVE


def test_a_muted_conversation_notifies_nobody_who_muted_it(app, people):
    sender, recipient = people
    make_follow(recipient, sender)
    conversation = membership.start_conversation(sender, recipient)
    membership.update_member(conversation.id, recipient.id, muted=True)
    _send(app, conversation, sender)
    assert _notifications(recipient) == 0
    membership.update_member(conversation.id, recipient.id, muted=False)
    _send(app, conversation, sender, 'once more')
    assert _notifications(recipient) == 1


def test_a_message_brings_a_hidden_conversation_back_for_both(app, people):
    sender, recipient = people
    conversation = membership.start_conversation(sender, recipient)
    membership.update_member(conversation.id, sender.id, hidden=True)
    membership.update_member(conversation.id, recipient.id, hidden=True)
    _send(app, conversation, sender)
    assert not membership.member(conversation.id, sender.id).hidden
    assert not membership.member(conversation.id, recipient.id).hidden


def test_conversations_made_elsewhere_start_active_and_shown(app, people):
    """Moderation notices, admin contact and the bot challenge make their conversations as before: active, so they
    notify (the column defaults)."""
    sender, recipient = people
    conversation = make_conversation(sender, recipient)
    state = membership.member(conversation.id, recipient.id)
    assert (state.state, state.hidden, state.muted, state.joined) == (membership.ACTIVE, False, False, True)


def test_no_membership_is_none(app, people):
    sender, recipient = people
    conversation = membership.start_conversation(sender, recipient)
    stranger = make_user(None, 'stranger', local=True)
    assert membership.member(conversation.id, stranger.id) is None


def test_a_remote_recipient_is_not_held_as_a_request(app, people):
    """A remote member's state is its own server's business: only a local recipient's is kept."""
    sender, _recipient = people
    remote = make_user(make_instance('far.example'), 'faraway')
    conversation = membership.start_conversation(sender, remote)
    assert membership.member(conversation.id, remote.id).state == membership.ACTIVE
