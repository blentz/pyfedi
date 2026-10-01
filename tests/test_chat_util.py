"""app/chat/util.py's send_message, remote arm.

MEASUREMENT BASIS. The local arm is covered incidentally by the private
message tests; before this file existed the REMOTE arm carried 7 missing
statements and 5 missing arcs on the full-suite --cov=app run at f694a7dd4
(util.py:40-45, :65-66, :73 and the 24->40 arc), and app/chat/util.py stood at
49.3%.

The arm has two independent guards over `recipient.instance.software`:

    :40  lemmy or mbin      -> ap_type ChatMessage, anything else Note
    :65  not lemmy and not piefed -> the object carries a Mention tag

Four softwares are what it takes to make both operands of each guard
load-bearing: lemmy (ChatMessage, no tag), mbin (ChatMessage, tag), piefed
(Note, no tag), mastodon (Note, tag). Two rows would leave one operand of each
guard untested.

send_post_request is imported into app.chat.util, so it is patched THERE.
"""
import pytest
from unittest.mock import patch

from app import db
from app.chat.util import send_message, update_message
from app.models import ChatMessage, Notification, Site
from tests.factories import make_conversation, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def _seed(software):
    """A local sender with a private key and a remote recipient on `software`.

    The sender needs a private_key because the delivery call passes it
    (util.py:73); make_user leaves it None unless asked, and a string is enough
    here because send_post_request is patched and never signs.
    """
    local = make_instance('test.piefed.local', software='piefed')
    burn = make_user(local, 'burnseat', local=True)
    assert burn.id == 1
    sender = make_user(local, 'sender', local=True)
    sender.private_key = 'a-private-key'
    remote_instance = make_instance('remote.example', software=software)
    recipient = make_user(remote_instance, 'recipient', local=False)
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    return sender, recipient


def _send(app, sender, recipient, body='hello over there'):
    conversation = make_conversation(sender, recipient)
    with app.test_request_context():
        with patch('app.chat.util.send_post_request') as delivery:
            reply = send_message(body, conversation.id, user=sender)
    return conversation, reply, delivery


@pytest.mark.parametrize('software, ap_type, tagged', [
    ('lemmy', 'ChatMessage', False),
    ('mbin', 'ChatMessage', True),
    ('piefed', 'Note', False),
    ('mastodon', 'Note', True),
])
def test_the_remote_object_matches_the_recipients_software(app, db_session, software,
                                                           ap_type, tagged):
    sender, recipient = _seed(software)

    conversation, reply, delivery = _send(app, sender, recipient)

    payload = delivery.call_args.args[1]
    assert payload['object']['type'] == ap_type
    assert ('tag' in payload['object']) is tagged
    if tagged:
        assert payload['object']['tag'] == [{
            'href': recipient.public_url(),
            'name': recipient.mention_tag(),
            'type': 'Mention',
        }]


def test_the_remote_message_is_delivered_to_the_recipients_inbox(app, db_session):
    """The delivery arguments, which are what make the call a federation and
    not merely a dict: the recipient's inbox, the SENDER's private key, and the
    sender's public url with #main-key as the key id.
    """
    sender, recipient = _seed('piefed')

    conversation, reply, delivery = _send(app, sender, recipient)

    inbox, payload, private_key, key_id = delivery.call_args.args
    assert inbox == recipient.ap_inbox_url
    assert private_key == 'a-private-key'
    assert key_id == sender.public_url() + '#main-key'
    assert payload['type'] == 'Create'
    assert payload['actor'] == sender.public_url()
    assert payload['to'] == [recipient.public_url()]
    assert payload['object']['attributedTo'] == sender.public_url()
    assert payload['object']['id'] == reply.ap_id
    assert payload['object']['content'] == reply.body_html


def test_the_remote_message_threads_onto_the_senders_last_message(app, db_session):
    """`inReplyTo` comes from conversation.last_ap_id(recipient.id)
    (app/models.py:241-248), which walks the conversation's messages FROM THAT
    SENDER and returns the first one carrying an ap_id.

    An unseeded conversation gives '', so the earlier message is what makes the
    lookup load-bearing -- and it is seeded from the RECIPIENT, since that is
    whose id the call passes.
    """
    sender, recipient = _seed('piefed')
    conversation = make_conversation(sender, recipient)
    earlier = ChatMessage(sender_id=recipient.id, recipient_id=sender.id,
                          conversation_id=conversation.id, body='earlier',
                          body_html='<p>earlier</p>',
                          ap_id='https://remote.example/private_message/7')
    db.session.add(earlier)
    db.session.commit()

    with app.test_request_context():
        with patch('app.chat.util.send_post_request') as delivery:
            send_message('a reply', conversation.id, user=sender)

    payload = delivery.call_args.args[1]
    assert payload['object']['inReplyTo'] == 'https://remote.example/private_message/7'


def test_a_conversation_with_no_earlier_message_threads_onto_nothing(app, db_session):
    """The other end of last_ap_id: no message from that sender means '', and
    the object still goes out. Without this row the `return ''` at
    app/models.py:247 would be untested from here.
    """
    sender, recipient = _seed('piefed')

    conversation, reply, delivery = _send(app, sender, recipient)

    assert delivery.call_args.args[1]['object']['inReplyTo'] == ''


def test_the_remote_message_is_stored_and_addressed(app, db_session):
    """The row the arm writes, which is the same row the local arm writes: the
    recipient is filled in, and the ap_id is minted under this server's own
    SERVER_URL even though the message is bound elsewhere.
    """
    sender, recipient = _seed('piefed')

    conversation, reply, delivery = _send(app, sender, recipient)

    stored = ChatMessage.query.one()
    assert stored.id == reply.id
    assert stored.sender_id == sender.id
    assert stored.recipient_id == recipient.id
    assert stored.body == 'hello over there'
    assert stored.ap_id.endswith(f'/private_message/{stored.id}')
    assert stored.conversation_id == conversation.id


# --------------------------------------------------------------------------
# P1: an edited message federated as a NEW message
# --------------------------------------------------------------------------


def _edited(sender, recipient, conversation, ap_id='https://test.piefed.local/private_message/1'):
    from app.models import utcnow
    reply = ChatMessage(sender_id=sender.id, recipient_id=recipient.id,
                        conversation_id=conversation.id, body='edited',
                        body_html='<p>edited</p>', ap_id=ap_id,
                        edited_at=utcnow())
    db.session.add(reply)
    db.session.commit()
    return reply


def test_an_edited_message_federates_as_an_update(app, db_session):
    """Before the repair the wrapper said Create while the activity id said
    update, so a peer saw a second message rather than an edit:

        PROBE b3 activity id: https://test.piefed.local/activities/update/odDGXrwdMmJXBH4
        PROBE b3 activity type: Create
        PROBE b3 object type: Note

    app/shared/tasks/notes.py:187 and app/shared/tasks/pages.py:252 both read
    `type = 'Create' if not edit else 'Update'`, and update_message is only ever
    called on an edit (app/api/alpha/utils/private_message.py:193).

    The inner type is asserted too: changing both would be a different bug.
    """
    from app.chat.util import update_message
    sender, recipient = _seed('piefed')
    conversation = make_conversation(sender, recipient)
    reply = _edited(sender, recipient, conversation)

    with app.test_request_context():
        with patch('app.chat.util.send_post_request') as delivery:
            update_message(reply)

    payload = delivery.call_args.args[1]
    assert payload['type'] == 'Update'
    assert payload['object']['type'] == 'Note'
    assert '/activities/update/' in payload['id']


def _local_pair():
    """A local sender and a LOCAL recipient, for update_message's other arm."""
    local = make_instance('test.piefed.local', software='piefed')
    burn = make_user(local, 'burnseat', local=True)
    assert burn.id == 1
    sender = make_user(local, 'sender', local=True)
    sender.private_key = 'a-private-key'
    recipient = make_user(local, 'recipient', local=True)
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    return sender, recipient


def test_editing_a_message_with_no_recipient_notifies_nobody(app, db_session):
    """D761, fixed: `recipient_id` is nullable and update_message looked the
    recipient up with .one(), so editing such a message raised NoResultFound.
    With nobody to tell it now tells nobody."""
    sender, recipient = _local_pair()
    conversation = make_conversation(sender, recipient)
    reply = _edited(sender, recipient, conversation)
    reply.recipient_id = None
    db.session.commit()

    with app.test_request_context():
        with patch('app.chat.util.send_post_request') as sent:
            update_message(reply)

    assert Notification.query.count() == 0
    assert sent.call_count == 0


def test_editing_a_message_notifies_a_local_recipient(app, db_session):
    """util.py:84-96. The notification's title says Updated rather than New,
    which is the only thing separating this arm from send_message's, and the
    recipient's badge is incremented.
    """
    from app.chat.util import update_message
    from app.models import Notification, User
    sender, recipient = _local_pair()
    conversation = make_conversation(sender, recipient)
    reply = _edited(sender, recipient, conversation)

    with app.test_request_context():
        update_message(reply)

    notification = Notification.query.one()
    assert notification.user_id == recipient.id
    assert notification.author_id == sender.id
    assert notification.title.startswith('Updated message from ')
    assert notification.url == f'/chat/{conversation.id}#message_{reply.id}'
    assert notification.targets == {'gen': '0', 'conversation_id': conversation.id,
                                    'message_id': reply.id}
    assert db.session.get(User, recipient.id).unread_notifications == 1


@pytest.mark.parametrize('software, ap_type, tagged', [
    ('lemmy', 'ChatMessage', False),
    ('mbin', 'ChatMessage', True),
    ('piefed', 'Note', False),
    ('mastodon', 'Note', True),
])
def test_the_edited_object_matches_the_recipients_software(app, db_session, software,
                                                           ap_type, tagged):
    """The same two guards send_message carries (util.py:98, :119), repeated in
    update_message rather than shared -- so they need their own four rows.
    """
    from app.chat.util import update_message
    sender, recipient = _seed(software)
    conversation = make_conversation(sender, recipient)
    reply = _edited(sender, recipient, conversation)

    with app.test_request_context():
        with patch('app.chat.util.send_post_request') as delivery:
            update_message(reply)

    payload = delivery.call_args.args[1]
    assert payload['object']['type'] == ap_type
    assert ('tag' in payload['object']) is tagged
    if tagged:
        assert payload['object']['tag'][0]['href'] == recipient.public_url()


def test_the_edit_carries_both_timestamps_and_the_senders_key(app, db_session):
    """`published` is the message's ORIGINAL created_at and `updated` is its
    edited_at (util.py:110-111), so a fixture whose two timestamps are equal
    could not tell a mutant swapping them apart.
    """
    from app.chat.util import update_message
    from app.models import utcnow
    from datetime import timedelta
    sender, recipient = _seed('piefed')
    conversation = make_conversation(sender, recipient)
    reply = _edited(sender, recipient, conversation)
    reply.created_at = utcnow() - timedelta(days=3)
    db.session.commit()

    with app.test_request_context():
        with patch('app.chat.util.send_post_request') as delivery:
            update_message(reply)

    inbox, payload, private_key, key_id = delivery.call_args.args
    assert inbox == recipient.ap_inbox_url
    assert private_key == 'a-private-key'
    assert key_id == sender.public_url() + '#main-key'
    assert payload['object']['published'] == reply.created_at.isoformat() + 'Z'
    assert payload['object']['updated'] == reply.edited_at.isoformat() + 'Z'
    assert payload['object']['id'] == reply.ap_id
    assert payload['object']['content'] == reply.body_html
    assert payload['actor'] == sender.public_url()
