"""Status ids (plan W1.2). A post and a comment are both statuses, but come from two tables with their own id
sequences, so a status id is a snowflake, as Mastodon's own are: the milliseconds since 1970 at which it was posted
(`posted_at`, the original server's time, as Mastodon keys a remote status), above one bit for the table and fifteen
of the row's id. Ids grow with time whichever table they come from (Tusky orders its home timeline by id), fit in 63
bits, and decode back to their row without a table of their own."""
from datetime import datetime, timedelta

from sqlalchemy import and_, or_

from app.models import Post, PostReply

EPOCH = datetime(1970, 1, 1)
MILLISECOND = timedelta(milliseconds=1)
_ROW_BITS = 15
_ROW_MASK = (1 << _ROW_BITS) - 1
_TABLES = (Post, PostReply)   # bit 15: 0 a post, 1 a comment


def _posted(row) -> datetime:
    return row.posted_at or row.created_at


def status_id(row: Post | PostReply) -> str:
    milliseconds = (_posted(row) - EPOCH) // MILLISECOND
    kind = 1 if isinstance(row, PostReply) else 0
    return str(milliseconds << 16 | kind << _ROW_BITS | row.id & _ROW_MASK)


def _parse(raw: str) -> int | None:
    if not raw or not raw.isdigit() or len(raw) > 19:
        return None
    value = int(raw)
    return value if 0 < value < 2 ** 63 else None


def moment(raw: str) -> datetime | None:
    """When the status `raw` names was posted, to the millisecond: a paging bound, whether or not the row is there."""
    value = _parse(raw)
    return EPOCH + (value >> 16) * MILLISECOND if value is not None else None


def find_status(raw: str) -> Post | PostReply | None:
    """The post or comment `raw` names, deleted or not (the caller judges what a viewer may see), or None."""
    value = _parse(raw)
    if value is None:
        return None
    model = _TABLES[value >> _ROW_BITS & 1]
    start = EPOCH + (value >> 16) * MILLISECOND
    end = start + MILLISECOND
    posted = or_(and_(model.posted_at >= start, model.posted_at < end),
                 and_(model.posted_at.is_(None), model.created_at >= start, model.created_at < end))
    return model.query.filter(posted, model.id.op('&')(_ROW_MASK) == value & _ROW_MASK).first()
