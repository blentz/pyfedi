"""Pixelfed's direct messages for its official app (plan W1.4b1; spec 2026-11-10-pixelfed-app-design §6, rulings
2026-11-11), in its server's shapes (DirectConversationController, DirectMessagePayloadService): a conversation is
PieFed's Conversation, a message its ChatMessage, and every message is sent through the web chat's own send
(app.chat.util.send_message), so it is notified, federated and shown on the web as one sent there. Each member's
state (a request, hidden, muted) is app.chat.membership's. One-to-one: a group is refused, as /api/v2.1/config says.
Photos in messages (W1.4b2) are the sender's uploads, through /api/v2/media."""
from flask import g, jsonify, request
from sqlalchemy import and_, func, select

from app import db
from app.chat import membership
from app.chat.util import MAX_MESSAGE_PICTURES, send_message
from app.constants import NOTIF_MESSAGE, SRC_API
from app.local_only import found_here, refuses_remote_contact, without_hidden_conversations, \
    without_local_only_actors
from app.mastodon import bp
from app.mastodon.entities import account, timestamp
from app.mastodon.instance import MAX_CHARACTERS
from app.mastodon.media import own_uploads
from app.mastodon.statuses import attachment
from app.models import ChatMessage, Conversation, Notification, User, UserBlock, UserFollower, conversation_member
from app.oauth_server.protect import require_oauth
from app.reads import users as reads_users
from app.shared.tasks import task_selector

FILTERS = ('primary', 'requests', 'hidden')
MAX_CONVERSATIONS, MAX_MESSAGES = 40, 50
LARGEST_ID = 2 ** 31 - 1


def _refused(reason: str, status: int = 422):
    return jsonify({'error': reason}), status


def whole_number(value, low: int = 1, high: int = LARGEST_ID) -> int | None:
    """`value` as a whole number in [low, high], else None: an id or a count from a query string or a JSON body."""
    text = str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else ''
    return int(text) if text.isdigit() and len(text) <= 10 and low <= int(text) <= high else None


def _membership(conversation_id):
    """The viewer's conversation `conversation_id` and their membership of it, or None: for one they are not in, have
    left, nobody holds, or is hidden here alike (follow-up F6)."""
    number = whole_number(conversation_id)
    conversation = found_here(Conversation, number) if number else None
    found = membership.member(conversation.id, g.oauth_user.id) if conversation else None
    return (conversation, found) if found is not None and found.joined else None


def _not_found():
    return _refused('Record not found', 404)


def _messages(conversation: Conversation):
    return ChatMessage.query.filter(ChatMessage.conversation_id == conversation.id, ChatMessage.deleted.is_(False),
                                    ChatMessage.recipient_id.isnot(None))


def _unread(conversation: Conversation, viewer_id: int):
    return _messages(conversation).filter(ChatMessage.recipient_id == viewer_id, ChatMessage.read.is_(False))


def _last_read_id(conversation: Conversation, viewer_id: int) -> str | None:
    newest = db.session.query(func.max(ChatMessage.id)).filter(
        ChatMessage.conversation_id == conversation.id, ChatMessage.recipient_id == viewer_id,
        ChatMessage.read.is_(True)).scalar()
    return str(newest) if newest else None


def message_entity(message: ChatMessage, viewer_id: int) -> dict:
    pictures = [attachment(file) for file in message.files if file.view_url()]
    kind = 'photos' if len(pictures) > 1 else 'photo' if pictures else 'text'   # Pixelfed's message types
    return {'id': str(message.id), 'conversation_id': str(message.conversation_id),
            'sender_id': str(message.sender_id), 'is_author': message.sender_id == viewer_id, 'type': kind,
            'text': message.body or '', 'media': pictures, 'meta': None, 'sensitive': False, 'in_reply_to_id': None,
            # edited_at moves on any change of the row (its read flag's too): an edit cannot be told from a reading
            'edited_at': None, 'created_at': timestamp(message.created_at)}


def _state(conversation: Conversation, found) -> dict:
    viewer_id = g.oauth_user.id
    return {'id': str(conversation.id), 'state': found.state if found.joined else 'left',
            'unread_count': _unread(conversation, viewer_id).count(), 'muted': found.muted, 'hidden': found.hidden,
            'last_read_message_id': _last_read_id(conversation, viewer_id)}


def conversation_entity(conversation: Conversation, found) -> dict:
    viewer_id = g.oauth_user.id
    others = [person for person in conversation.members if person.id != viewer_id]
    last = _messages(conversation).order_by(ChatMessage.id.desc()).first()
    entity = _state(conversation, found)
    entity.update({'type': 'direct', 'name': None, 'participants': [account(person, viewer_id) for person in others],
                   'participant_count': len(conversation.members),
                   'last_message': message_entity(last, viewer_id) if last else None,
                   'created_at': timestamp(conversation.created_at),
                   'updated_at': timestamp(conversation.updated_at or conversation.created_at)})
    return entity


def _inbox(viewer_id: int, kind: str):
    """The viewer's conversations in one of the app's tabs, newest first: those they are in and that hold a
    message (as Pixelfed lists them), without any whose member is hidden here."""
    member = conversation_member.c
    query = Conversation.query.join(conversation_member, and_(member.conversation_id == Conversation.id,
                                                              member.user_id == viewer_id)) \
        .filter(member.joined.is_(True),
                select(ChatMessage.id).where(ChatMessage.conversation_id == Conversation.id,
                                             ChatMessage.deleted.is_(False)).exists())
    if kind == 'requests':
        query = query.filter(member.state == membership.REQUEST, member.hidden.is_(False))
    elif kind == 'hidden':
        query = query.filter(member.hidden.is_(True))
    else:
        query = query.filter(member.state == membership.ACTIVE, member.hidden.is_(False))
    return without_hidden_conversations(query, Conversation.id) \
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())


@bp.route('/api/v1.1/direct/conversations')
@require_oauth('read')
def direct_conversations():
    kind = request.args.get('filter', 'primary')
    limit = whole_number(request.args.get('limit', 20), high=MAX_CONVERSATIONS)
    raw_cursor = request.args.get('cursor') or '0'
    offset = whole_number(raw_cursor, low=0) if raw_cursor != '0' else 0
    if kind not in FILTERS or limit is None or offset is None:
        return _refused('The filter, limit or cursor is not one this server knows')
    rows = _inbox(g.oauth_user.id, kind).offset(offset).limit(limit + 1).all()
    data = [conversation_entity(row, membership.member(row.id, g.oauth_user.id)) for row in rows[:limit]]
    return jsonify({'data': data, 'meta': {'filter': kind,
                                           'next_cursor': str(offset + limit) if len(rows) > limit else None,
                                           'prev_cursor': str(max(offset - limit, 0)) if offset else None}})


@bp.route('/api/v1.1/direct/unread_count')
@require_oauth('read')
def direct_unread_count():
    viewer_id = g.oauth_user.id
    member = conversation_member.c
    primary = _inbox(viewer_id, 'primary').filter(member.muted.is_(False), select(ChatMessage.id).where(
        ChatMessage.conversation_id == Conversation.id, ChatMessage.recipient_id == viewer_id,
        ChatMessage.read.is_(False), ChatMessage.deleted.is_(False)).exists())
    return jsonify({'primary': primary.count(), 'requests': _inbox(viewer_id, 'requests').count()})


MUTUALS_PAGE = 10


@bp.route('/api/v1.1/direct/compose/mutuals')
@require_oauth('read')
def direct_mutuals():
    """Whom the viewer follows and who follow them back, the app's first suggestions on its New message screen,
    ten at a time by `cursor` (the last one's id), as Pixelfed's FollowerService::getMutualsWithProfiles."""
    me = g.oauth_user
    raw = request.args.get('cursor')
    after = whole_number(raw) if raw else 0
    if after is None:
        return _refused('cursor is not an account id')
    followed = db.session.query(UserFollower.remote_user_id).filter(
        UserFollower.local_user_id == me.id, UserFollower.is_inward.is_(False), UserFollower.is_accepted.is_(True))
    following_me = db.session.query(UserFollower.local_user_id).filter(
        UserFollower.remote_user_id == me.id, UserFollower.is_inward.is_(False), UserFollower.is_accepted.is_(True))
    mutuals = without_local_only_actors(User.query.filter(User.id.in_(followed), User.id.in_(following_me),
                                                           User.deleted.is_(False), User.banned.is_(False)),
                                        User, True)
    total = mutuals.count()
    rows = mutuals.filter(User.id > after).order_by(User.id).limit(MUTUALS_PAGE + 1).all()
    more, rows = len(rows) > MUTUALS_PAGE, rows[:MUTUALS_PAGE]
    return jsonify({'data': [account(person, me.id) for person in rows], 'has_more': more,
                    'next_cursor': str(rows[-1].id) if more else None, 'total_count': total})


LOOKUP_LIMIT = 8


@bp.route('/api/v1.1/direct/lookup', methods=['POST'])
@require_oauth('read')
def direct_lookup():
    """The app's New message search (Pixelfed's composeLookup): up to eight accounts whose name (or remote address,
    with an '@' in it) holds `q`, from the people listing W1's search reads (nothing is fetched from another server),
    without blocks either way. Each is wrapped as Pixelfed wraps one, or with `_pe` the account itself."""
    me = g.oauth_user
    fields = request.get_json(silent=True)
    fields = fields if isinstance(fields, dict) else {}
    query = fields.get('q', request.args.get('q'))
    if not isinstance(query, str) or not 2 <= len(query) <= 50:
        return _refused('q is 2 to 50 characters')
    blocked = db.session.query(UserBlock.blocked_id).filter(UserBlock.blocker_id == me.id)
    blocking = db.session.query(UserBlock.blocker_id).filter(UserBlock.blocked_id == me.id)
    people = reads_users.person_listing(me.id, reads_users.People(search=query.lstrip('@'))) \
        .filter(User.id.notin_(blocked), User.id.notin_(blocking)).limit(LOOKUP_LIMIT).all()
    accounts = [(person, account(person, me.id)) for person in people]
    if fields.get('_pe') or request.args.get('_pe'):
        return jsonify([entity for _person, entity in accounts])
    return jsonify([{'local': person.is_local(), 'id': str(person.id), 'name': person.user_name, 'privacy': True,
                     'avatar': entity['avatar'], 'account': entity} for person, entity in accounts])


def may_message(sender: User, recipient: User):
    """None when `sender` may message `recipient`, else the refusal: a block either way, a local-only account and a
    remote one (spec 3.4), or the sender's own standing (can_send_pm_to, as the web chat asks)."""
    if recipient.has_blocked_user(sender.id) or sender.has_blocked_user(recipient.id):
        return _refused('You cannot message this account.', 403)
    if refuses_remote_contact(sender, recipient):
        return _refused('This account cannot be messaged from here.', 403)
    if not sender.can_send_pm_to(recipient):
        return _refused('You are not permitted to send a private message at this time, likely because your '
                        'account is too new.', 403)
    return None


@bp.route('/api/v1.1/direct/conversations', methods=['POST'])
@require_oauth('write')
def direct_start():
    """The conversation with this person, found or started (as a request of theirs unless they follow the sender).
    One recipient: PieFed's chat is one-to-one."""
    me = g.oauth_user
    fields = request.get_json(silent=True)
    named = fields.get('recipient_ids') if isinstance(fields, dict) else None
    if not isinstance(named, list) or not named:
        return _refused('Name the person to message in recipient_ids')
    ids = {str(value) for value in named} - {str(me.id)}
    if not ids:
        return _refused('Name someone other than yourself')
    if len(ids) > 1:
        return _refused('A conversation here is with one person: group chats are not supported')
    number = whole_number(ids.pop())
    recipient = without_local_only_actors(User.query.filter(User.id == number, User.deleted.is_(False)), User,
                                          True).first() if number else None
    if recipient is None:
        return _refused('One or more recipients could not be found.', 404)
    refusal = may_message(me, recipient)
    if refusal:
        return refusal
    conversation = Conversation.find_existing_conversation(recipient=recipient, sender=me)
    status = 200
    if conversation is None:
        conversation, status = membership.start_conversation(me, recipient), 201
    membership.update_member(conversation.id, me.id, joined=True, hidden=False)   # opening it again shows it
    return jsonify(conversation_entity(conversation, membership.member(conversation.id, me.id))), status


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>')
@require_oauth('read')
def direct_conversation(conversation_id):
    found = _membership(conversation_id)
    return jsonify(conversation_entity(*found)) if found else _not_found()


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/messages')
@require_oauth('read')
def direct_messages(conversation_id):
    """Newest first: `max_id` pages back in time, `min_id` is what arrived since (what the app polls with)."""
    found = _membership(conversation_id)
    if not found:
        return _not_found()
    limit = whole_number(request.args.get('limit', 20), high=MAX_MESSAGES)
    max_id, min_id = (whole_number(request.args[name]) if request.args.get(name) else 0 for name in ('max_id', 'min_id'))
    if limit is None or max_id is None or min_id is None:
        return _refused('The limit, max_id or min_id is not a whole number this server takes')
    query = _messages(found[0])
    if min_id:
        rows = query.filter(ChatMessage.id > min_id).order_by(ChatMessage.id).limit(limit + 1).all()
        more, rows = len(rows) > limit, list(reversed(rows[:limit]))
    else:
        if max_id:
            query = query.filter(ChatMessage.id < max_id)
        rows = query.order_by(ChatMessage.id.desc()).limit(limit + 1).all()
        more, rows = len(rows) > limit, rows[:limit]
    viewer_id = g.oauth_user.id
    return jsonify({'data': [message_entity(row, viewer_id) for row in rows],
                    'meta': {'has_more': more, 'newest_id': str(rows[0].id) if rows else None,
                             'oldest_id': str(rows[-1].id) if rows else None}})


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/messages', methods=['POST'])
@require_oauth('write')
def direct_send(conversation_id):
    """Text, photos (the sender's own uploads, through /api/v2/media; W1.4b2) or both, through the web chat's own
    send; whoever had left is in the conversation again, as the web's new message does (D748)."""
    found = _membership(conversation_id)
    if not found:
        return _not_found()
    conversation, me = found[0], g.oauth_user
    fields = request.get_json(silent=True)
    fields = fields if isinstance(fields, dict) else {}
    named = fields.get('media_ids') or []
    pictures = own_uploads(me.id, named) if isinstance(named, list) and len(named) <= MAX_MESSAGE_PICTURES else None
    if pictures is None or len(pictures) != len(named):
        return _refused(f'media_ids name up to {MAX_MESSAGE_PICTURES} of your own uploads')
    text = fields.get('message') or ''
    if not isinstance(text, str) or len(text) > MAX_CHARACTERS or not (text.strip() or pictures):
        return _refused(f'A message is text of 1 to {MAX_CHARACTERS} characters, photos, or both')
    for other in conversation.members:
        refusal = None if other.id == me.id else may_message(me, other)
        if refusal:
            return refusal
    for person in conversation.members:
        membership.update_member(conversation.id, person.id, joined=True)
    message = send_message(text, conversation.id, user=me, src=SRC_API, files=pictures)
    return jsonify(message_entity(message, me.id)), 201


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/messages/<int:message_id>', methods=['DELETE'])
@require_oauth('write')
def direct_delete(conversation_id, message_id):
    """One's own message, deleted as alpha's delete does it, and federated."""
    found = _membership(conversation_id)
    number = whole_number(message_id)
    message = ChatMessage.query.filter_by(id=number, conversation_id=found[0].id, sender_id=g.oauth_user.id,
                                          deleted=False).first() if found and number else None
    if message is None:
        return _not_found()
    message.deleted = True
    db.session.commit()
    task_selector('delete_pm', message_id=message.id)
    return jsonify({'deleted': True})


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/read', methods=['POST'])
@require_oauth('write')
def direct_read(conversation_id):
    """The viewer's messages in it read, up to `message_id` or all of them, with their notifications."""
    found = _membership(conversation_id)
    if not found:
        return _not_found()
    fields = request.get_json(silent=True)
    up_to = fields.get('message_id') if isinstance(fields, dict) else None
    me = g.oauth_user
    unread = _unread(found[0], me.id)
    if up_to is not None:
        if whole_number(up_to) is None:
            return _refused('message_id is not a message id')
        unread = unread.filter(ChatMessage.id <= whole_number(up_to))
    read_ids = set()
    for message in unread.all():
        message.read = True
        read_ids.add(message.id)
    for notification in Notification.query.filter_by(user_id=me.id, notif_type=NOTIF_MESSAGE,
                                                      subtype='chat_message', read=False):
        if (notification.targets or {}).get('message_id') in read_ids:
            notification.read = True
            me.unread_notifications = max((me.unread_notifications or 0) - 1, 0)
    db.session.commit()
    return jsonify(_state(found[0], membership.member(found[0].id, me.id)))


def _set(conversation_id, **values):
    found = _membership(conversation_id)
    if not found:
        return _not_found()
    membership.update_member(found[0].id, g.oauth_user.id, **values)
    return jsonify(_state(found[0], membership.member(found[0].id, g.oauth_user.id)))


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/accept', methods=['POST'])
@require_oauth('write')
def direct_accept(conversation_id):
    return _set(conversation_id, state=membership.ACTIVE)


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/mute', methods=['POST'])
@require_oauth('write')
def direct_mute(conversation_id):
    return _set(conversation_id, muted=True)


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/unmute', methods=['POST'])
@require_oauth('write')
def direct_unmute(conversation_id):
    return _set(conversation_id, muted=False)


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/hide', methods=['POST'])
@require_oauth('write')
def direct_hide(conversation_id):
    return _set(conversation_id, hidden=True)


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/unhide', methods=['POST'])
@require_oauth('write')
def direct_unhide(conversation_id):
    return _set(conversation_id, hidden=False)


@bp.route('/api/v1.1/direct/conversations/<int:conversation_id>/leave', methods=['POST'])
@require_oauth('write')
def direct_leave(conversation_id):
    """PieFed's leave (alpha's and the web's): out of it, and the conversation gone once nobody here is in it."""
    found = _membership(conversation_id)
    if not found:
        return _not_found()
    conversation = found[0]
    membership.update_member(conversation.id, g.oauth_user.id, joined=False)
    state = _state(conversation, membership.member(conversation.id, g.oauth_user.id))
    conversation.delete_if_abandoned()
    return jsonify(state)
