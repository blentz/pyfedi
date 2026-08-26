"""Builders for database rows used by the boost ingestion tests.

Each builder sets the minimum needed for a valid row and commits, so the object
has an id. If Postgres rejects an insert for a missing NOT NULL column, add that
column here rather than in the test.
"""

from app import db
from app.activitypub.signature import RsaKeys
from app.models import (Community, CommunityBan, CommunityMember, Instance, InstanceBan, Post,
                        Role, RolePermission, Site, User, UserFollower, user_role, utcnow)


def make_instance(domain: str, software: str = 'mastodon') -> Instance:
    instance = Instance(domain=domain, software=software)
    db.session.add(instance)
    db.session.commit()
    return instance


def make_user(instance, name: str, local: bool = False, with_keys: bool = False) -> User:
    """A local user has ap_id None; a remote user has a full actor URI.

    with_keys generates a real RSA keypair. Off by default because generation
    costs roughly a second and almost no test needs it -- but a user that SENDS
    a signed activity does: HttpSignature.signed_request calls .encode() on the
    private key, so a keyless sender dies at signing with "'NoneType' object has
    no attribute 'encode'" before any HTTP request is attempted. A test
    asserting on delivery must build its sending actor with with_keys=True.
    """
    private_key, public_key = RsaKeys.generate_keypair() if with_keys else (None, None)
    user = User(
        user_name=name,
        email=f'{name}@example.com',
        instance_id=instance.id if instance else 1,
        verified=True,
        banned=False,
        private_key=private_key,
        public_key=public_key,
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


def grant_permission(user: User, permission: str) -> Role:
    """Create a Role carrying `permission`, and assign it to `user`.

    `user_access(permission, user.id)` reads role_permission joined to the
    `user_role` association table (app/utils.py) -- there is no UserRole model,
    `user_role` is a plain db.Table (app/models.py:918), so the assignment row
    is inserted directly through it rather than via a relationship object.
    """
    role = Role(name=f'role-{permission}', weight=0)
    db.session.add(role)
    db.session.commit()

    role_permission = RolePermission(role_id=role.id, permission=permission)
    db.session.add(role_permission)
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def make_community_member(user: User, community: Community, is_moderator: bool = False) -> CommunityMember:
    member = CommunityMember(
        user_id=user.id,
        community_id=community.id,
        is_moderator=is_moderator,
        is_owner=False,
        is_banned=False,
    )
    db.session.add(member)
    db.session.commit()
    return member


def ban_user_from_community(user: User, community: Community) -> CommunityBan:
    """Only the CommunityBan half of communities_banned_from's UNION.

    communities_banned_from (app/utils.py) also unions in Community rows joined
    through InstanceBan, but no InstanceBan factory exists here -- that path is
    out of scope for this task; can_create_post's instance check goes through
    banned_instances() instead, which a later task covers.
    """
    ban = CommunityBan(
        user_id=user.id,
        community_id=community.id,
        banned_by=None,
        reason='test ban',
    )
    db.session.add(ban)
    db.session.commit()
    return ban


def make_instance_ban(user: User, instance: Instance) -> InstanceBan:
    """The InstanceBan half of banned_instances() -- ban_user_from_community's
    docstring ruled this out of scope for the earlier community-ban task on the
    basis that can_create_post's instance check goes through banned_instances()
    instead, covered here. InstanceBan has no surrogate id column: (user_id,
    instance_id) is the composite primary key.
    """
    ban = InstanceBan(user_id=user.id, instance_id=instance.id)
    db.session.add(ban)
    db.session.commit()
    return ban


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
