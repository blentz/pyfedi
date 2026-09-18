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
from app.chat.util import send_message
from app.models import ChatMessage, Site
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
    site = Site.query.get(1)
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
