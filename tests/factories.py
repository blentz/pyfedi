"""Builders for database rows used by the boost ingestion tests.

Each builder sets the minimum needed for a valid row and commits, so the object
has an id. If Postgres rejects an insert for a missing NOT NULL column, add that
column here rather than in the test.
"""

from app import db
from app.models import Community, Instance, Post, Site, User, UserFollower, utcnow


def make_instance(domain: str, software: str = 'mastodon') -> Instance:
    instance = Instance(domain=domain, software=software)
    db.session.add(instance)
    db.session.commit()
    return instance


def make_user(instance, name: str, local: bool = False) -> User:
    """A local user has ap_id None; a remote user has a full actor URI."""
    user = User(
        user_name=name,
        email=f'{name}@example.com',
        instance_id=instance.id if instance else 1,
        verified=True,
        banned=False,
        ap_id=None if local else f'{name}@{instance.domain}',
        ap_profile_id=None if local else f'https://{instance.domain}/users/{name}',
        ap_public_url=None if local else f'https://{instance.domain}/users/{name}',
        ap_inbox_url=None if local else f'https://{instance.domain}/users/{name}/inbox',
    )
    db.session.add(user)
    db.session.commit()
    return user


def make_community(name: str = 'microblogs') -> Community:
    community = Community(
        name=name,
        title=name,
        instance_id=1,
        user_id=1,
        ap_profile_id=f'https://test.piefed.local/c/{name}',
        ap_public_url=f'https://test.piefed.local/c/{name}',
        ap_followers_url=f'https://test.piefed.local/c/{name}/followers',
        ap_domain='test.piefed.local',
        subscriptions_count=0,
        local_only=False,
        nsfw=False,
    )
    db.session.add(community)
    db.session.commit()
    return community


def make_post(community, user, ap_id: str, title: str = 'a post', private: bool = False,
              microblog: bool = False) -> Post:
    """Build a Post.

    microblog=True reproduces the columns Post.new() sets for a Mastodon Note with
    no 'name': title='', private=True, and microblog=True. It does NOT reproduce
    Post.new()'s activity-level Public check that can clear private back to False
    for a genuinely unlisted post (app/models.py ~1796-1807) -- private here is
    exactly the object-titleless default, nothing more. status is left at the
    column default (POST_STATUS_PUBLISHED = 1), which already matches what
    Post.new() implicitly leaves it at, since Post.new() never sets status itself.

    Post.private is an unlisted marker, NOT a followers-only flag -- it is the
    filter on the discovery surfaces (search, tags, domains, community listings,
    profiles) while the subscribed feed skips it. PostReply.private is the one
    that means followers-only.

    Prefer microblog=True in any test about feed visibility of ingested content.
    Passing private= directly sets the column without the rest of the shape.
    """
    if microblog:
        private = True
        title = ''
    post = Post(
        community_id=community.id,
        user_id=user.id,
        title=title,
        ap_id=ap_id,
        instance_id=user.instance_id,
        posted_at=utcnow(),
        last_active=utcnow(),
        from_bot=False,
        nsfw=False,
        deleted=False,
        private=private,
        microblog=microblog,
    )
    db.session.add(post)
    db.session.commit()
    return post


def make_site() -> Site:
    """The Site row with id 1 that app.utils.blocked_phrases() (called from
    Post.new()) unconditionally looks up. Not created automatically by
    db_session, which only truncates tables -- callers that exercise the
    post-creation path must call this first.
    """
    site = Site(name='Test Site', blocked_phrases='')
    db.session.add(site)
    db.session.commit()
    return site


def make_follow(local_user, remote_user, is_accepted=True, is_inward=False) -> UserFollower:
    """local_user follows remote_user. is_inward False means outward: we follow them.

    is_accepted: None = request sent (pending), True = accepted, False = rejected.
    is_inward: False (default) = local_user follows remote_user (outward -- opens feed
    visibility). True = remote_user follows local_user (inward -- someone follows US;
    this must NOT open feed visibility of what remote_user posts or boosts).
    """
    follow = UserFollower(
        local_user_id=local_user.id,
        remote_user_id=remote_user.id,
        is_accepted=is_accepted,
        is_inward=is_inward,
    )
    db.session.add(follow)
    db.session.commit()
    return follow
