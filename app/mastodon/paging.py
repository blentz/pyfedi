"""Mastodon's paging (plan W1.2): `max_id`, `since_id` and `min_id` bound a page, `limit` sizes it (20, at most 40),
and a `Link` header names the next and previous pages, which every app surveyed follows and some follow only (plan W1.0
inventory: no Link header, no second page).

A timeline may draw on posts and comments both. Each source is ordered by (posted_at, table, row id), which the
posted_at index serves, and is cut at the bounds; the page is the top of the sources merged, which is exact, since the
top `limit` of a union lies within the top `limit` of each part. Statuses keep that order; their ids follow it to the
second (app.mastodon.ids)."""
from dataclasses import dataclass
from urllib.parse import urlencode

from flask import request
from sqlalchemy import and_, asc, desc, func, or_

from app.mastodon.ids import bound, kind_of, posted, status_id
from app.models import PostReply

DEFAULT_LIMIT = 20
MAX_LIMIT = 40
_BOUNDS = ('max_id', 'since_id', 'min_id')


@dataclass(frozen=True)
class Window:
    max_id: tuple | None
    since_id: tuple | None
    min_id: tuple | None
    limit: int


def window(args) -> Window:
    """The page asked for by `args`; a bound that is no status id is no bound, a limit that is no number the default."""
    try:
        limit = int(args.get('limit', DEFAULT_LIMIT))
    except ValueError:
        limit = DEFAULT_LIMIT
    return Window(*(bound(args.get(name, '')) for name in _BOUNDS), limit=min(max(limit, 1), MAX_LIMIT))


@dataclass(frozen=True)
class Source:
    """A query of posts or of comments, already filtered for the viewer, and its model."""
    query: object
    model: object


def _posted_column(model):
    # a comment from before posted_at was kept has only created_at (app.mastodon.ids.posted)
    return func.coalesce(model.posted_at, model.created_at) if model is PostReply else model.posted_at


def _kind(model) -> int:
    return 1 if model is PostReply else 0


def _before(model, at: tuple):
    when, kind, row_id = at
    column = _posted_column(model)
    if _kind(model) < kind:
        return column <= when
    if _kind(model) > kind:
        return column < when
    return or_(column < when, and_(column == when, model.id < row_id))


def _after(model, at: tuple):
    when, kind, row_id = at
    column = _posted_column(model)
    if _kind(model) > kind:
        return column >= when
    if _kind(model) < kind:
        return column > when
    return or_(column > when, and_(column == when, model.id > row_id))


def _key(row) -> tuple:
    return posted(row), kind_of(row), row.id


def page(sources: list[Source], win: Window) -> list:
    """The rows of one page, newest first."""
    rows = []
    ascending = win.min_id is not None   # the oldest rows newer than min_id, as Mastodon gives them
    for source in sources:
        query, model = source.query, source.model
        if win.max_id is not None:
            query = query.filter(_before(model, win.max_id))
        if win.since_id is not None:
            query = query.filter(_after(model, win.since_id))
        if win.min_id is not None:
            query = query.filter(_after(model, win.min_id))
        order = asc if ascending else desc
        rows += query.order_by(None).order_by(order(_posted_column(model)), order(model.id)).limit(win.limit).all()
    rows.sort(key=_key, reverse=not ascending)
    rows = rows[:win.limit]
    return rows if not ascending else rows[::-1]


def link_header(rows: list) -> str | None:
    """`Link` naming the next (older) and previous (newer) pages, keeping the request's other arguments."""
    if not rows:
        return None
    kept = [(name, value) for name, value in request.args.items(multi=True) if name not in _BOUNDS]
    base = request.base_url
    older = f'{base}?{urlencode(kept + [("max_id", status_id(rows[-1]))])}'
    newer = f'{base}?{urlencode(kept + [("min_id", status_id(rows[0]))])}'
    return f'<{older}>; rel="next", <{newer}>; rel="prev"'
