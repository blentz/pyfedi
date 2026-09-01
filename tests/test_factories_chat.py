"""tests/test_factories_chat.py"""
from tests.factories import make_chat_message, make_instance, make_user


def test_make_chat_message_builds_a_row_reachable_by_ap_id_and_sender(app, db_session):
    """Undo/Delete's PM-restore branch queries ChatMessage by ap_id AND
    sender_id together, so both must be set for the factory to be useful.
    `deleted` is asserted at an explicitly-passed True rather than its column
    default, so this proves the parameter works rather than restating the
    default.
    """
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    recipient = make_user(None, 'recipient', local=True)

    message = make_chat_message(sender, recipient, 'https://peer.example/pm/1', deleted=True)

    assert message.id is not None
    assert message.sender_id == sender.id
    assert message.recipient_id == recipient.id
    assert message.ap_id == 'https://peer.example/pm/1'
    assert message.deleted is True
    assert message.conversation_id is None
