"""Builders for database rows used by the boost ingestion tests.

Each builder sets the minimum needed for a valid row and commits, so the object
has an id. If Postgres rejects an insert for a missing NOT NULL column, add that
column here rather than in the test.
"""

from app import db
from app.models import Community, Instance, Post, User, UserFollower, utcnow


def make_instance(domain: str, software: str = 'mastodon') -> Instance:
    instance = Instance(domain=domain, software=software, online=True)
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


def make_post(community, user, ap_id: str, title: str = 'a post') -> Post:
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
    )
    db.session.add(post)
    db.session.commit()
    return post


def make_follow(local_user, remote_user) -> UserFollower:
    """local_user follows remote_user. is_inward False means outward: we follow them."""
    follow = UserFollower(
        local_user_id=local_user.id,
        remote_user_id=remote_user.id,
        is_accepted=True,
        is_inward=False,
    )
    db.session.add(follow)
    db.session.commit()
    return follow
