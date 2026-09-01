"""tests/test_factories_conversation.py"""
from app.models import Conversation
from tests.factories import make_conversation, make_instance, make_user


def test_make_conversation_is_findable_by_the_lookup_process_chat_uses(app, db_session):
    """`Conversation.find_existing_conversation` (app/models.py:254) joins
    `conversation_member` TWICE and requires a row for each party, so a
    conversation built without both members is invisible to the very lookup
    `process_chat` performs. This test asserts through that staticmethod rather
    than through `.members`, because being findable is the only property the
    dispatcher cares about.

    It is also asserted symmetrically: the staticmethod takes (recipient,
    sender) in that order, and a factory that only registered one direction
    would pass one call and fail the other.
    """
    instance = make_instance('peer.example')
    sender = make_user(instance, 'sender')
    recipient = make_user(None, 'recipient', local=True)

    conversation = make_conversation(sender, recipient)

    assert conversation.id is not None
    assert conversation.user_id == sender.id
    assert {m.id for m in conversation.members} == {sender.id, recipient.id}
    assert Conversation.find_existing_conversation(
        recipient=recipient, sender=sender).id == conversation.id
    assert Conversation.find_existing_conversation(
        recipient=sender, sender=recipient).id == conversation.id
