"""What a signed-in person does to a video in PeerTube's API (spec 2026-11-09-peertube-login-design §3): rate it (a like
is an upvote and a dislike a downvote, as M5 rules for favourites) and comment on it, reply to a comment and delete
their own. Each runs through the shared function the web and the alpha API use (app.shared.post, app.shared.reply),
with its permissions, rate limits, spam checks and federation. PeerTube's rating is a state, not a toggle, so each
acts only when something changes."""
from flask import g, jsonify, request

from app import db
from app.constants import SRC_API
from app.mastodon.timelines import readable
from app.models import PostReply, PostVote
from app.peertube import bp
from app.peertube.auth import failure, problem, signed_in
from app.peertube.entities import find_video, not_found
from app.peertube.videos import comment_entity
from app.shared import post as shared_post
from app.shared import reply as shared_reply
from app.utils import Authorised

RATINGS = {'like': 1, 'dislike': -1, 'none': 0}
MAX_COMMENT = 5000   # as a Mastodon status (app.mastodon.instance.MAX_CHARACTERS)


def _effect(post) -> int:
    vote = db.session.query(PostVote).filter_by(user_id=g.oauth_user.id, post_id=post.id).first()
    return 0 if vote is None or not vote.effect else (1 if vote.effect > 0 else -1)


@bp.route('/api/v1/users/me/videos/<video_id>/rating')
@signed_in()
def my_rating(video_id):
    post = find_video(video_id)
    if post is None:
        return not_found()
    name = {1: 'like', -1: 'dislike', 0: 'none'}[_effect(post)]
    return jsonify({'videoId': post.id, 'rating': name})


def _fields() -> dict:
    fields = request.get_json(silent=True)
    return fields if isinstance(fields, dict) else request.form.to_dict()


@bp.route('/api/v1/videos/<video_id>/rate', methods=['PUT'])
@signed_in('write')
def rate(video_id):
    post = find_video(video_id)
    if post is None:
        return not_found()
    wanted = RATINGS.get(_fields().get('rating'))
    if wanted is None:
        return problem(400, 'invalid_request', 'rating is like, dislike or none')
    auth, now = Authorised(g.oauth_user), _effect(post)
    try:
        if now != wanted and now != 0:   # take the old vote back first: a vote the other way is not a switch here
            shared_post.vote_for_post(post.id, 'reversal', True, None, SRC_API, auth)
        if now != wanted and wanted != 0:
            shared_post.vote_for_post(post.id, 'upvote' if wanted > 0 else 'downvote', True, None, SRC_API, auth)
    except Exception as error:   # a ban, the vote quota or another refusal of the shared vote
        return failure(error)
    return '', 204


def _text() -> str | None:
    text = _fields().get('text')
    return text if isinstance(text, str) and text.strip() and len(text) <= MAX_COMMENT else None


def _comment_answer(post, reply: PostReply):
    """PeerTube's answer to a new comment: 200 (the official app expects exactly that) and {comment}."""
    db.session.expire(reply)
    entry = {'comment': reply, 'replies': []}
    return jsonify({'comment': comment_entity(entry, post, reply.root_id or reply.id)}), 200


def _make(post, parent: PostReply | None):
    text = _text()
    if text is None:
        return problem(400, 'invalid_request', f'text is required, at most {MAX_COMMENT} characters')
    try:
        _uid, reply = shared_reply.make_reply({'body': text, 'notify_author': True, 'language_id': None}, post,
                                              parent.id if parent is not None else None, SRC_API,
                                              Authorised(g.oauth_user))
    except Exception as error:   # a ban, the rate limit, a locked post, a validation refusal
        return failure(error)
    return _comment_answer(post, reply)


@bp.route('/api/v1/videos/<video_id>/comment-threads', methods=['POST'])
@signed_in('write')
def comment(video_id):
    post = find_video(video_id)
    return _make(post, None) if post is not None else not_found()


def _readable_comment(post, comment_id: int) -> PostReply | None:
    reply = db.session.get(PostReply, comment_id)
    if reply is None or reply.post_id != post.id or not readable(reply, g.oauth_user.id):
        return None
    return reply


@bp.route('/api/v1/videos/<video_id>/comments/<int:comment_id>', methods=['POST'])
@signed_in('write')
def reply_to(video_id, comment_id):
    post = find_video(video_id)
    parent = _readable_comment(post, comment_id) if post is not None else None
    return _make(post, parent) if parent is not None else not_found()


@bp.route('/api/v1/videos/<video_id>/comments/<int:comment_id>', methods=['DELETE'])
@signed_in('write')
def delete_comment(video_id, comment_id):
    """The person's own comment only, as the shared delete allows an app."""
    post = find_video(video_id)
    reply = _readable_comment(post, comment_id) if post is not None else None
    if reply is None:
        return not_found()
    if reply.user_id != g.oauth_user.id:
        return problem(403, 'forbidden', 'Only its author can delete a comment here')
    try:
        shared_reply.delete_reply(reply.id, SRC_API, Authorised(g.oauth_user))
    except Exception as error:
        return failure(error)
    return '', 204
