"""Builders for database rows used by the boost ingestion tests.

Each builder sets the minimum needed for a valid row and commits, so the object
has an id. If Postgres rejects an insert for a missing NOT NULL column, add that
column here rather than in the test.
"""

from app import db
from app.activitypub.signature import RsaKeys
from app.models import (Community, CommunityBan, CommunityBlock, CommunityFlair, CommunityFlairBlock,
                        CommunityMember, Domain, DomainBlock, Instance, InstanceBan, InstanceBlock,
                        NotificationSubscription, Post, PostReply, PostReplyBookmark,
                        PostReplyVote, PostVote, Role, RolePermission, Site, User, UserBlock, UserFollower,
                        hidden_posts, read_posts, user_role, utcnow)


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
    no 'name': title='', private=True, microblog=True, and a non-empty body_html.
    It does NOT reproduce Post.new()'s activity-level Public check that can clear
    private back to False for a genuinely unlisted post (app/models.py:1834-1846)
    -- private here is exactly the object-titleless default, nothing more. status is
    left at the column default (POST_STATUS_PUBLISHED = 1), which already matches
    what Post.new() implicitly leaves it at, since Post.new() never sets status
    itself.

    title='' is the WORST case Post.new() can leave, not the usual one: it derives a
    title from the body via microblog_content_to_title(), which returns '' for a
    body with no paragraph of five characters or more (an image-only toot, say).
    body_html is what makes that case survivable -- app/community/routes.py's RSS
    route calls feedgen's fe.title() and fe.description(), and feedgen raises
    "Required fields not set" only when BOTH are empty. A factory that left
    body_html unset would manufacture a crash production cannot reach.

    Post.private is the microblog marker, NOT a followers-only flag: no non-public
    object ever becomes a Post, because create_post() refuses `followers` and
    `direct` visibility before Post.new() is reached
    (tests/test_post_private_is_only_the_microblog_marker.py). It filters the
    discovery surfaces -- search, tags, domains, user profiles -- and, in the
    aggregate feeds, the community source only, so a microblog arrives there via a
    follow rather than a subscription. It does NOT filter the community's own
    listing or RSS feed: a post in a community is visible when you browse that
    community. PostReply.private is the one that means followers-only.

    Prefer microblog=True in any test about feed visibility of ingested content.
    Passing private= directly sets the column without the rest of the shape.
    """
    body_html = None
    if microblog:
        private = True
        title = ''
        body_html = '<p>a short toot</p>'
    post = Post(
        community_id=community.id,
        user_id=user.id,
        title=title,
        body_html=body_html,
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


def make_post_reply(post: Post, user: User, body: str = 'a reply') -> PostReply:
    """A PostReply under `post`, authored by `user`. Mirrors the columns
    tests/conftest.py's own inline PostReply construction sets (user_id,
    post_id, community_id, instance_id, body, posted_at, deleted) -- there is
    no NOT NULL constraint beyond the primary key in the model, but these are
    the columns downstream queries (bookmarks, votes, subscriptions) actually
    join against.
    """
    reply = PostReply(
        user_id=user.id,
        post_id=post.id,
        community_id=post.community_id,
        instance_id=user.instance_id,
        body=body,
        posted_at=utcnow(),
        deleted=False,
    )
    db.session.add(reply)
    db.session.commit()
    return reply


def make_post_reply_bookmark(user: User, reply: PostReply) -> PostReplyBookmark:
    """The row app/utils.py's authorise_api_user reads via raw SQL:
    `SELECT post_reply_id FROM "post_reply_bookmark" WHERE user_id = :user_id`
    -- matches this factory's (user_id, post_reply_id) columns exactly.
    """
    bookmark = PostReplyBookmark(user_id=user.id, post_reply_id=reply.id)
    db.session.add(bookmark)
    db.session.commit()
    return bookmark


def make_post_reply_vote(user: User, reply: PostReply, effect: float) -> PostReplyVote:
    """A vote by `user` on `reply`. app/utils.py's recently_upvoted_post_replies /
    recently_downvoted_post_replies select post_reply_id from post_reply_vote
    filtered on user_id and effect > 0 / < 0 -- pass effect=1.0 for an upvote,
    effect=-1.0 for a downvote.
    """
    vote = PostReplyVote(user_id=user.id, author_id=reply.user_id, post_reply_id=reply.id,
                         effect=effect)
    db.session.add(vote)
    db.session.commit()
    return vote


def make_notification_subscription(user: User, entity_id: int, type_: int,
                                   name: str = 'sub') -> NotificationSubscription:
    """A NotificationSubscription row. app/utils.py's authorise_api_user reads
    `SELECT entity_id FROM "notification_subscription" WHERE type = :type and
    user_id = :user_id` for the reply-subscription case (type=NOTIF_REPLY) --
    matches this factory's (type, entity_id, user_id) columns exactly.
    """
    subscription = NotificationSubscription(name=name, type=type_, entity_id=entity_id,
                                            user_id=user.id)
    db.session.add(subscription)
    db.session.commit()
    return subscription


def make_user_block(blocker: User, blocked: User) -> UserBlock:
    """app/utils.py's blocked_users(user_id) reads UserBlock rows filtered on
    blocker_id and returns blocked_id -- matches this factory's columns.
    """
    block = UserBlock(blocker_id=blocker.id, blocked_id=blocked.id)
    db.session.add(block)
    db.session.commit()
    return block


def make_follow(local_user, remote_user, is_accepted=True, is_inward=False) -> UserFollower:
    """local_user follows remote_user. is_inward False means outward: we follow them.

    is_accepted: None = request sent (pending), True = accepted, False = rejected.
    is_inward: False (default) = local_user follows remote_user (outward -- opens feed
    visibility). True = remote_user follows local_user (inward -- someone follows US;
    this must NOT open feed visibility of what remote_user posts or boosts).

    An accepted outward follow also bumps `local_user.num_following`, because
    production does: app/shared/user.py:245 for a local target, and
    app/activitypub/routes.py:1144 when a remote target's Accept arrives. It is a
    denormalised counter, not a view over user_follower, and
    `get_deduped_post_ids` reads THAT counter -- not the rows -- to decide whether
    to add the follow and boost disjuncts at all (app/utils.py:3815). A follow row
    without the counter is a shape production never produces, and it would make a
    feed test silently exercise the no-follows branch.
    """
    follow = UserFollower(
        local_user_id=local_user.id,
        remote_user_id=remote_user.id,
        is_accepted=is_accepted,
        is_inward=is_inward,
    )
    db.session.add(follow)
    if is_accepted is True and not is_inward:
        local_user.num_following = (local_user.num_following or 0) + 1
    db.session.commit()
    return follow


def make_domain(name: str) -> Domain:
    """A Domain row -- the FK target for DomainBlock and for Post.domain_id.

    app/utils.py's blocked_domains(user_id) returns DomainBlock.domain_id values,
    which get_deduped_post_ids compares against p.domain_id; both sides need a
    real Domain row to point at.
    """
    domain = Domain(name=name)
    db.session.add(domain)
    db.session.commit()
    return domain


def make_domain_block(user: User, domain: Domain) -> DomainBlock:
    """app/utils.py's blocked_domains(user_id) reads DomainBlock rows filtered on
    user_id and returns domain_id -- matches this factory's columns.
    """
    block = DomainBlock(user_id=user.id, domain_id=domain.id)
    db.session.add(block)
    db.session.commit()
    return block


def make_instance_block(user: User, instance: Instance) -> InstanceBlock:
    """app/utils.py's blocked_or_banned_instances(user_id) reads InstanceBlock rows
    filtered on user_id and returns instance_id, unioned with banned_instances()
    (the InstanceBan-backed function covered by make_instance_ban above) -- matches
    this factory's columns.
    """
    block = InstanceBlock(user_id=user.id, instance_id=instance.id)
    db.session.add(block)
    db.session.commit()
    return block


def make_community_block(user: User, community: Community) -> CommunityBlock:
    """app/utils.py's blocked_communities(user_id) reads CommunityBlock rows
    filtered on user_id and returns community_id -- matches this factory's
    columns.
    """
    block = CommunityBlock(user_id=user.id, community_id=community.id)
    db.session.add(block)
    db.session.commit()
    return block


def mark_post_read(user: User, post: Post) -> None:
    """Insert into the `read_posts` association table.

    `read_posts` has no ORM model -- it is a plain db.Table (app/models.py:933) --
    so the row is inserted directly through it, mirroring grant_permission's use of
    user_role.insert(). get_deduped_post_ids reads
    `SELECT read_post_id FROM "read_posts" WHERE user_id = :user_id` when the
    viewer's hide_read_posts is set (app/utils.py:3862), and get_instance_stickies
    reads the identical query under the same condition (app/utils.py:4016).
    """
    db.session.execute(read_posts.insert().values(user_id=user.id, read_post_id=post.id))
    db.session.commit()


def hide_post(user: User, post: Post) -> None:
    """Insert into the `hidden_posts` association table.

    `hidden_posts` has no ORM model either (app/models.py:942). get_deduped_post_ids
    reads `SELECT hidden_post_id FROM "hidden_posts" WHERE user_id = :user_id`
    unconditionally for every authenticated viewer (app/utils.py:3865), and
    get_instance_stickies reads the identical query unconditionally too
    (app/utils.py:4012).
    """
    db.session.execute(hidden_posts.insert().values(user_id=user.id, hidden_post_id=post.id))
    db.session.commit()


def make_post_flair(post: Post, name: str = 'flair') -> CommunityFlair:
    """A CommunityFlair scoped to `post`'s community, attached to `post`.

    There is no PostFlair model: flair is a community-level CommunityFlair
    (app/models.py:4151) attached to posts through the `post_flair` many-to-many
    table (app/models.py:345), exposed as the `Post.flair` relationship. This
    factory follows that shape rather than a per-post model. get_deduped_post_ids's
    blocked-flair filter reads `post_flair` directly (`SELECT post_id FROM
    "post_flair" WHERE flair_id IN :blocked_flair_ids`, app/utils.py:3898).
    """
    flair = CommunityFlair(community_id=post.community_id, flair=name)
    db.session.add(flair)
    db.session.commit()
    post.flair.append(flair)
    db.session.commit()
    return flair


def make_flair_block(user: User, flair: CommunityFlair) -> CommunityFlairBlock:
    """app/utils.py's get_deduped_post_ids reads CommunityFlairBlock rows filtered
    on user_id and community_id, then excludes posts carrying any blocked
    community_flair_id (app/utils.py:3894-3899) -- matches this factory's columns.
    community_id is taken from `flair` so the block always targets the community
    the flair belongs to, matching how the query filters
    CommunityFlairBlock.community_id.in_(community_ids).
    """
    block = CommunityFlairBlock(user_id=user.id, community_id=flair.community_id,
                                community_flair_id=flair.id)
    db.session.add(block)
    db.session.commit()
    return block


def make_post_vote(user: User, post: Post, effect: float) -> PostVote:
    """A vote by `user` on `post`, mirroring make_post_reply_vote.

    post_ids_to_models's sort keys (ranking, ranking_scaled, score) are columns on
    Post itself, not derived from post_vote at query time -- production
    recalculates them when a vote is cast (app/shared/post.py:211-214), so a bare
    PostVote row does not change sort order by itself. This factory exists for
    parity with make_post_reply_vote and for tests that assert on the PostVote row
    directly (e.g. a blocked user's vote still exists in the table but should not
    count); a test about post_ids_to_models's sort order should set
    post.score / post.ranking / post.ranking_scaled directly instead.
    """
    vote = PostVote(user_id=user.id, author_id=post.user_id, post_id=post.id, effect=effect)
    db.session.add(vote)
    db.session.commit()
    return vote
