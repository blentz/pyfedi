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

from app.discovery import MEDIA_SOFTWARE

LIVE_COMMUNITY = 'microblogs'
LIVE_KEY_PATTERN = r'^(any|local|popular|media|community:\d+)$'
# A backfilled post is old but gets a new, high id; this window keeps it out of the stream
# while still admitting a post that federated in a few minutes late.
LIVE_WINDOW = timedelta(hours=1)
LIVE_LIMIT = 40
HOME_LIVE_FILTERS = ('subscribed', 'local', 'popular', 'media', 'all')


def _software(community) -> str:
    """The community's instance software, lower-cased; '' when the instance row is missing (D1007)."""
    instance = community.instance
    return (instance.software or '').lower() if instance is not None else ''


def is_live_community(community) -> bool:
    """A `microblogs` community that is local, or hosted on another PieFed instance."""
    if community.name != LIVE_COMMUNITY:
        return False
    return community.is_local() or _software(community) == 'piefed'


def live_available(community, user, content_type: str, page: int) -> bool:
    return user.is_authenticated and content_type == 'posts' and page == 1 and is_live_community(community)


def home_live_available(user, view_filter: str, page: int, tag: str) -> bool:
    return user.is_authenticated and view_filter in HOME_LIVE_FILTERS and page == 0 and not tag


def home_live_key(view_filter: str) -> str:
    """Subscribed includes followed authors in any community, so it wakes on every post, as All does."""
    return 'any' if view_filter in ('subscribed', 'all') else view_filter


def live_posts(posts_query, after: int) -> list:
    """Posts from `posts_query` newer than the cursor `after` (a Post.id: local ids only grow,
    unlike a peer's `published`), recent, unpinned, newest first."""
    # More than LIVE_LIMIT new posts: the newest LIVE_LIMIT come back and the cursor moves past
    # the rest (by design: show the latest).
    return posts_query.filter(Post.id > after, Post.posted_at > utcnow() - LIVE_WINDOW,
                              Post.sticky == False).order_by(desc(Post.posted_at)).limit(LIVE_LIMIT).all()


def live_feed_keys(post, community, backfill: bool) -> set:
    """The Live feeds a stored post belongs to (spec Amendment A, "Wake-up keys")."""
    if backfill or post.visibility != VISIBILITY_PUBLIC or post.status <= POST_STATUS_REVIEWING or post.deleted:
        return set()
    keys = {'any'}
    if is_live_community(community):
        keys.add(f'community:{community.id}')
    if community.instance_id == 1 and community.name != LIVE_COMMUNITY:
        keys.add('local')
    if community.show_popular:
        keys.add('popular')
    if _software(community) in MEDIA_SOFTWARE:
        keys.add('media')
    return keys


def announce_live_post(post, community, backfill: bool) -> None:
    """Wake every open Live view this post belongs to. Payloads are empty: each viewer then
    fetches its own filtered posts, so a broadcast reveals nothing a filter would hide."""
    if not current_app.config['NOTIF_SERVER']:
        return
    keys = sorted(live_feed_keys(post, community, backfill))
    if not keys:
        return
    try:
        pipe = utils.get_redis_connection().pipeline()
        for key in keys:
            pipe.publish(f'live:{key}', '{}')
        pipe.execute()
    except Exception as exc:
        current_app.logger.warning(f'Live feed wake-up not sent: {exc}')


post_stored_hooks.append(announce_live_post)
