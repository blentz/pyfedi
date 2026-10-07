"""The Live view of the microblogs community: new posts arrive without a reload, the way
Mastodon's Live Feeds does.

Spec: docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md
"""
from datetime import timedelta

from flask import current_app
from sqlalchemy import desc

from app.constants import POST_STATUS_REVIEWING, VISIBILITY_PUBLIC
from app.models import Post, post_stored_hooks
from app import utils
from app.utils import utcnow

LIVE_COMMUNITY = 'microblogs'
LIVE_CHANNEL = 'live:microblogs'
# A backfilled post is old but gets a new, high id; this window keeps it out of the stream
# while still admitting a post that federated in a few minutes late.
LIVE_WINDOW = timedelta(hours=1)
LIVE_LIMIT = 40


def is_live_community(community) -> bool:
    return community.name == LIVE_COMMUNITY and community.is_local()


def live_available(community, user, content_type: str, page: int) -> bool:
    return user.is_authenticated and content_type == 'posts' and page == 1 and is_live_community(community)


def live_posts(posts_query, after: int) -> list:
    """Posts from `posts_query` newer than the cursor `after` (a Post.id: local ids only grow,
    unlike a peer's `published`), recent, unpinned, newest first."""
    return posts_query.filter(Post.id > after, Post.posted_at > utcnow() - LIVE_WINDOW,
                              Post.sticky == False).order_by(desc(Post.posted_at)).limit(LIVE_LIMIT).all()


def announce_live_post(post, community, backfill: bool) -> None:
    """Wake every open Live view after a post lands. The payload is empty: each viewer then
    fetches its own filtered posts, so the broadcast reveals nothing a filter would hide."""
    if not current_app.config['NOTIF_SERVER'] or backfill or not is_live_community(community):
        return
    if post.visibility != VISIBILITY_PUBLIC or post.status <= POST_STATUS_REVIEWING or post.deleted:
        return
    try:
        utils.publish_sse_event(LIVE_CHANNEL, '{}')
    except Exception as exc:
        current_app.logger.warning(f'Live feed wake-up not sent: {exc}')


post_stored_hooks.append(announce_live_post)
