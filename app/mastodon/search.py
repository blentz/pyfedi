"""Mastodon's search (v2; plan W1.2): accounts, statuses and hashtags matching `q`, each narrowed by `type`, sized by
`limit` and paged by `offset`. Accounts come from the people listing (app.reads.users: no account hidden here, none
that opted out of the directory), statuses from the post and comment listings' full-text search (indexable ones only,
with the viewer's blocks and memberships), hashtags by prefix. `resolve` is not acted on: nothing is fetched from
another server here."""
from flask import g, jsonify, request

from app.mastodon import bp
from app.mastodon.paging import DEFAULT_LIMIT, MAX_LIMIT
from app.mastodon.statuses import Page
from app.models import PostReply, Tag
from app.oauth_server.protect import require_oauth
from app.reads import users as reads_users
from app.reads.posts import KEYSET_PAGES, Listing, post_listing
from app.reads.replies import CommentListing, comment_listing
from app.reads.viewer import Restrictions
from app.storefront import current_storefront, domain_base_url

_TYPES = ('accounts', 'statuses', 'hashtags')
_MAX_OFFSET = 10_000


def _number(name: str, default: int, highest: int) -> int:
    value = request.args.get(name, '')
    return min(int(value), highest) if value.isdigit() else default


def _accounts(q: str, viewer_id, limit: int, offset: int, page: Page) -> list[dict]:
    people = reads_users.person_listing(viewer_id, reads_users.People(search=q.lstrip('@')))
    return [page.account(person) for person in people.offset(offset).limit(limit)]


def _statuses(q: str, viewer_id, limit: int, offset: int) -> list:
    restrictions = Restrictions(viewer_id)
    posts = post_listing(g.oauth_user, restrictions, Listing(sort='New', query=q, ignore_sticky=True), KEYSET_PAGES)
    comments = comment_listing(viewer_id, restrictions, CommentListing(query=q, sort='New')).replies
    wanted = offset + limit
    rows = posts.posts.limit(wanted).all() + comments.filter(PostReply.indexable == True).limit(wanted).all()
    rows.sort(key=lambda row: (row.posted_at or row.created_at, row.id), reverse=True)
    return rows[offset:wanted]


def _hashtags(q: str, limit: int, offset: int) -> list[dict]:
    base = domain_base_url(current_storefront())
    tags = Tag.query.filter(Tag.name.ilike(f"{q.lstrip('#').lower()}%"), Tag.banned == False). \
        order_by(Tag.post_count.desc(), Tag.name).offset(offset).limit(limit)
    return [{'name': tag.name, 'url': f'{base}/tag/{tag.name}', 'history': [], 'following': False} for tag in tags]


@bp.route('/api/v2/search')
@require_oauth('read:search', optional=True)
def search():
    q = request.args.get('q', '').strip()
    kind = request.args.get('type', '')
    limit = max(_number('limit', DEFAULT_LIMIT, MAX_LIMIT), 1)
    offset = _number('offset', 0, _MAX_OFFSET)
    answer = {'accounts': [], 'statuses': [], 'hashtags': []}
    if not q:
        return jsonify(answer)
    viewer_id = g.oauth_user.id if g.oauth_user else None
    wanted = (kind,) if kind in _TYPES else _TYPES
    statuses = _statuses(q, viewer_id, limit, offset) if 'statuses' in wanted else []
    page = Page(g.oauth_user, statuses)
    if 'accounts' in wanted:
        answer['accounts'] = _accounts(q, viewer_id, limit, offset, page)
    answer['statuses'] = page.statuses(statuses)
    if 'hashtags' in wanted:
        answer['hashtags'] = _hashtags(q, limit, offset)
    return jsonify(answer)
