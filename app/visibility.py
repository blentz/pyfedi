"""Who may see a Post or PostReply, by its stored ActivityPub audience (interop D6/D7).

One predicate in three shapes (Python, ORM, raw SQL) so every read surface asks
the same question. No moderator exemption (ruling D19): moderators see
followers-only content through the report queue's snapshot, nowhere else.
"""
from dataclasses import dataclass
from typing import Optional

from flask_babel import _
from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import aliased

from app import db
from app.constants import VISIBILITY_FOLLOWERS, VISIBILITY_PUBLIC, VISIBILITY_UNLISTED
from app.models import ModLog, Post, PostReply, UserFollower

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


def is_open(obj) -> bool:
    """True when `obj` may be shown to anyone (public or unlisted). The outbound mirror of the inbound relay gates:
    a followers-only object was never sent to a community's followers, so no activity about it is either. An object
    with no `visibility` (a user, a community) counts as open, as a NULL value does in `can_view`."""
    visibility = getattr(obj, 'visibility', None)
    return visibility is None or visibility in OPEN_VISIBILITIES


def post_title_for(post, viewer_id: Optional[int]) -> str:
    """A post's title as a notification, subscription name or page title tells it to `viewer_id`. Someone who may not
    view the post learns that one exists (its link still 404s for them), not what it is called."""
    return post.title if can_view(post, viewer_id) else _('a followers-only post')


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


def _open(model):
    return or_(model.visibility.in_(OPEN_VISIBILITIES), model.visibility.is_(None))


def modlog_open_clause():
    """ModLog entries whose post, reply and reply's post are all open to everyone. For a reader who is not an
    admin, an entry about anything else names no target user, so a filter by target user must not match it (R3)."""
    reply_post = aliased(Post)
    hidden_post = exists().where(Post.id == ModLog.post_id, ~_open(Post))
    hidden_reply = exists().where(PostReply.id == ModLog.reply_id,
                                  or_(~_open(PostReply),
                                      exists().where(reply_post.id == PostReply.post_id, ~_open(reply_post))))
    return and_(~hidden_post, ~hidden_reply)


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
    path: tuple = ()  # ancestor ids from the root, as PostReply.path, so a serializer needs no DB read


def mark_restricted(tree: list, viewer_id: Optional[int], parent_path: tuple = ()) -> list:
    for entry in tree:
        comment = entry['comment']
        if not isinstance(comment, RestrictedReply) and not can_view(comment, viewer_id):
            if comment.path:
                path = tuple(comment.path)
            elif parent_path:
                path = parent_path + (comment.id,)
            else:
                path = (0, comment.parent_id, comment.id) if comment.parent_id else (0, comment.id)
            entry['comment'] = RestrictedReply(comment.id, comment.depth or 0, comment.parent_id, comment.post_id, path)
            entry['restricted'] = True
        shown = entry['comment']
        mark_restricted(entry['replies'], viewer_id, tuple(shown.path) if shown.path else ())
    return tree
