"""Notifications in PeerTube's API (spec 2026-11-09-peertube-login-design §3, ruling P3). Four of PieFed's kinds have
a PeerTube type the app shows truthfully, and only those are listed:

- a new post by someone or in somewhere the person follows, when it is a video: 1 NEW_VIDEO_FROM_SUBSCRIPTION;
- a comment on their post or a reply to their comment, on a video: 2 NEW_COMMENT_ON_MY_VIDEO;
- a mention of them in a comment on a video: 11 COMMENT_MENTION;
- a new follower: 10 NEW_FOLLOW.

The official app decodes every element and fails the whole list on one unknown type, so nothing else is sent: chat,
bans, moderation and the rest stay on the web. What a person may be told of is the Mastodon API's rule
(app.mastodon.notifications.shown): not hidden here, the author not muted, the subject still readable."""
from flask import g, jsonify, request
from sqlalchemy import text, update

from app import db
from app.constants import NOTIF_FOLLOW, NOTIF_MENTION
from app.mastodon.kinds import COMMENT_TYPES, POST_TYPES
from app.mastodon.notifications import shown
from app.models import Community, Notification, Post, PostReply, User
from app.peertube import bp, entities
from app.peertube.auth import problem, signed_in
from app.peertube.entities import timestamp
from app.shared import user as shared_user

NEW_VIDEO_FROM_SUBSCRIPTION, NEW_COMMENT_ON_MY_VIDEO, NEW_FOLLOW, COMMENT_MENTION = 1, 2, 10, 11
KINDS = (*POST_TYPES, *COMMENT_TYPES, NOTIF_MENTION, NOTIF_FOLLOW)
MAX_READ = 300   # the newest notifications looked at: each is checked as it is shown
MAX_IDS = 100


def _actor(actor: User | Community) -> dict:
    """PeerTube's ActorInfo: its images name the whole URL (fileUrl), unlike an account's."""
    url = entities.image_url(actor.avatar_image() if isinstance(actor, User) else actor.icon_image())
    images = [{'fileUrl': url, 'width': 120}] if url else []
    name = actor.user_name if isinstance(actor, User) else actor.name
    title = actor.display_name() if isinstance(actor, User) else actor.title
    return {'id': actor.id, 'name': name, 'displayName': title, 'host': entities.host_of(actor),
            'avatars': images, 'avatar': images[0] if images else None}


def _video_info(post: Post) -> dict:
    return {'id': post.id, 'uuid': entities.video_uuid(post), 'shortUUID': entities.video_uuid(post),
            'name': post.title or ''}


def _comment(reply: PostReply, author: User) -> dict:
    return {'id': reply.id, 'threadId': reply.root_id or reply.id, 'account': _actor(author),
            'video': _video_info(reply.post)}


def _typed(notification: Notification, author: User, subject) -> dict | None:
    """The notification as PeerTube's, or None for one with no PeerTube type the app would show truthfully."""
    kind = notification.notif_type
    if kind in POST_TYPES and isinstance(subject, Post) and entities.is_video(subject):
        return {'type': NEW_VIDEO_FROM_SUBSCRIPTION,
                'video': {**_video_info(subject), 'channel': _actor(subject.community)}}
    if kind in (*COMMENT_TYPES, NOTIF_MENTION) and isinstance(subject, PostReply) and entities.is_video(subject.post):
        return {'type': COMMENT_MENTION if kind == NOTIF_MENTION else NEW_COMMENT_ON_MY_VIDEO,
                'comment': _comment(subject, author)}
    if kind == NOTIF_FOLLOW:
        me = g.oauth_user
        return {'type': NEW_FOLLOW, 'actorFollow': {
            'id': notification.id, 'follower': _actor(author), 'state': 'accepted',
            'following': {'type': 'account', 'name': me.user_name, 'displayName': me.display_name(),
                          'host': entities.host_of(me)}}}
    return None


def _listed(unread_only: bool) -> list[dict]:
    user = g.oauth_user
    query = Notification.query.filter(Notification.user_id == user.id, Notification.notif_type.in_(KINDS))
    if muted := shared_user.muted_ids(user.id, notifications_only=True):
        query = query.filter(Notification.author_id.not_in(muted))
    if unread_only:
        query = query.filter(Notification.read == False)
    rows = query.order_by(Notification.id.desc()).limit(MAX_READ).all()
    listed = []
    for notification, author, subject in shown(rows, user.id):
        if (typed := _typed(notification, author, subject)) is not None:
            when = timestamp(notification.created_at or entities.EPOCH)
            listed.append({'id': notification.id, 'read': bool(notification.read), 'createdAt': when,
                           'updatedAt': when, **typed})
    return listed


def _count() -> tuple[int, int]:
    """start and count, where a count of 0 asks for the total alone (the unread badge)."""
    if request.args.get('count', '') == '0':
        return entities.whole('start', 0), 0
    return entities.window()


def _types() -> set[int] | None:
    """typeOneOf, repeated, bracketed or joined by commas; None for every type."""
    named = request.args.getlist('typeOneOf') + request.args.getlist('typeOneOf[]')
    if not named:
        return None
    return {int(part) for value in named for part in value.split(',') if part.strip().isdigit()}


@bp.route('/api/v1/users/me/notifications')
@signed_in()
def list_notifications():
    start, count = _count()
    listed = _listed(request.args.get('unread', '').lower() == 'true')
    if (types := _types()) is not None:
        listed = [item for item in listed if item['type'] in types]
    return jsonify({'total': len(listed), 'data': listed[start:start + count]})


def _recount(user: User) -> None:
    user.unread_notifications = Notification.query.filter_by(user_id=user.id, read=False).count()
    db.session.commit()


@bp.route('/api/v1/users/me/notifications/read', methods=['POST'])
@signed_in('write')
def read_some():
    """The official app sends {ids: [..]}, TubeLab a form's ids[]."""
    fields = request.get_json(silent=True)
    if isinstance(fields, dict):
        raw = fields.get('ids')
    else:
        raw = request.form.getlist('ids[]') or request.form.getlist('ids')
    if not isinstance(raw, list) or not raw or len(raw) > MAX_IDS:
        return problem(400, 'invalid_request', f'ids is a list of 1 to {MAX_IDS} notification ids')
    try:
        ids = [int(value) for value in raw]
    except (TypeError, ValueError):
        return problem(400, 'invalid_request', 'ids are notification ids')
    db.session.execute(update(Notification).where(Notification.user_id == g.oauth_user.id,
                                                  Notification.id.in_(ids)).values(read=True))
    _recount(g.oauth_user)
    return '', 204


@bp.route('/api/v1/users/me/notifications/read-all', methods=['POST'])
@signed_in('write')
def read_all():
    db.session.execute(text('UPDATE notification SET read = true WHERE user_id = :user_id'),
                       {'user_id': g.oauth_user.id})
    _recount(g.oauth_user)
    return '', 204
