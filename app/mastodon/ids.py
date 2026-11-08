"""Status ids (plan W1.2). A post and a comment are both statuses, but come from two tables with their own id
sequences, so a status id is a snowflake, as Mastodon's own are: the second since 2000 at which it was posted
(`posted_at`, the original server's time, as Mastodon keys a remote status), above one bit for the table and the whole
row id (31 bits, all an Integer column holds). Ids grow with time whichever table they come from (Tusky orders its home
timeline by id), fit in 63 bits until 2068, and name exactly one row: the time a remote server claims for its posts
moves where they sort, never which row an id finds."""
from datetime import datetime, timedelta

from app import db
from app.models import Post, PostReply

EPOCH = datetime(2000, 1, 1)
_ROW_BITS = 31
_ROW_MASK = (1 << _ROW_BITS) - 1
_MAX_SECONDS = (1 << (63 - _ROW_BITS - 1)) - 1
_TABLES = (Post, PostReply)   # the bit above the row id: 0 a post, 1 a comment


def _seconds(row) -> int:
    """Seconds since 2000 it was posted, held to what fits: a claimed time before 2000 or after 2068 sorts at an end."""
    return min(max(int((posted(row) - EPOCH).total_seconds()), 0), _MAX_SECONDS)


def status_id(row: Post | PostReply) -> str:
    return str(_seconds(row) << (_ROW_BITS + 1) | kind_of(row) << _ROW_BITS | row.id)


def _parse(raw: str) -> int | None:
    if not raw or not raw.isdigit() or len(raw) > 19:
        return None
    value = int(raw)
    return value if 0 < value < 2 ** 63 else None


def moment(raw: str) -> datetime | None:
    """The second the status `raw` names was posted: a paging bound, whether or not the row is there."""
    value = _parse(raw)
    return EPOCH + timedelta(seconds=value >> (_ROW_BITS + 1)) if value is not None else None


def kind_of(row) -> int:
    """0 for a post, 1 for a comment: where a status of each table sorts among those posted the same instant."""
    return 1 if isinstance(row, PostReply) else 0


def posted(row) -> datetime:
    return row.posted_at or row.created_at


def bound(raw: str, may_read) -> tuple | None:
    """Where the status `raw` names sorts, (posted, table, row id), for paging past it: exact when its row is one the
    viewer may read (`may_read(row)`), else to the second its id holds, as for a status since deleted. A row the viewer
    may not read never moves the bound, so a page cannot tell them it exists or when it was posted. None for no id."""
    value = _parse(raw)
    if value is None:
        return None
    row = find_status(raw)
    if row is not None and may_read(row):
        return posted(row), kind_of(row), row.id
    return moment(raw), value >> _ROW_BITS & 1, value & _ROW_MASK


def find_status(raw: str) -> Post | PostReply | None:
    """The post or comment `raw` names, deleted or not (the caller judges what a viewer may see), or None. Found by
    its row id: the time above it only orders ids."""
    value = _parse(raw)
    if value is None or not value & _ROW_MASK:
        return None
    return db.session.get(_TABLES[value >> _ROW_BITS & 1], value & _ROW_MASK)
