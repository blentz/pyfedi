"""Who may see a Post or PostReply, by its stored ActivityPub audience (interop D6/D7).

One predicate in three shapes (Python, ORM, raw SQL) so every read surface asks
the same question. No moderator exemption (ruling D19): moderators see
followers-only content through the report queue's snapshot, nowhere else.
"""
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import and_, exists, or_

from app import db
from app.constants import VISIBILITY_FOLLOWERS, VISIBILITY_PUBLIC, VISIBILITY_UNLISTED
from app.models import UserFollower

OPEN_VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_UNLISTED)


def _follows(viewer_id: int, author_id: int) -> bool:
    return db.session.query(exists().where(
        UserFollower.local_user_id == viewer_id,
        UserFollower.remote_user_id == author_id,
        UserFollower.is_inward.is_(False),
        UserFollower.is_accepted.is_(True))).scalar()


def can_view(obj, viewer_id: Optional[int]) -> bool:
    visibility = obj.visibility if obj.visibility is not None else VISIBILITY_PUBLIC
    if visibility in OPEN_VISIBILITIES:
        return True
    if visibility != VISIBILITY_FOLLOWERS or not viewer_id:
        return False
    return viewer_id == obj.user_id or _follows(viewer_id, obj.user_id)


def visible_to_clause(model, viewer_id: Optional[int]):
    open_audience = or_(model.visibility.in_(OPEN_VISIBILITIES), model.visibility.is_(None))
    if not viewer_id:
        return open_audience
    follows = exists().where(UserFollower.local_user_id == viewer_id,
                             UserFollower.remote_user_id == model.user_id,
                             UserFollower.is_inward.is_(False),
                             UserFollower.is_accepted.is_(True))
    return or_(open_audience,
               and_(model.visibility == VISIBILITY_FOLLOWERS,
                    or_(model.user_id == viewer_id, follows)))


def listable_clause(model):
    return model.visibility == VISIBILITY_PUBLIC


def listable_sql(alias: str) -> str:
    return f"{alias}.visibility = 'public'"


def visible_to_sql(alias: str) -> str:
    """Raw-SQL twin of visible_to_clause. Binds :visibility_viewer_id (NULL for anonymous)."""
    return (f"({alias}.visibility IN ('public', 'unlisted') OR {alias}.visibility IS NULL OR ({alias}.visibility = 'followers' AND "
            f"({alias}.user_id = :visibility_viewer_id OR EXISTS (SELECT 1 FROM user_follower vf "
            f"WHERE vf.local_user_id = :visibility_viewer_id AND vf.remote_user_id = {alias}.user_id "
            f"AND vf.is_inward IS FALSE AND vf.is_accepted IS TRUE))))")


@dataclass(frozen=True)
class RestrictedReply:
    """What a template or serializer gets in place of a reply the viewer may not see (D18).
    Carries tree position only, so no template slip can leak author or body."""
    id: int
    depth: int
    parent_id: Optional[int]
    post_id: int


def mark_restricted(tree: list, viewer_id: Optional[int]) -> list:
    for entry in tree:
        comment = entry['comment']
        if not isinstance(comment, RestrictedReply) and not can_view(comment, viewer_id):
            entry['comment'] = RestrictedReply(comment.id, comment.depth or 0, comment.parent_id, comment.post_id)
            entry['restricted'] = True
        mark_restricted(entry['replies'], viewer_id)
    return tree
