"""Who may see a Post or PostReply, by its stored ActivityPub audience (interop D6/D7).

One predicate in three shapes (Python, ORM, raw SQL) so every read surface asks
the same question. No moderator exemption (ruling D19): moderators see
followers-only content through the report queue's snapshot, nowhere else.
"""
import re
from dataclasses import dataclass
from typing import Optional

from flask import current_app
from flask_babel import _
from sqlalchemy import and_, exists, inspect as sa_inspect, or_
from sqlalchemy.orm import aliased

from app import db
from app.constants import POST_STATUS_PUBLISHED, VISIBILITY_FOLLOWERS, VISIBILITY_PUBLIC, VISIBILITY_UNLISTED
from app.local_only import federating_clause
from app.models import Community, Instance, ModLog, Post, PostReply, User, UserFollower
from app.storefront import hosted_instance_ids

OPEN_VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_UNLISTED)


def _follows(viewer_id: int, author_id: int) -> bool:
    return db.session.query(exists().where(
        UserFollower.local_user_id == viewer_id,
        UserFollower.remote_user_id == author_id,
        UserFollower.is_inward.is_(False),
        UserFollower.is_accepted.is_(True))).scalar()


# The @user@instance syntax send_post and send_reply resolve Mentions from (app/mentions.py:mentioned_users_found_by)
MENTION_PATTERN = re.compile(r"@([a-zA-Z0-9_.-]*)@([a-zA-Z0-9_.-]*)\b")


def _parent_author_id(reply) -> Optional[int]:
    """Who a reply answers: its parent reply's author, else its post's author."""
    if reply.parent_id is not None:
        return db.session.query(PostReply.user_id).filter(PostReply.id == reply.parent_id).scalar()
    return db.session.query(Post.user_id).filter(Post.id == reply.post_id).scalar()


def _mentions(obj, viewer_id: int) -> bool:
    """True when `obj`'s body Mentions local user `viewer_id` as send_post/send_reply would resolve it (cc'd): an
    @name@SERVER_NAME match whose name is that user's user_name exactly, as search_for_user looks a local name up."""
    if not obj.body:
        return False
    server = current_app.config['SERVER_NAME']
    names = {match.group(1) for match in MENTION_PATTERN.finditer(obj.body) if match.group(2) == server}
    if not names:
        return False
    viewer = db.session.get(User, viewer_id)
    return viewer is not None and viewer.ap_id is None and viewer.user_name in names


def can_view(obj, viewer_id: Optional[int]) -> bool:
    """A followers-only object is visible to its author, the author's accepted followers and, as Mastodon does, the
    users it was pushed to (R-REPLYVIEW): the author a reply answers, and locally Mentioned users. visible_to_clause
    and visible_to_sql agree on all of it except Mentions, which they cannot resolve: a Mentioned user can open the
    object and is notified of it, but it is not listed for them.

    Local-only content is for logged-in readers only (spec §3.4), so with no viewer it is never visible. The two
    clauses do not apply that rule, since admin pages pass them no viewer too: lists leave it out with
    app.local_only.without_local_only."""
    if obj.local_only and not viewer_id:
        return False
    visibility = obj.visibility if obj.visibility is not None else VISIBILITY_PUBLIC
    if visibility in OPEN_VISIBILITIES:
        return True
    if visibility != VISIBILITY_FOLLOWERS or not viewer_id:
        return False
    if viewer_id == obj.user_id or _follows(viewer_id, obj.user_id):
        return True
    if isinstance(obj, PostReply) and _parent_author_id(obj) == viewer_id:
        return True
    return _mentions(obj, viewer_id)


def is_open(obj) -> bool:
    """True when `obj` may be shown to anyone (public or unlisted). The outbound mirror of the inbound relay gates:
    a followers-only object was never sent to a community's followers, so no activity about it is either. An object
    with no `visibility` (a user, a community) counts as open, as a NULL value does in `can_view`."""
    visibility = getattr(obj, 'visibility', None)
    return visibility is None or visibility in OPEN_VISIBILITIES


def post_withheld_from_peers(post) -> int | None:
    """The status our ActivityPub post routes refuse `post` with, whoever asks: 404 for a local-only post, 403 for
    one in a local-only or private community or not yet published. None when they go on to their other checks.
    community_id is nullable: a post with no community is withheld by none."""
    if post.local_only:
        return 404
    community = post.community
    if community is not None and (community.local_only or community.private):
        return 403
    if post.status < POST_STATUS_PUBLISHED:
        return 403
    return None


def post_served_to_any_peer(post) -> bool:
    """Whether our ActivityPub post route serves `post` to a peer that proves nothing about itself: not withheld
    (`post_withheld_from_peers`), public or unlisted, and not deleted (a deleted one is a Tombstone). A post on one of
    our domains found by URL is shown only then, as it was when that URL was fetched from our own route (plan C4)."""
    # visibility is NOT NULL, so None is only an unflushed row: is_open counts it open, as can_view does
    return post_withheld_from_peers(post) is None and is_open(post) and not post.deleted


def follower_instances(user) -> list:
    """The remote instances holding at least one ACCEPTED follower of local `user`: where a followers-only object of
    theirs is delivered (Amendment B, S7.2). Unlike `User.following_instances`, a pending or refused follow does not
    count, and nor does a follow in the other direction."""
    return (db.session.query(Instance)
            .join(User, User.instance_id == Instance.id)
            .join(UserFollower, UserFollower.remote_user_id == User.id)
            .filter(UserFollower.local_user_id == user.id,
                    UserFollower.is_inward.is_(True),
                    UserFollower.is_accepted.is_(True),
                    Instance.id.not_in(hosted_instance_ids()),
                    Instance.dormant.is_(False),
                    Instance.gone_forever.is_(False))
            .distinct().all())


def followers_only_reply_instances(reply) -> list:
    """Where a local followers-only reply was delivered (Amendment B, S7.3): its author's follower instances, plus
    the instance of the author it replies to when that author is remote. Each instance once."""
    parent = db.session.get(PostReply, reply.parent_id) if reply.parent_id else reply.post
    instances = follower_instances(reply.author)
    parent_instance = parent.author.instance
    if parent_instance is not None and parent_instance.id not in hosted_instance_ids() and parent_instance not in instances:
        instances.append(parent_instance)
    return instances


def followers_only_addressing(author, recipients) -> dict:
    """The `to`, `cc` and `interactionPolicy` of local `author`'s followers-only post or comment.

    One answer for what is pushed (send_post, send_reply: Amendment B, S7.2 and S7.3) and what an entitled signer
    fetches (S6), so the two cannot disagree: to the author's followers, cc each of `recipients` (the users it
    answers or Mentions, never the follower list: R-FOCC), nothing Public, and only the author approves a quote.
    The object carries no community `audience` either; that is for the caller to leave out."""
    return {'to': [author.followers_url()],
            'cc': [recipient.public_url() for recipient in recipients],
            'interactionPolicy': {'canQuote': {'automaticApproval': [author.public_url()]}}}


def post_title_for(post, viewer_id: Optional[int]) -> str:
    """A post's title as a notification, subscription name or page title tells it to `viewer_id`. Someone who may not
    view the post learns that one exists (its link still 404s for them), not what it is called."""
    return post.title if can_view(post, viewer_id) else _('a followers-only post')


def visible_to_clause(model, viewer_id: Optional[int]):
    """ORM twin of can_view for Post or PostReply (or an alias of either); Mentions are not resolved (see can_view)."""
    open_audience = or_(model.visibility.in_(OPEN_VISIBILITIES), model.visibility.is_(None))
    if not viewer_id:
        return open_audience
    follows = exists().where(UserFollower.local_user_id == viewer_id,
                             UserFollower.remote_user_id == model.user_id,
                             UserFollower.is_inward.is_(False),
                             UserFollower.is_accepted.is_(True))
    audience = [model.user_id == viewer_id, follows]
    if sa_inspect(model).mapper.class_ is PostReply:
        parent = aliased(PostReply)
        audience.append(or_(
            and_(model.parent_id.isnot(None),
                 exists().where(parent.id == model.parent_id, parent.user_id == viewer_id)),
            and_(model.parent_id.is_(None),
                 exists().where(Post.id == model.post_id, Post.user_id == viewer_id))))
    return or_(open_audience,
               and_(model.visibility == VISIBILITY_FOLLOWERS, or_(*audience)))


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


def modlog_local_only_clause():
    """ModLog entries that name no local-only post, comment, community or target account: what a reader who is not
    logged in is shown (spec §3.4; accounts and communities by the owner ruling of 2026-10-21), on the web modlog and
    the API's alike."""
    return and_(~exists().where(Post.id == ModLog.post_id, Post.local_only.is_(True)),
                ~exists().where(PostReply.id == ModLog.reply_id, PostReply.local_only.is_(True)),
                ~exists().where(Community.id == ModLog.community_id, ~federating_clause(Community)),
                ~exists().where(User.id == ModLog.target_user_id, ~federating_clause(User)))


def listable_clause(model):
    return model.visibility == VISIBILITY_PUBLIC


def listable_sql(alias: str) -> str:
    return f"{alias}.visibility = 'public'"


def visible_to_sql(alias: str, reply: bool = False) -> str:
    """Raw-SQL twin of visible_to_clause. Binds :visibility_viewer_id (NULL for anonymous). `reply` says `alias` is
    a post_reply row, which its parent's author may also see (R-REPLYVIEW)."""
    answers = (f" OR ({alias}.parent_id IS NOT NULL AND EXISTS (SELECT 1 FROM post_reply vpr "
               f"WHERE vpr.id = {alias}.parent_id AND vpr.user_id = :visibility_viewer_id))"
               f" OR ({alias}.parent_id IS NULL AND EXISTS (SELECT 1 FROM post vp "
               f"WHERE vp.id = {alias}.post_id AND vp.user_id = :visibility_viewer_id))") if reply else ''
    return (f"({alias}.visibility IN ('public', 'unlisted') OR {alias}.visibility IS NULL OR ({alias}.visibility = 'followers' AND "
            f"({alias}.user_id = :visibility_viewer_id OR EXISTS (SELECT 1 FROM user_follower vf "
            f"WHERE vf.local_user_id = :visibility_viewer_id AND vf.remote_user_id = {alias}.user_id "
            f"AND vf.is_inward IS FALSE AND vf.is_accepted IS TRUE){answers})))")


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
