from flask import flash, json
from flask_babel import _
from flask_login import current_user

from app import db
from app.activitypub.signature import send_post_request
from app.constants import NOTIF_MESSAGE, SRC_WEB
from app.federation_urls import activity_id, owner_base
from app.models import User, ChatMessage, Notification, utcnow, Conversation
from app.storefront import canonicalise_text
from app.utils import shorten_string, markdown_to_html, publish_sse_event


def send_message(message: str, conversation_id: int, user: User = current_user, src=SRC_WEB) -> ChatMessage:
    conversation = db.session.get(Conversation, conversation_id)
    message = canonicalise_text(message)   # stored and federated: a link to this site names its canonical host
    reply = ChatMessage(sender_id=user.id, conversation_id=conversation.id,
                        body=message, body_html=markdown_to_html(message))
    conversation.updated_at = utcnow()
    db.session.add(reply)
    db.session.commit()
    for recipient in conversation.members:
        if recipient.id != user.id:
            reply.recipient_id = recipient.id
            reply.ap_id = f"{owner_base(reply)}/private_message/{reply.id}"   # the sender's domain (plan C3)
            db.session.commit()
            if recipient.is_local():
                publish_sse_event(f"messages:{recipient.id}", json.dumps({'conversation': conversation.id}))

                # Notify local recipient
                targets_data = {'gen': '0', 'conversation_id': conversation.id, 'message_id': reply.id}
                notify = Notification(title=shorten_string('New message from ' + user.display_name()),
                                      url=f'/chat/{conversation_id}#message_{reply.id}',
                                      user_id=recipient.id,
                                      author_id=user.id,
                                      notif_type=NOTIF_MESSAGE,
                                      subtype='chat_message',
                                      targets=targets_data)
                db.session.add(notify)
                recipient.unread_notifications += 1
                db.session.commit()
            else:
                if recipient.instance.software == "lemmy" or recipient.instance.software == "mbin":
                    ap_type = "ChatMessage"
                else:
                    ap_type = "Note"
                # Federate reply
                reply_json = {
                    "actor": user.public_url(),
                    "id": activity_id('create', user),
                    "object": {
                        "attributedTo": user.public_url(),
                        "content": reply.body_html,
                        "id": reply.ap_id,
                        "inReplyTo": conversation.last_ap_id(recipient.id),
                        "mediaType": "text/html",
                        "published": utcnow().isoformat() + 'Z',  # Lemmy is inconsistent with the date format they use
                        "to": [
                            recipient.public_url()
                        ],
                        "type": ap_type
                    },
                    "to": [
                        recipient.public_url()
                    ],
                    "type": "Create"
                }
                if recipient.instance.software != "lemmy" and recipient.instance.software != "piefed":
                    reply_json['object']['tag'] = [
                        {
                            "href": recipient.public_url(),
                            "name": recipient.mention_tag(),
                            "type": "Mention"
                        }
                    ]
                send_post_request(recipient.ap_inbox_url, reply_json, user.private_key,
                                  user.public_url() + '#main-key')

    return reply


def chat_message_ap_object(reply: ChatMessage, recipient: User) -> dict:
    """The ActivityPub object a DM is federated as in an Update: what update_message pushes and what
    /private_message/<id> serves (S3.5), so a fetched message and an edited one cannot differ. send_message keeps
    its own builder for the original Create."""
    ap_type = "ChatMessage" if recipient.instance.software in ("lemmy", "mbin") else "Note"
    obj = {
        "attributedTo": reply.sender.public_url(),
        "content": reply.body_html,
        "id": reply.ap_id,
        "mediaType": "text/html",
        "published": reply.created_at.isoformat() + 'Z',
        "to": [recipient.public_url()],
        "type": ap_type
    }
    if recipient.instance.software != "lemmy" and recipient.instance.software != "piefed":
        obj['tag'] = [
            {
                "href": recipient.public_url(),
                "name": recipient.mention_tag(),
                "type": "Mention"
            }
        ]
    return obj


def update_message(reply: ChatMessage):
    user = reply.sender
    recipient = db.session.get(User, reply.recipient_id) if reply.recipient_id else None
    if recipient is None:  # D761: recipient_id is nullable; nobody to tell
        return
    conversation_id = reply.conversation_id

    if recipient.is_local():
        # Notify local recipient
        targets_data = {'gen': '0', 'conversation_id': conversation_id, 'message_id': reply.id}
        notify = Notification(title=shorten_string('Updated message from ' + user.display_name()),
                              url=f'/chat/{conversation_id}#message_{reply.id}',
                              user_id=recipient.id,
                              author_id=user.id,
                              notif_type=NOTIF_MESSAGE,
                              subtype='chat_message',
                              targets=targets_data)
        db.session.add(notify)
        recipient.unread_notifications += 1
        db.session.commit()
    else:
        # Federate reply
        reply_json = {
            "actor": user.public_url(),
            "id": activity_id('update', user),
            "object": {**chat_message_ap_object(reply, recipient), "updated": reply.edited_at.isoformat() + 'Z'},
            "to": [recipient.public_url()],
            # an edit is an Update, as app/shared/tasks/notes.py:187 and
            # app/shared/tasks/pages.py:252 both have it
            "type": "Update"
        }
        send_post_request(recipient.ap_inbox_url, reply_json, user.private_key,
                                  user.public_url() + '#main-key')





