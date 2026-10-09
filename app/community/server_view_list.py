"""Server views (/c/microblogs@<host>) as rows of the All Communities list: one UNION ALL of the
filtered community query and a per-instance aggregate of the microblogs community's posts, ordered
and paginated as one list.

Spec: docs/superpowers/specs/2026-10-08-server-views-in-community-list-design.md
"""
from datetime import timedelta

from flask import current_app
from sqlalchemy import Integer, cast, func, literal, not_, null, select, union_all
from sqlalchemy.orm import joinedload

from app import db
from app.community.live import LIVE_COMMUNITY
from app.models import BannedInstances, Community, Instance, Post
from app.utils import instance_banned, utcnow

VIEW_PREFIX = f'{LIVE_COMMUNITY}@'
SORT_FIELDS = ('title', 'subscriptions_count', 'post_count', 'post_reply_count', 'last_active',
               'created_at', 'active_weekly')
DEFAULT_SORT = ('active_weekly', 'desc')   # what safe_order_by falls back to for these fields


class ServerView:
    """A server view as a row of the list. It reads like a Community where the template needs it."""
    is_server_view = True
    id = None
    instance_id = None       # platform_of reads instance_id: a view carries no platform badge
    nsfw = False
    nsfl = False
    subscriptions_count = 0

    def __init__(self, instance, microblogs, post_count, post_reply_count, last_active, created_at,
                 active_weekly):
        self.instance = instance
        self.host = instance.domain.lower()
        self._microblogs = microblogs
        self.post_count = post_count
        self.post_reply_count = post_reply_count
        self.last_active = last_active
        self.created_at = created_at
        self.active_weekly = active_weekly

    def link(self) -> str:
        return f'{VIEW_PREFIX}{self.host}'

    def display_name(self) -> str:
        return self.link()

    def icon_image(self, size='default') -> str:
        return self._microblogs.icon_image(size)


def like_pattern(text: str) -> str:
    """A LIKE pattern matching `text` anywhere, with LIKE's own wildcards taken literally (escape '\\')."""
    escaped = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def parse_sort(sort_by):
    """(field, 'asc'|'desc') for a `sort_by` like 'post_count desc'; DEFAULT_SORT when it is not one."""
    parts = (sort_by or '').strip().split()
    field = parts[0] if parts else ''
    if field not in SORT_FIELDS:
        return DEFAULT_SORT
    direction = parts[1].lower() if len(parts) > 1 else 'asc'
    return field, 'desc' if direction == 'desc' else 'asc'


def view_side_allowed(*, home_select, subscribe_select, topic_id, language_id, feed_id, platform, nsfw) -> bool:
    """False when a filter is set that no server view can satisfy."""
    return not (home_select == 'local' or subscribe_select != 'any' or topic_id or language_id or feed_id
                or platform or nsfw == 'yes')


def community_select(community_query):
    """The filtered community query reduced to the union's columns, unordered."""
    return community_query.order_by(None).with_entities(
        Community.id.label('community_id'), cast(null(), Integer).label('instance_id'),
        Community.title.label('title'), Community.subscriptions_count.label('subscriptions_count'),
        Community.post_count.label('post_count'), Community.post_reply_count.label('post_reply_count'),
        Community.last_active.label('last_active'), Community.created_at.label('created_at'),
        Community.active_weekly.label('active_weekly')).statement


def server_view_select(microblogs_id: int, search: str = '', host: str = '', blocked_instance_ids=(),
                       exclude_keywords=()):
    """One row per server with a server view, aggregated over the microblogs community's posts.
    Wildcard bans are not matched here; paginate_union drops those rows."""
    domain = func.lower(Instance.domain)
    title = func.concat(VIEW_PREFIX, domain)
    week_ago = utcnow() - timedelta(days=7)
    banned = select(func.lower(BannedInstances.domain)).where(BannedInstances.domain.isnot(None))
    real = select(Community.ap_id).where(Community.ap_id.like(f'{VIEW_PREFIX}%'))
    stmt = (select(cast(null(), Integer).label('community_id'), func.min(Post.instance_id).label('instance_id'),
                   title.label('title'), literal(0).label('subscriptions_count'),
                   func.count(Post.id).label('post_count'),
                   func.coalesce(func.sum(Post.reply_count), 0).label('post_reply_count'),
                   func.max(Post.posted_at).label('last_active'), func.min(Post.posted_at).label('created_at'),
                   func.count(Post.id).filter(Post.posted_at > week_ago).label('active_weekly'))
            .select_from(Post)
            .join(Instance, Instance.id == Post.instance_id)
            .where(Post.community_id == microblogs_id, Post.deleted == False, Post.instance_id != 1,
                   domain != current_app.config['SERVER_NAME'].lower(),
                   domain.not_in(banned), title.not_in(real))
            .group_by(domain))
    if search:
        stmt = stmt.where(title.ilike(like_pattern(search), escape='\\'))
    for keyword in exclude_keywords:
        if keyword:
            stmt = stmt.where(not_(title.ilike(like_pattern(keyword), escape='\\')))
    if host:
        stmt = stmt.where(domain == host.strip().lower())
    if blocked_instance_ids:
        stmt = stmt.where(Post.instance_id.not_in(list(blocked_instance_ids)))
    return stmt


class UnionPage:
    """The slice of Flask-SQLAlchemy's Pagination the list route and template read."""

    def __init__(self, items, page, per_page, total):
        self.items = items
        self.page = page
        self.per_page = per_page
        self.total = total

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.per_page < self.total

    @property
    def prev_num(self):
        return self.page - 1 if self.has_prev else None

    @property
    def next_num(self):
        return self.page + 1 if self.has_next else None


def _hydrate(rows, microblogs) -> list:
    community_ids = [row.community_id for row in rows if row.community_id is not None]
    instance_ids = [row.instance_id for row in rows if row.instance_id is not None]
    communities = {c.id: c for c in Community.query.options(joinedload(Community.instance))
                   .filter(Community.id.in_(community_ids))} if community_ids else {}
    instances = {i.id: i for i in Instance.query.filter(Instance.id.in_(instance_ids))} if instance_ids else {}
    items = []
    for row in rows:
        if row.community_id is not None:
            if row.community_id in communities:
                items.append(communities[row.community_id])
            continue
        instance = instances.get(row.instance_id)
        if instance is None or instance_banned(instance.domain.lower()):   # wildcard bans: not matched in SQL
            continue
        items.append(ServerView(instance, microblogs, row.post_count, row.post_reply_count, row.last_active,
                                row.created_at, row.active_weekly))
    return items


def _count(union) -> int:
    return db.session.scalar(select(func.count()).select_from(union))


def paginate_union(community_query, view_select, sort_by, page, per_page, microblogs) -> UnionPage:
    """One page of communities and (when view_select is given) server views, ordered as one list."""
    parts = [community_select(community_query)]
    if view_select is not None:
        parts.append(view_select)
    union = (union_all(*parts) if len(parts) > 1 else parts[0]).subquery()
    field, direction = parse_sort(sort_by)
    key = union.c[field]
    order = [key.desc() if direction == 'desc' else key.asc(), union.c.title, union.c.community_id,
             union.c.instance_id]
    page = max(page, 1)
    rows = db.session.execute(select(union, func.count().over().label('total')).order_by(*order)
                              .limit(per_page).offset((page - 1) * per_page)).all()
    total = rows[0].total if rows else _count(union)   # an empty page (past the end, or no rows) needs its own count
    return UnionPage(_hydrate(rows, microblogs), page, per_page, total)
