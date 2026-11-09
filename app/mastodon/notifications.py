"""Mastodon's notifications (v1; plan W1.2). PieFed's that have a Mastodon form:

- a new post by a person or community the viewer follows, or in a topic or feed they watch: `status`;
- a reply to their post or comment, and a mention of them: `mention`, with the reply or mentioning status;
- a new follower: `follow`; a follow request: `follow_request`.

Bans, moderation, reports, messages and the rest have none and are not listed. A notification about something the
viewer may no longer read, or that is hidden here, is left out, as the alpha API leaves it out. Pages are cut by
notification id (`max_id`, `since_id`, `min_id`) and named in a Link header. PieFed reports no `api_versions`, so apps
ask for these, not v2's grouped notifications (plan W1.0 inventory)."""
from flask import g, jsonify, request

from app import db
from app.constants import NOTIF_COMMUNITY, NOTIF_FEED, NOTIF_FOLLOW, NOTIF_FOLLOW_REQUEST, NOTIF_MENTION, \
    NOTIF_POST, NOTIF_REPLY, NOTIF_TOPIC, NOTIF_USER
from app.hidden_routes import hidden_notifications
from app.mastodon import bp
from app.mastodon.entities import timestamp
from app.mastodon.paging import DEFAULT_LIMIT, MAX_LIMIT, links
from app.mastodon.statuses import Page
from app.mastodon.timelines import readable
from app.models import Notification, Post, PostReply, User
from app.oauth_server.protect import require_oauth
from app.shared import user as shared_user

_POST_TYPES = (NOTIF_USER, NOTIF_COMMUNITY, NOTIF_TOPIC, NOTIF_FEED)
_COMMENT_TYPES = (NOTIF_POST, NOTIF_REPLY)
_TYPES = {**{kind: 'status' for kind in _POST_TYPES}, **{kind: 'mention' for kind in _COMMENT_TYPES},
          NOTIF_MENTION: 'mention', NOTIF_FOLLOW: 'follow', NOTIF_FOLLOW_REQUEST: 'follow_request'}
_PG_INT_MAX = 2 ** 31 - 1


def _int(name: str) -> int | None:
    value = request.args.get(name, '')
    return int(value) if value.isdigit() and 0 < int(value) <= _PG_INT_MAX else None


def _limit() -> int:
    value = request.args.get('limit', '')
    return min(max(int(value), 1), MAX_LIMIT) if value.isdigit() else DEFAULT_LIMIT


def _kinds() -> list[int]:
    """The PieFed types the request's `types[]` and `exclude_types[]` leave."""
    wanted = set(request.args.getlist('types[]') or request.args.getlist('types')) or set(_TYPES.values())
    unwanted = set(request.args.getlist('exclude_types[]') or request.args.getlist('exclude_types'))
    return [kind for kind, name in _TYPES.items() if name in wanted and name not in unwanted]


def _subject(notification: Notification):
    """The post or comment a notification is about, or None (a follow has none)."""
    targets = notification.targets or {}
    if notification.notif_type in _POST_TYPES or (notification.notif_type == NOTIF_MENTION and
                                                  notification.subtype == 'post_mention'):
        return db.session.get(Post, targets.get('post_id') or 0)
    if notification.notif_type in _COMMENT_TYPES or notification.notif_type == NOTIF_MENTION:
        return db.session.get(PostReply, targets.get('comment_id') or 0)
    return None


def _shown(rows: list[Notification]) -> list[tuple]:
    """(notification, author, subject) for each one the viewer may be told of."""
    shown = []
    for notification, hidden in zip(rows, hidden_notifications(rows)):
        author = db.session.get(User, notification.author_id) if notification.author_id else None
        if hidden or author is None or author.deleted:
            continue
        subject = _subject(notification)
        if notification.notif_type not in (NOTIF_FOLLOW, NOTIF_FOLLOW_REQUEST) and \
                not readable(subject, g.oauth_user.id):
            continue
        shown.append((notification, author, subject))
    return shown


def _entity(notification: Notification, author: User, subject, page: Page) -> dict:
    entity = {'id': str(notification.id), 'type': _TYPES[notification.notif_type],
              'created_at': timestamp(notification.created_at), 'group_key': f'ungrouped-{notification.id}',
              'account': page.account(author)}
    if subject is not None:
        entity['status'] = page.statuses([subject])[0]
    return entity


@bp.route('/api/v1/notifications')
@require_oauth('read:notifications')
def notifications():
    query = Notification.query.filter(Notification.user_id == g.oauth_user.id, Notification.notif_type.in_(_kinds()))
    if muted := shared_user.muted_ids(g.oauth_user.id, notifications_only=True):   # a mute with its notifications
        query = query.filter(Notification.author_id.not_in(muted))
    if (max_id := _int('max_id')) is not None:
        query = query.filter(Notification.id < max_id)
    if (since_id := _int('since_id')) is not None:
        query = query.filter(Notification.id > since_id)
    ascending = (min_id := _int('min_id')) is not None
    if ascending:
        query = query.filter(Notification.id > min_id)
    order = Notification.id.asc() if ascending else Notification.id.desc()
    rows = query.order_by(order).limit(_limit()).all()
    if ascending:
        rows.reverse()
    shown = _shown(rows)
    page = Page(g.oauth_user, [subject for _n, _a, subject in shown if subject is not None])
    page.authors.update({author.id: author for _n, author, _s in shown})
    response = jsonify([_entity(notification, author, subject, page) for notification, author, subject in shown])
    if rows:   # the rows read, not only those shown: a page of hidden ones still leads on
        response.headers['Link'] = links(str(rows[0].id), str(rows[-1].id))
    return response
