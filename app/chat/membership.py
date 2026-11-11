"""Each member's own state in a conversation (spec 2026-11-10-pixelfed-app-design §6, rulings 2026-11-11), kept on
conversation_member beside `joined`:

- `state`: 'active', or 'request' for a local recipient who does not follow the sender of a conversation's first
  message, until they accept it or answer. A request notifies nobody (as Pixelfed's own requests).
- `hidden`: filed away by the person (Pixelfed's Hidden tab); any message in the conversation brings it back.
- `muted`: the conversation's messages notify nobody who muted it, whatever sent them.

Conversations made by moderation, admin contact and the bot challenge are made as before and keep the columns'
defaults: active, shown, unmuted."""
from sqlalchemy import and_, select, update

from app import db
from app.models import Conversation, User, conversation_member

ACTIVE, REQUEST = 'active', 'request'


def member(conversation_id: int, user_id: int):
    """`user_id`'s membership of `conversation_id` (state, hidden, muted, joined), or None for no member."""
    return db.session.execute(
        select(conversation_member.c.state, conversation_member.c.hidden, conversation_member.c.muted,
               conversation_member.c.joined)
        .where(conversation_member.c.conversation_id == conversation_id,
               conversation_member.c.user_id == user_id)).first()


def update_member(conversation_id: int, user_id: int, **values) -> None:
    db.session.execute(update(conversation_member).where(and_(
        conversation_member.c.conversation_id == conversation_id, conversation_member.c.user_id == user_id))
        .values(**values))
    db.session.commit()


def start_conversation(sender: User, recipient: User) -> Conversation:
    """A conversation a person starts with another (the web's new message, alpha's and the apps' APIs, and one
    arriving over ActivityPub): a request for a local recipient who does not follow the sender."""
    conversation = Conversation(user_id=sender.id)
    conversation.members.append(recipient)
    conversation.members.append(sender)
    db.session.add(conversation)
    db.session.commit()
    if recipient.is_local() and recipient.is_following(sender) != 'following':
        update_member(conversation.id, recipient.id, state=REQUEST)
    return conversation


def message_sent(conversation_id: int, sender_id: int) -> None:
    """A message in the conversation: its sender has answered (a request of theirs is accepted), and it is back in
    view for everyone who had hidden it."""
    db.session.execute(update(conversation_member)
                       .where(conversation_member.c.conversation_id == conversation_id)
                       .values(hidden=False))
    db.session.execute(update(conversation_member).where(and_(
        conversation_member.c.conversation_id == conversation_id, conversation_member.c.user_id == sender_id))
        .values(state=ACTIVE))
    db.session.commit()


def notifies(conversation_id: int, user_id: int) -> bool:
    """Whether a message in the conversation notifies `user_id`: not while it is a request of theirs, nor muted."""
    found = member(conversation_id, user_id)
    return found is None or (found.state == ACTIVE and not found.muted)
