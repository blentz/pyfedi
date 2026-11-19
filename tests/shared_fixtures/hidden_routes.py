"""Fixtures from tests/test_hidden_routes.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import ChatMessage, Conversation, utcnow


@pytest.fixture
def talking(onion_hidden_on_clearnet_onion):
    """The first domain's bob talks with the onion's bob (from the onion: nowhere else can he be seen) and with the first
    domain's admin."""
    onion_hidden_on_clearnet_onion.with_onion = Conversation(user_id=onion_hidden_on_clearnet_onion.first_bob.id)
    onion_hidden_on_clearnet_onion.with_onion.members.extend([onion_hidden_on_clearnet_onion.first_bob, onion_hidden_on_clearnet_onion.onion_bob])
    onion_hidden_on_clearnet_onion.with_admin = Conversation(user_id=onion_hidden_on_clearnet_onion.first_bob.id)
    onion_hidden_on_clearnet_onion.with_admin.members.extend([onion_hidden_on_clearnet_onion.first_bob, onion_hidden_on_clearnet_onion.admin])
    db.session.add_all([onion_hidden_on_clearnet_onion.with_onion, onion_hidden_on_clearnet_onion.with_admin])
    db.session.commit()
    for conversation, sender, recipient, text in ((onion_hidden_on_clearnet_onion.with_onion, onion_hidden_on_clearnet_onion.onion_bob, onion_hidden_on_clearnet_onion.first_bob, 'ONIONCHAT'),
                                                  (onion_hidden_on_clearnet_onion.with_admin, onion_hidden_on_clearnet_onion.admin, onion_hidden_on_clearnet_onion.first_bob, 'FIRSTCHAT')):
        db.session.add(ChatMessage(conversation_id=conversation.id, sender_id=sender.id, recipient_id=recipient.id,
                                   body=text, body_html=f'<p>{text}</p>'))
    onion_hidden_on_clearnet_onion.with_onion.updated_at = utcnow()   # the newest: /chat opens it first
    db.session.commit()
    return onion_hidden_on_clearnet_onion
