"""Comments in Lemmy's API (spec 2026-11-10-lemmy-v3-design §2, §3): a post's thread, or a comment's, over alpha's
listing, so what a person sees is what alpha shows them, a comment they may not see a stub in its place."""
from flask import current_app, jsonify, request
from marshmallow import EXCLUDE

from app.api.alpha.schema import ListCommentsRequest
from app.api.alpha.utils.reply import get_reply_list
from app.lemmy import arguments, bp, shapes

LISTED = ('type_', 'sort', 'max_depth', 'page', 'limit', 'community_id', 'post_id', 'parent_id', 'saved_only',
          'liked_only')
# Jerboa asks for a thread by depth with no limit, as Lemmy answers with up to 300 comments; alpha's pages are
# PAGE_LENGTH long, so that is read a page at a time, to at most this many
WHOLE_THREAD = 300


@bp.route('/api/v3/comment/list')
def comment_list():
    raw = {name: request.args[name] for name in LISTED if request.args.get(name) not in (None, '')}
    data = ListCommentsRequest().load(raw, unknown=EXCLUDE)
    if 'max_depth' not in data or 'limit' in data or 'page' in data:
        comments = get_reply_list(arguments.auth(), data)['comments']
    else:
        page_length, comments, page = current_app.config['PAGE_LENGTH'], [], 1
        while True:
            alpha = get_reply_list(arguments.auth(), {**data, 'limit': page_length, 'page': page})
            comments += alpha['comments']
            if alpha['next_page'] is None or len(comments) >= WHOLE_THREAD:
                break
            page += 1
        comments = comments[:WHOLE_THREAD]
    return jsonify(comments=[shapes.comment_view(view) for view in comments])
