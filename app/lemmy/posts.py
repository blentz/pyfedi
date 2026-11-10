"""Posts in Lemmy's API (spec 2026-11-10-lemmy-v3-design §2, §3): the feeds and a post, over alpha's handlers, so a
listing is what alpha's is for the same person, with every block, ban, NSFW preference and visibility rule."""
from flask import jsonify, request
from marshmallow import EXCLUDE

from app import db
from app.api.alpha.schema import ListPostsRequest
from app.api.alpha.utils.post import get_post, get_post_list
from app.lemmy import bp, shapes
from app.models import PostReply

# Lemmy's post sorts that alpha has no such name for, as their nearest
SORTS = {'MostComments': 'Active', 'NewComments': 'Active', 'Controversial': 'Hot'}
LISTED = ('type_', 'sort', 'page', 'limit', 'community_id', 'community_name', 'saved_only', 'liked_only')


def _auth() -> str | None:
    return request.headers.get('Authorization')


def listing_arguments() -> dict:
    """GetPosts' query as alpha's ListPostsRequest, loaded by its schema; Lemmy's `page_cursor` is alpha's next page."""
    raw = {name: request.args[name] for name in LISTED if request.args.get(name) not in (None, '')}
    if 'sort' in raw:
        raw['sort'] = SORTS.get(raw['sort'], raw['sort'])
    data = ListPostsRequest().load(raw, unknown=EXCLUDE)
    if request.args.get('page_cursor'):
        data['page_cursor'] = request.args['page_cursor']
    return data


@bp.route('/api/v3/post/list')
def post_list():
    alpha = get_post_list(_auth(), listing_arguments())
    return jsonify(posts=[shapes.post_view(view) for view in alpha['posts']], next_page=alpha['next_page'])


@bp.route('/api/v3/post')
def one_post():
    """A post by its id, or by one of its comments' (Lemmy's comment_id, for a link to a comment)."""
    post_id = request.args.get('id', type=int)
    if post_id is None and (comment_id := request.args.get('comment_id', type=int)) is not None:
        comment = db.session.get(PostReply, comment_id)
        post_id = comment.post_id if comment is not None else None
    if post_id is None:
        raise Exception('not_found')
    return jsonify(shapes.get_post_response(get_post(_auth(), {'id': post_id})))
