"""Mastodon's timelines, a status and its thread (plan W1.2; spec 2026-11-07-mastodon-client-api-design §3.3,
rulings M2-M5). Every list of posts or comments comes from the shared read layer (app.reads), as the alpha API's do,
so a viewer is shown here exactly what their blocks, memberships and visibility allow there.

- public: the posts of the communities shown in All; `local` this domain's communities, `remote` the others.
- home (M2): the posts and comments of the people the viewer follows, and their own.
- list/:id (M2): a community the viewer joined is a list; its timeline is the community's posts.
- tag/:name: posts carrying the hashtag.
- accounts/:id/statuses: a person's posts and, unless `exclude_replies`, comments."""
from flask import g, jsonify, request
from sqlalchemy import and_
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE
from app.local_only import hidden_here
from app.mastodon import bp
from app.mastodon.ids import find_status
from app.mastodon.paging import Source, link_header, page, window
from app.mastodon.statuses import Page
from app.models import Community, CommunityMember, Post, PostReply, Tag, UserFollower
from app.oauth_server.protect import require_oauth
from app.reads import post as reads_post
from app.reads import users as reads_users
from app.reads.posts import KEYSET_PAGES, Listing, post_listing
from app.reads.replies import WHOLE_THREAD, CommentListing, comment_listing, reply_tree, thread_replies
from app.reads.viewer import Restrictions
from app.storefront import hosted_instance_ids
from app.visibility import can_view

_PG_INT_MAX = 2 ** 31 - 1
_MEDIA = (POST_TYPE_IMAGE, POST_TYPE_GALLERY)


def _not_found():
    return jsonify({'error': 'Record not found'}), 404


def _truthy(name: str) -> bool:
    return request.args.get(name, '').lower() in ('1', 'true')


def _viewer_id() -> int | None:
    return g.oauth_user.id if g.oauth_user else None


def _restrictions() -> Restrictions:
    return Restrictions(_viewer_id())


def _posts(listing: Listing):
    return post_listing(g.oauth_user, _restrictions(), listing, KEYSET_PAGES).posts


def _comments(listing: CommentListing):
    return comment_listing(_viewer_id(), _restrictions(), listing).replies


def _respond(sources: list[Source]):
    rows = page(sources, window(request.args))
    response = jsonify(Page(g.oauth_user, rows).statuses(rows))
    if (link := link_header(rows)) is not None:
        response.headers['Link'] = link
    return response


def _only_media(query):
    return query.filter(Post.type.in_(_MEDIA)) if _truthy('only_media') else query


@bp.route('/api/v1/timelines/public')
@require_oauth('read:statuses', optional=True)
def public_timeline():
    posts = _posts(Listing(type='Local' if _truthy('local') else 'All', sort='', ignore_sticky=True))
    if _truthy('remote'):
        posts = posts.filter(Post.instance_id.not_in(hosted_instance_ids()))
    return _respond([Source(_only_media(posts), Post)])


@bp.route('/api/v1/timelines/tag/<tag>')
@require_oauth('read:statuses', optional=True)
def tag_timeline(tag):
    posts = _posts(Listing(sort='', ignore_sticky=True)).filter(
        Post.tags.any(and_(Tag.name == tag.lower().lstrip('#'), Tag.banned == False)))
    return _respond([Source(_only_media(posts), Post)])


def _followed_ids(user_id: int) -> tuple[int, ...]:
    rows = db.session.query(UserFollower.remote_user_id).filter(UserFollower.local_user_id == user_id,
                                                                UserFollower.is_inward.is_(False),
                                                                UserFollower.is_accepted.is_(True))
    return tuple({row[0] for row in rows} | {user_id})


@bp.route('/api/v1/timelines/home')
@require_oauth('read:statuses')
def home_timeline():
    people = _followed_ids(g.oauth_user.id)
    return _respond([Source(_posts(Listing(sort='', person_ids=people, ignore_sticky=True)), Post),
                     Source(_comments(CommentListing(person_ids=people)), PostReply)])


def _joined(user_id: int, community_id: int) -> Community | None:
    if community_id > _PG_INT_MAX:
        return None
    community = db.session.query(Community).join(CommunityMember, CommunityMember.community_id == Community.id).filter(
        CommunityMember.user_id == user_id, CommunityMember.is_banned == False, Community.id == community_id).first()
    return community if community is not None and not hidden_here(community, True) else None


@bp.route('/api/v1/timelines/list/<int:list_id>')
@require_oauth('read:lists')
def list_timeline(list_id):
    if _joined(g.oauth_user.id, list_id) is None:
        return _not_found()
    return _respond([Source(_posts(Listing(sort='', community_id=list_id, ignore_sticky=True)), Post)])


@bp.route('/api/v1/accounts/<int:account_id>/statuses')
@require_oauth('read:statuses', optional=True)
def account_statuses(account_id):
    if account_id > _PG_INT_MAX:
        return _not_found()
    try:
        person = reads_users.person_by_id(account_id, g.oauth_user is not None, reads_users.BY_NAME)
    except NoResultFound:
        return _not_found()
    if _truthy('pinned'):
        return jsonify([])   # a profile pins nothing
    sources = [Source(_only_media(_posts(Listing(sort='', person_id=person.id, ignore_sticky=True))), Post)]
    if not _truthy('exclude_replies') and not _truthy('only_media'):
        sources.append(Source(_comments(CommentListing(person_id=person.id)), PostReply))
    return _respond(sources)


def readable(row, viewer_id: int | None) -> bool:
    """Whether the viewer may open this post or comment: not deleted, not hidden here, visible to them, and not in a
    private community they do not belong to (the read layer's refusals; refused and absent are one answer)."""
    if row is None or row.deleted or hidden_here(row, viewer_id is not None):
        return False
    try:
        if isinstance(row, Post):
            reads_post.refuse_unreadable_post(row, viewer_id, Restrictions(viewer_id))
        else:
            if not can_view(row, viewer_id):
                return False
            reads_post.refuse_private_thread(db.session.get(Community, row.community_id), viewer_id)
            return readable(row.post, viewer_id)
    except Exception:
        return False
    return True


def _status_row(raw: str):
    row = find_status(raw)
    return row if readable(row, _viewer_id()) else None


@bp.route('/api/v1/statuses/<status_id>')
@require_oauth('read:statuses', optional=True)
def get_status(status_id):
    row = _status_row(status_id)
    if row is None:
        return _not_found()
    return jsonify(Page(g.oauth_user, [row]).statuses([row])[0])


def _flatten(entries: list[dict]) -> list:
    rows = []
    for entry in entries:
        rows.append(entry['comment'])
        rows += _flatten(entry['replies'])
    return rows


def _ancestors(reply: PostReply, viewer_id) -> list:
    chain = []
    parent_id = reply.parent_id
    while parent_id is not None:
        parent = db.session.get(PostReply, parent_id)
        if parent is None:
            break
        chain.append(parent)
        parent_id = parent.parent_id
    return [reply.post] + [row for row in reversed(chain) if readable(row, viewer_id)]


@bp.route('/api/v1/statuses/<status_id>/context')
@require_oauth('read:statuses', optional=True)
def status_context(status_id):
    """Ancestors from the post down, descendants in thread order: the comments the viewer may read whose every parent
    they may read too (a reply to one they cannot see would hang from nothing)."""
    row = _status_row(status_id)
    if row is None:
        return _not_found()
    viewer_id = _viewer_id()
    post = row if isinstance(row, Post) else row.post
    thread = [reply for reply in thread_replies(post, g.oauth_user, _restrictions(), 'old', WHOLE_THREAD)
              if not reply.deleted and can_view(reply, viewer_id)]
    tree = reply_tree(thread)
    if isinstance(row, PostReply):
        found = next((entry for entry in _depth_first(tree) if entry['comment'].id == row.id), None)
        descendants = _flatten(found['replies']) if found is not None else []
        ancestors = _ancestors(row, viewer_id)
    else:
        descendants, ancestors = _flatten(tree), []
    statuses = Page(g.oauth_user, ancestors + descendants).statuses(ancestors + descendants)
    return jsonify({'ancestors': statuses[:len(ancestors)], 'descendants': statuses[len(ancestors):]})


def _depth_first(entries: list[dict]):
    """Every entry of the tree, depth first."""
    for entry in entries:
        yield entry
        yield from _depth_first(entry['replies'])
