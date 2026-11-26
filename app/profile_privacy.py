"""Which parts of a local user's profile other people see (spec 2026-11-26-profile-privacy).

- Microblog posts (in the local microblogs community) are always shown.
- Community posts (`profile_community_posts`) and comments (`profile_comments`, every comment and reply) only when
  the person opts in.
- Followers (`profile_followers`): `list` (the default), `count` or `none`.

The person themselves and their staff (`view user details` or `administer all users` on them) see everything; so does
everyone of a remote person, who has no such settings. Every other viewer is bound, signed in or not. These narrow what
a profile lists: the visibility, local-only and private-community rules still apply first."""
from sqlalchemy import or_

from app.microblog_feed import microblog_community_ids
from app.models import Post, PostReply, User
from app.permissions import user_access

FOLLOWERS_LIST = 'list'
FOLLOWERS_COUNT = 'count'
FOLLOWERS_NONE = 'none'
FOLLOWERS = (FOLLOWERS_LIST, FOLLOWERS_COUNT, FOLLOWERS_NONE)


def exempt(person: User, viewer_id: int | None) -> bool:
    """Whether `person`'s settings do not bind the viewer `viewer_id` (None: nobody signed in)."""
    if not person.is_local():
        return True
    if not viewer_id:
        return False
    return viewer_id == person.id or user_access('view user details', viewer_id, on=person) or \
        user_access('administer all users', viewer_id, on=person)


def shows_community_posts(person: User, viewer_id: int | None) -> bool:
    return bool(person.profile_community_posts) or exempt(person, viewer_id)


def shows_comments(person: User, viewer_id: int | None) -> bool:
    return bool(person.profile_comments) or exempt(person, viewer_id)


def followers_of(person: User) -> str:
    """The person's followers setting; an unknown value reads as the default."""
    return person.profile_followers if person.profile_followers in FOLLOWERS else FOLLOWERS_LIST


def followers_shown(person: User, viewer_id: int | None) -> str:
    """How the viewer is shown `person`'s followers: FOLLOWERS_LIST, FOLLOWERS_COUNT or FOLLOWERS_NONE."""
    return FOLLOWERS_LIST if exempt(person, viewer_id) else followers_of(person)


def follower_count_shown(person: User, viewer_id: int | None, count: int) -> int:
    """`count`, or 0 where an API must answer a number and the person discloses none."""
    return 0 if followers_shown(person, viewer_id) == FOLLOWERS_NONE else count


def hidden_posts_clause(person: User, viewer_id: int | None):
    """A filter leaving out the posts of `person` the viewer is not shown on their profile, or None when that is none.
    Written to hold for any listing: other authors' posts pass it."""
    if shows_community_posts(person, viewer_id):
        return None
    return or_(Post.user_id != person.id, Post.community_id.in_(microblog_community_ids()))


def hidden_replies_clause(person: User, viewer_id: int | None):
    """As hidden_posts_clause, for comments."""
    if shows_comments(person, viewer_id):
        return None
    return PostReply.user_id != person.id
