"""What a local user's microblog feed carries (spec 2026-11-21-microblog-feed-setting): the feed Mastodon and other
microblog software see of a person: their outbox, the Mastodon client API's account statuses and home timeline, and
the copies of their posts pushed to their followers.

- `microblog` (the default): only the posts in the local microblogs community.
- `community`: those and the posts in every other community.
- `all`: those and every comment and reply too.

A remote author has no such setting: whatever reached us of theirs is shown. Followers-only content is addressed to the
followers by its author and is not governed by this setting."""
from sqlalchemy import exists, not_, or_, select

from app.constants import COMMUNITY_KIND_MICROBLOGS
from app.models import Community, Post, PostReply, User
from app.visibility import OPEN_VISIBILITIES

FEED_MICROBLOG = 'microblog'
FEED_COMMUNITY = 'community'
FEED_ALL = 'all'
FEEDS = (FEED_MICROBLOG, FEED_COMMUNITY, FEED_ALL)


def feed_of(user) -> str:
    """The user's setting; an unknown value reads as the default."""
    return user.microblog_feed if user.microblog_feed in FEEDS else FEED_MICROBLOG


def shows_community_posts(user) -> bool:
    return feed_of(user) in (FEED_COMMUNITY, FEED_ALL)


def shows_replies(user) -> bool:
    return feed_of(user) == FEED_ALL


def in_microblog_community(community) -> bool:
    """The local microblogs community, by kind as is_local_microblogs: the posts every feed carries."""
    return community.kind == COMMUNITY_KIND_MICROBLOGS and community.ap_id is None


def post_in_feed(user, post) -> bool:
    """Whether the feed of `post`'s local author `user` carries this open post."""
    return shows_community_posts(user) or in_microblog_community(post.community)


def microblog_community_ids():
    """The ids of the local microblogs communities, as a subquery."""
    return select(Community.id).where(Community.kind == COMMUNITY_KIND_MICROBLOGS, Community.ap_id.is_(None))


def _local_author_with(model, feeds: tuple[str, ...]):
    return exists().where(User.id == model.user_id, User.ap_id.is_(None), User.microblog_feed.not_in(feeds))


def _addressed_to_followers(model):
    return model.visibility.not_in(OPEN_VISIBILITIES)


def feed_posts_clause():
    """For any authors: a post is shown unless its author is local, their feed carries microblog posts only, it is
    not in a microblogs community, and it is open (public or unlisted). A value outside FEEDS reads as the default,
    as feed_of reads it."""
    return or_(Post.community_id.in_(microblog_community_ids()), _addressed_to_followers(Post),
               not_(_local_author_with(Post, (FEED_COMMUNITY, FEED_ALL))))


def feed_replies_clause():
    """For any authors: a comment is shown unless its author is local, their feed does not carry replies, and it is
    open."""
    return or_(_addressed_to_followers(PostReply), not_(_local_author_with(PostReply, (FEED_ALL,))))
