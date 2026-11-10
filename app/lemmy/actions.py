"""What a member does in Lemmy's API (spec 2026-11-10-lemmy-v3-design §2, ruling L2): vote, comment, save, mark read,
hide, follow and block. Each takes Lemmy's body under alpha's names, loads it with alpha's own request schema, so a
mistyped field is refused as alpha refuses it, and calls alpha's handler, so every write goes through app/shared with
its federation, notifications, limits and checks (plan §3)."""
from flask import jsonify, request
from marshmallow import EXCLUDE

from app import limiter
from app.api.alpha.routes import COMMENT_LIMIT, COMMENT_SCOPE, is_trusted_request
from app.api.alpha.schema import BlockCommunityRequest, CreateCommentRequest, DeleteCommentRequest, \
    DeletePostRequest, EditCommentRequest, FollowCommunityRequest, LikeCommentRequest, LikePostRequest, \
    MarkPostAsReadRequest, SaveCommentRequest, SavePostRequest, UserBlockRequest
from app.api.alpha.utils.community import post_community_block, post_community_follow
from app.api.alpha.utils.post import a_visible_post, post_post_delete, post_post_like, post_post_mark_as_read, \
    put_post_save
from app.api.alpha.utils.reply import post_reply, post_reply_delete, post_reply_like, put_reply, put_reply_save
from app.api.alpha.utils.user import post_user_block
from app.constants import SRC_API
from app.lemmy import bp, shapes
from app.shared import post as shared_post

COMMENT_NAMES = {'content': 'body'}   # Lemmy's CreateComment and EditComment
MAX_POSTS = 50   # posts hidden in one request: Jerboa hides one at a time


def _auth() -> str | None:
    return request.headers.get('Authorization')


def _body(schema, renamed: dict | None = None) -> dict:
    """The request's JSON body under alpha's names, loaded by alpha's schema for the same request."""
    raw = request.get_json(silent=True)
    raw = dict(raw) if isinstance(raw, dict) else {}
    for lemmy, alpha in (renamed or {}).items():
        if lemmy in raw:
            raw[alpha] = raw.pop(lemmy)
    return schema().load(raw, unknown=EXCLUDE)


def _post_response(alpha: dict):
    return jsonify(post_view=shapes.post_view(alpha['post_view']))


def _comment_response(alpha: dict):
    """CommentResponse: Lemmy names whom the comment notified; Jerboa reads nothing of it."""
    return jsonify(comment_view=shapes.comment_view(alpha['comment_view']), recipient_ids=[])


@bp.route('/api/v3/post/like', methods=['POST'])
def like_post():
    return _post_response(post_post_like(_auth(), _body(LikePostRequest)))


@bp.route('/api/v3/post/save', methods=['PUT'])
def save_post():
    return _post_response(put_post_save(_auth(), _body(SavePostRequest)))


@bp.route('/api/v3/post/delete', methods=['POST'])
def delete_post():
    return _post_response(post_post_delete(_auth(), _body(DeletePostRequest)))


@bp.route('/api/v3/post/mark_as_read', methods=['POST'])
def mark_posts_read():
    return jsonify(success=post_post_mark_as_read(_auth(), _body(MarkPostAsReadRequest))['success'])


@bp.route('/api/v3/post/hide', methods=['POST'])
def hide_posts():
    """Lemmy hides several at once ({post_ids, hide}): at most MAX_POSTS, each one the person may see, through the
    shared hide alone (no post view is rendered for an answer that carries none)."""
    fields = request.get_json(silent=True)
    fields = fields if isinstance(fields, dict) else {}
    post_ids, hide = fields.get('post_ids'), fields.get('hide')
    if not isinstance(post_ids, list) or not 0 < len(post_ids) <= MAX_POSTS or not isinstance(hide, bool) \
            or not all(isinstance(post_id, int) and not isinstance(post_id, bool) for post_id in post_ids):
        raise Exception('invalid_request')
    for post_id in post_ids:
        shared_post.hide_post(a_visible_post(post_id, _auth()).id, hide, SRC_API, _auth())
    return jsonify(success=True)


@bp.route('/api/v3/comment', methods=['POST'])
def create_comment():
    data = _body(CreateCommentRequest, COMMENT_NAMES)
    with limiter.shared_limit(COMMENT_LIMIT, scope=COMMENT_SCOPE, exempt_when=is_trusted_request):   # alpha's bucket
        return _comment_response(post_reply(_auth(), data))


@bp.route('/api/v3/comment', methods=['PUT'])
def edit_comment():
    return _comment_response(put_reply(_auth(), _body(EditCommentRequest, COMMENT_NAMES)))


@bp.route('/api/v3/comment/delete', methods=['POST'])
def delete_comment():
    return _comment_response(post_reply_delete(_auth(), _body(DeleteCommentRequest)))


@bp.route('/api/v3/comment/like', methods=['POST'])
def like_comment():
    return _comment_response(post_reply_like(_auth(), _body(LikeCommentRequest)))


@bp.route('/api/v3/comment/save', methods=['PUT'])
def save_comment():
    return _comment_response(put_reply_save(_auth(), _body(SaveCommentRequest)))


@bp.route('/api/v3/community/follow', methods=['POST'])
def follow_community():
    alpha = post_community_follow(_auth(), _body(FollowCommunityRequest))
    return jsonify(community_view=shapes.community_view(alpha['community_view']), discussion_languages=[])


@bp.route('/api/v3/community/block', methods=['POST'])
def block_community():
    alpha = post_community_block(_auth(), _body(BlockCommunityRequest))
    return jsonify(community_view=shapes.community_view(alpha['community_view']), blocked=alpha['blocked'])


@bp.route('/api/v3/user/block', methods=['POST'])
def block_person():
    alpha = post_user_block(_auth(), _body(UserBlockRequest))
    return jsonify(person_view=shapes.person_view(alpha['person_view']), blocked=alpha['blocked'])
