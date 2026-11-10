"""Watch history in PeerTube's API (spec 2026-11-09-peertube-login-design §3, ruling P5): PieFed's read marks of video
posts. Watching a video in the app (its periodic POST videos/{id}/views) marks the post read, as opening it on the
web does; the history lists the read videos, newest read first; removing an entry, or all before a date, takes the
read mark away, so the web's "hide read posts" shows them again."""
from datetime import datetime, timedelta, timezone

from flask import g, jsonify, request
from sqlalchemy import desc, select

from app import db
from app.mastodon.timelines import readable
from app.models import Post, read_posts, utcnow
from app.peertube import bp, entities
from app.peertube.auth import problem, signed_in
from app.peertube.entities import find_video, not_found
from app.shared.post import mark_post_read

REMARK_AFTER = timedelta(minutes=10)   # the app reports every few seconds while it plays: one mark is enough
MAX_HISTORY = 1000


def _read_at(user_id: int, post_id: int):
    return db.session.execute(select(read_posts.c.interacted_at).where(
        read_posts.c.user_id == user_id, read_posts.c.read_post_id == post_id)).scalar()


@bp.route('/api/v1/videos/<video_id>/views', methods=['POST'])
@signed_in(optional=True)
def view(video_id):
    """PieFed counts no views; a signed-in viewer's watching marks the post read."""
    post = find_video(video_id)
    if post is None:
        return not_found()
    user = g.oauth_user
    if user is not None:
        marked = _read_at(user.id, post.id)
        if marked is None or marked < utcnow() - REMARK_AFTER:
            mark_post_read([post.id], True, user.id)
    return '', 204


@bp.route('/api/v1/users/me/history/videos')
@signed_in()
def history():
    user = g.oauth_user
    start, count = entities.window()
    rows = db.session.query(Post).join(read_posts, read_posts.c.read_post_id == Post.id).filter(
        read_posts.c.user_id == user.id, entities.playable_clause()).order_by(
        desc(read_posts.c.interacted_at), desc(Post.id)).limit(MAX_HISTORY).all()
    found = [post for post in rows if readable(post, user.id)]
    return jsonify({'total': len(found), 'data': [entities.video(post) for post in found[start:start + count]]})


@bp.route('/api/v1/users/me/history/videos/<video_id>', methods=['DELETE'])
@signed_in('write')
def forget_one(video_id):
    post = find_video(video_id)
    if post is None:
        return not_found()
    mark_post_read([post.id], False, g.oauth_user.id)
    return '', 204


def _before(raw) -> datetime | None:
    """PeerTube's beforeDate, an ISO 8601 moment, as the naive UTC the database keeps."""
    moment = datetime.fromisoformat(raw)
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


@bp.route('/api/v1/users/me/history/videos/remove', methods=['POST'])
@signed_in('write')
def forget_all():
    """Every read mark of a video post, or those before `beforeDate`."""
    fields = request.get_json(silent=True)
    raw = (fields if isinstance(fields, dict) else request.form).get('beforeDate')
    try:
        before = _before(raw) if raw else None
    except (TypeError, ValueError):
        return problem(400, 'invalid_request', 'beforeDate is an ISO 8601 date')
    user = g.oauth_user
    marked = db.session.query(read_posts.c.read_post_id).join(Post, Post.id == read_posts.c.read_post_id).filter(
        read_posts.c.user_id == user.id, entities.playable_clause())
    if before is not None:
        marked = marked.filter(read_posts.c.interacted_at < before)
    ids = [row[0] for row in marked.all()]
    if ids:
        mark_post_read(ids, False, user.id)
    return '', 204
