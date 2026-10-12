"""Inbound quote posts (C2, docs/superpowers/specs/2026-10-12-ap-interop-c2-quote-posts-design.md): the local row a
post or comment quotes, and whether the viewer sees it as a card or as a plain link.

Lookups are cached per request in `g.quote_targets`. `prime_quotes`, a before_render_template receiver, fills the
cache for every quoting row in a template's context with one query per table, so a feed of quoting posts does not
look each one up."""
import re

from flask import current_app, g, has_request_context
from flask_login import current_user

from app import db
from app.constants import POST_STATUS_PUBLISHED
from app.models import BannedInstances, Community, Instance, Post, PostReply, QuoteAuthorization
from app.utils import blocked_communities, blocked_or_banned_instances, blocked_users, communities_banned_from, \
    community_membership_private, \
    shorten_string, user_in_restricted_country
from app.visibility import can_view, is_open

_LOCAL_PATH = re.compile(r'/(post|comment)/(\d+)')


def resolve_quotes(ids: set[str]) -> dict:
    """Quoted id -> local Post or PostReply, for those of `ids` this instance has (by ap_id, or a local
    /post/<id> or /comment/<id> URL)."""
    if not ids:
        return {}
    found = {p.ap_id: p for p in db.session.query(Post).filter(Post.ap_id.in_(ids))}
    rest = ids - found.keys()
    if rest:
        found.update({r.ap_id: r for r in db.session.query(PostReply).filter(PostReply.ap_id.in_(rest))})
    prefix = current_app.config['SERVER_URL']
    for quoted_id in ids - found.keys():
        match = _LOCAL_PATH.fullmatch(quoted_id[len(prefix):]) if quoted_id.startswith(prefix) else None
        if match:
            row = db.session.get(Post if match.group(1) == 'post' else PostReply, int(match.group(2)))
            if row is not None:
                found[quoted_id] = row
    return found


def _cache() -> dict:
    # Celery workers and CLI loops keep one app context for life, so only a request gets a cache (g dies with it).
    if not has_request_context():
        return {}
    if 'quote_targets' not in g:
        g.quote_targets = {}
    return g.quote_targets


def _quoting_rows(context: dict) -> list:
    rows, stack, seen = [], list(context.values()), set()
    while stack:
        value = stack.pop()
        if isinstance(value, (Post, PostReply)):
            if value.quote_ap_id:
                rows.append(value)
        elif isinstance(value, (list, tuple, dict)) or isinstance(getattr(value, 'items', None), list):
            if id(value) in seen:
                continue
            seen.add(id(value))
            if isinstance(value, dict):
                if 'comment' in value:  # a comment-tree node: {'comment': row, 'replies': [...]}
                    stack.append(value['comment'])
                    stack.extend(value.get('replies') or [])
            elif isinstance(value, (list, tuple)):
                stack.extend(value)
            else:  # a Pagination: its rows are already loaded
                stack.extend(value.items)
    return rows


def prime_quotes(sender, template, context, **extra):
    cache = _cache()
    missing = {row.quote_ap_id for row in _quoting_rows(context)} - cache.keys()
    if missing:
        found = resolve_quotes(missing)
        cache.update({quoted_id: found.get(quoted_id) for quoted_id in missing})


def quote_target(item):
    cache = _cache()
    if item.quote_ap_id not in cache:
        cache[item.quote_ap_id] = resolve_quotes({item.quote_ap_id}).get(item.quote_ap_id)
    return cache[item.quote_ap_id]


def _shows_card(item, q, user) -> bool:
    uid = int(user.get_id() or 0)
    post = q if isinstance(q, Post) else q.post
    if q.deleted or post.deleted:
        return False
    community = db.session.get(Community, q.community_id)
    if community is None or community.is_gone() or community.banned or community.id in blocked_communities(uid) \
            or community.id in communities_banned_from(uid):
        return False
    if community.private and community.id not in community_membership_private(uid):  # show_post: 403 otherwise
        return False
    if not can_view(q, uid or None) or not can_view(post, uid or None):  # a comment is only as visible as its post
        return False
    if q.user_id in blocked_users(uid) or q.instance_id in blocked_or_banned_instances(uid):
        return False
    instance = db.session.get(Instance, q.instance_id) if q.instance_id else None
    if instance is not None and db.session.query(BannedInstances.id).filter_by(domain=instance.domain).first():
        return False
    if post.status < POST_STATUS_PUBLISHED and uid != post.user_id:  # refuse_unpublished_post: the author's alone
        return False
    for flag in ('nsfw', 'nsfl'):  # R4: only a signed-in viewer set to Show sees a sensitive card
        if getattr(post, flag) and (not uid or getattr(user, 'hide_' + flag) != 0 or user_in_restricted_country(user)):
            return False
    if q.author.is_local() and not is_open(q):  # spec ruling 2: our own object needs our approval
        column = {'post_id': q.id} if isinstance(q, Post) else {'post_reply_id': q.id}
        if db.session.query(QuoteAuthorization.id).filter_by(quoting_uri=item.ap_id, **column).first() is None:
            return False
    return True


def quote_state(item, user=None) -> dict | None:
    """How `item`'s quote shows to `user` (default: the request's viewer): None when it quotes nothing."""
    if not getattr(item, 'quote_ap_id', None):
        return None
    user = current_user if user is None else user
    q = quote_target(item)
    if q is None or not _shows_card(item, q, user):
        return {'kind': 'line', 'url': item.quote_ap_id, 'target': None, 'href': None, 'author': None,
                'community': None, 'text': None, 'posted_at': None}
    is_post = isinstance(q, Post)
    return {'kind': 'card', 'url': item.quote_ap_id, 'target': q,
            'href': f'/post/{q.id}' if is_post else f'/post/{q.post_id}#comment_{q.id}',
            'author': q.author, 'community': db.session.get(Community, q.community_id),
            'text': (q.title if is_post and q.title else shorten_string(q.body or '', 200)),
            'posted_at': q.posted_at}
