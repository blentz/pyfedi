"""A community, a person and search in Lemmy's API (spec 2026-11-10-lemmy-v3-design §2), over alpha's handlers."""
from flask import jsonify, request
from marshmallow import EXCLUDE

from app.api.alpha.schema import GetCommunityRequest, GetUserRequest, SearchRequest
from app.api.alpha.utils.community import get_community
from app.api.alpha.utils.misc import get_search
from app.api.alpha.utils.user import get_user
from app.lemmy import arguments, bp, shapes
from app.lemmy.posts import SORTS

SEARCHED = ('Communities', 'Posts', 'Users', 'Comments')   # what Lemmy's All searches, each a list of its own
SEARCH_NAMES = ('q', 'type_', 'limit', 'listing_type', 'page', 'sort', 'community_name', 'community_id')


def _query(schema, names: tuple, given: dict | None = None) -> dict:
    """`names` of the query (or of `given`) under alpha's sort names, loaded by alpha's schema for the request."""
    given = request.args.to_dict() if given is None else given
    raw = {name: given[name] for name in names if given.get(name) not in (None, '')}
    if 'sort' in raw:
        raw['sort'] = SORTS.get(raw['sort'], raw['sort'])
    return schema().load(raw, unknown=EXCLUDE)


@bp.route('/api/v3/community')
def community():
    alpha = get_community(arguments.auth(), _query(GetCommunityRequest, ('id', 'name')))
    return jsonify(community_view=shapes.community_view(alpha['community_view']),
                   moderators=[shapes.moderator_view(row) for row in alpha['moderators']], discussion_languages=[])


@bp.route('/api/v3/user')
def person():
    """A person with their posts and comments, which Lemmy always lists (alpha's include_content)."""
    data = _query(GetUserRequest, ('person_id', 'username', 'sort', 'page', 'limit', 'community_id', 'saved_only'))
    alpha = get_user(arguments.auth(), {**data, 'include_content': True})
    return jsonify(person_view=shapes.person_view(alpha['person_view']),
                   comments=[shapes.comment_view(view) for view in alpha['comments']],
                   posts=[shapes.post_view(view) for view in alpha['posts']],
                   moderates=[shapes.moderator_view(row) for row in alpha['moderates']])


@bp.route('/api/v3/search')
def search():
    searched = request.args.get('type_') or 'All'
    found = {'type_': searched, 'comments': [], 'posts': [], 'communities': [], 'users': []}
    for kind in (SEARCHED if searched == 'All' else (searched,)):
        data = _query(SearchRequest, SEARCH_NAMES, {**request.args.to_dict(), 'type_': kind})
        alpha = get_search(arguments.auth(), data)
        found['comments'] += [shapes.comment_view(view) for view in alpha['comments']]
        found['posts'] += [shapes.post_view(view) for view in alpha['posts']]
        found['communities'] += [shapes.community_view(view) for view in alpha['communities']]
        found['users'] += [shapes.person_view(view) for view in alpha['users']]
    return jsonify(found)
