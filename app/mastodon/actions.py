"""What a Mastodon app does to a status (plan W1.3; ruling M5): favourite is an upvote and unfavourite takes it back,
bookmark and unbookmark keep it, reblog boosts a post and unreblog takes the boost back (a comment cannot be
boosted). Each runs through the shared function the alpha API and the web use (app.shared.post,
app.shared.reply), as the person the app's token acts for (app.utils.Authorised), and answers the status as it now is.
Mastodon's actions are idempotent where PieFed's toggle or refuse a repeat, so each acts only when something changes."""
from flask import g, jsonify

from app import db
from app.constants import SRC_API
from app.mastodon import bp
from app.mastodon.errors import failure
from app.mastodon.ids import find_status
from app.mastodon.statuses import Page
from app.mastodon.timelines import readable
from app.models import Post, PostBookmark, PostReplyBookmark, PostReplyVote, PostVote
from app.oauth_server.protect import require_oauth
from app.shared import post as shared_post
from app.shared import reply as shared_reply
from app.utils import Authorised


def _no_boost(reply, auth):
    raise Exception('A comment cannot be boosted')   # PieFed boosts posts alone (PostBoost)


def _not_found():
    return jsonify({'error': 'Record not found'}), 404


def _answer(row):
    db.session.expire(row)   # the shared write committed through its own path: read the row as it now is
    return jsonify(Page(g.oauth_user, [row]).statuses([row])[0])


def _act(status_id: str, on_post, on_reply):
    row = find_status(status_id)
    if not readable(row, g.oauth_user.id):
        return _not_found()
    try:
        (on_post if isinstance(row, Post) else on_reply)(row, Authorised(g.oauth_user))
    except Exception as error:   # a shared write's abort (403 a banned account, 429 the vote quota) or refusal
        return failure(error)
    return _answer(row)


def _upvoted(model, column, row) -> bool:
    return db.session.query(model).filter(model.user_id == g.oauth_user.id, column == row.id,
                                          model.effect > 0).first() is not None


def _favourite_post(post, auth):
    if not _upvoted(PostVote, PostVote.post_id, post):
        shared_post.vote_for_post(post.id, 'upvote', True, None, SRC_API, auth)


def _favourite_reply(reply, auth):
    if not _upvoted(PostReplyVote, PostReplyVote.post_reply_id, reply):
        shared_reply.vote_for_reply(reply.id, 'upvote', True, None, SRC_API, auth)


def _unfavourite_post(post, auth):
    if _upvoted(PostVote, PostVote.post_id, post):
        shared_post.vote_for_post(post.id, 'reversal', True, None, SRC_API, auth)


def _unfavourite_reply(reply, auth):
    if _upvoted(PostReplyVote, PostReplyVote.post_reply_id, reply):
        shared_reply.vote_for_reply(reply.id, 'reversal', True, None, SRC_API, auth)


def _kept(model, column, row) -> bool:
    return db.session.query(model).filter(model.user_id == g.oauth_user.id, column == row.id).first() is not None


def _bookmark_post(post, auth):
    if not _kept(PostBookmark, PostBookmark.post_id, post):
        shared_post.bookmark_post(post.id, SRC_API, auth)


def _bookmark_reply(reply, auth):
    if not _kept(PostReplyBookmark, PostReplyBookmark.post_reply_id, reply):
        shared_reply.bookmark_reply(reply.id, SRC_API, auth)


def _unbookmark_post(post, auth):
    if _kept(PostBookmark, PostBookmark.post_id, post):
        shared_post.remove_bookmark_post(post.id, SRC_API, auth)


def _unbookmark_reply(reply, auth):
    if _kept(PostReplyBookmark, PostReplyBookmark.post_reply_id, reply):
        shared_reply.remove_bookmark_reply(reply.id, SRC_API, auth)


@bp.route('/api/v1/statuses/<status_id>/favourite', methods=['POST'])
@require_oauth('write:favourites')
def favourite(status_id):
    return _act(status_id, _favourite_post, _favourite_reply)


@bp.route('/api/v1/statuses/<status_id>/unfavourite', methods=['POST'])
@require_oauth('write:favourites')
def unfavourite(status_id):
    return _act(status_id, _unfavourite_post, _unfavourite_reply)


@bp.route('/api/v1/statuses/<status_id>/bookmark', methods=['POST'])
@require_oauth('write:bookmarks')
def bookmark(status_id):
    return _act(status_id, _bookmark_post, _bookmark_reply)


@bp.route('/api/v1/statuses/<status_id>/unbookmark', methods=['POST'])
@require_oauth('write:bookmarks')
def unbookmark(status_id):
    return _act(status_id, _unbookmark_post, _unbookmark_reply)


@bp.route('/api/v1/statuses/<status_id>/reblog', methods=['POST'])
@require_oauth('write:statuses')
def reblog(status_id):
    """Answers the reblog as Mastodon does, a status holding the original in `reblog`; it is the original's own id,
    since a boost is not a status of its own here."""
    answer = _act(status_id, lambda post, auth: shared_post.boost_post(post.id, SRC_API, auth), _no_boost)
    if isinstance(answer, tuple):   # a refusal: (response, status)
        return answer
    original = answer.get_json()
    return jsonify({**original, 'account': Page(g.oauth_user, []).account(g.oauth_user), 'reblog': original,
                    'content': '', 'media_attachments': [], 'mentions': [], 'tags': [], 'card': None})


@bp.route('/api/v1/statuses/<status_id>/unreblog', methods=['POST'])
@require_oauth('write:statuses')
def unreblog(status_id):
    return _act(status_id, lambda post, auth: shared_post.unboost_post(post.id, SRC_API, auth), _no_boost)
