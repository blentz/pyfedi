"""Builders for database rows used by the boost ingestion tests.

Each builder sets the minimum needed for a valid row and commits, so the object
has an id. If Postgres rejects an insert for a missing NOT NULL column, add that
column here rather than in the test.

Not everything here is a row builder. peer_actor_json returns the plain
ActivityPub actor document a peer would serve, for the tests that drive
actor_json_to_model; announce_activity and note_document do the same for the
activity envelope and the object it names. Their docstrings say why they live
here.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime

from flask import current_app
from flask_login import login_user

from app import db
from app.activitypub.signature import RsaKeys
from app.models import (ActivityPubLog, BannedInstances, ChatMessage, Community, CommunityBan, CommunityBlock,
                        CommunityFlair, CommunityFlairBlock, CommunityJoinRequest, CommunityMember, Conversation,
                        Domain, DomainBlock, Feed, FeedItem, FeedJoinRequest, FeedMember, Instance, InstanceBan,
                        InstanceBlock, NotificationSubscription, Poll, PollChoice, Post, PostReply,
                        PostReplyBookmark, PostReplyVote, PostVote, Role, RolePermission, Site, User, UserBlock,
                        UserFollower, UserFollowRequest, UserRegistration, hidden_posts, read_posts, user_role,
                        utcnow)
from app.utils import get_deduped_post_ids


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


def make_feed_viewer(instance, name: str, local: bool = False, with_keys: bool = False) -> User:
    """make_user, with hide_nsfw/hide_nsfl zeroed for feed-filter tests.

    Both columns default to 1 ("on", app/models.py:984-985), so a plain
    make_user viewer inherits NSFW/NSFL filtering-on from the moment it is
    created, unless a test explicitly zeroes them. That coupling is inert
    today -- make_post defaults nsfw=False, and nothing in this module sets
    nsfl -- but it inflates the apparent blast radius of any future mutation
    on app/utils.py's hide_nsfw/hide_nsfl clause across every feed test whose
    viewer was built with plain make_user, which is exactly the confusion
    tests/README.md's "sort-chain divergence" section warns wide/narrow
    blast radius can cause. tests/test_feed_display_preferences.py's module
    docstring is the worked example: an over-broadened hide_nsfw/hide_nsfl
    clause there failed six unrelated presence tests as collateral before its
    viewers were fixed to zero both columns.

    Use this for any User that will be passed as `current_user` into a feed
    query (get_deduped_post_ids, get_instance_stickies, possible_communities);
    use plain make_user for an author, booster, or other non-viewing actor.
    A test that specifically exercises hide_nsfw/hide_nsfl (e.g.
    tests/test_feed_display_preferences.py's TestHideNsfw/TestHideNsfl) should
    keep setting them explicitly on its own viewer instead -- this factory
    only fixes the default for tests that are not testing that preference.
    """
    viewer = make_user(instance, name, local=local, with_keys=with_keys)
    viewer.hide_nsfw = 0
    viewer.hide_nsfl = 0
    db.session.commit()
    return viewer


def feed_ids(app, viewer, community_ids, sort='new', **kwargs):
    """The post ids get_deduped_post_ids returns for `viewer`, logged in.

    A fresh uuid result_id on every call bypasses the Redis cache path
    (`if cache_key and redis_client.exists(cache_key): return ...`,
    app/utils.py), so every call genuinely re-runs the query instead of
    replaying a previous result -- load-bearing wherever a test file makes
    several calls that must not reuse each other's answer.

    Shared by tests/test_feed_sorts.py, tests/test_feed_visibility_filters.py,
    tests/test_feed_display_preferences.py, tests/test_factories_feed.py and
    tests/test_subscribed_feed_microblogs.py, which used to each carry their
    own copy of this exact body (or a thin variant of it). A file whose own
    call sites use a different argument order or name (test_feed_sorts.py's
    (sort, community) order, test_subscribed_feed_microblogs.py's
    subscribed_feed_ids and its nested class' feed_ids) keeps a local wrapper
    that delegates here, rather than rewriting every call site.
    """
    with app.test_request_context('/'):
        login_user(viewer)
        return get_deduped_post_ids(uuid.uuid4().hex, community_ids, sort, **kwargs)


def make_community(name: str = 'microblogs', host: str = 'test.piefed.local') -> Community:
    """A Community whose ActivityPub identity is published on `host`.

    `host` defaults to the value every caller before the resolver sub-project
    hardcoded, so existing callers are unaffected. It is a parameter because
    resolve_remote_post (app/activitypub/util.py) derives its announce-actor
    domain from `ap_profile_id`'s netloc and compares it against the netloc of
    the post URI it is asked to fetch -- a test of that comparison has to be
    able to make the two agree, disagree, or differ only in case, and cannot do
    any of that while the community's host is a constant. '.local' is also the
    one suffix get_request refuses outright (is_invalid_get_request_uri), so a
    test that wants the announce host and the post host to AGREE on a fetchable
    URI has to move the community off the default.
    """
    community = Community(
        name=name,
        title=name,
        instance_id=1,
        user_id=1,
        ap_profile_id=f'https://{host}/c/{name}',
        ap_public_url=f'https://{host}/c/{name}',
        ap_followers_url=f'https://{host}/c/{name}/followers',
        ap_domain=host,
        subscriptions_count=0,
        local_only=False,
        nsfw=False,
    )
    db.session.add(community)
    db.session.commit()
    return community


def make_feed(instance, name: str = 'peerfeed', public: bool = False,
              local: bool = False, with_keys: bool = False) -> Feed:
    """A Feed the preamble's feed_only lookup can resolve.

    `ap_profile_id` must contain '/f/': find_remote_actor (app/activitypub/
    actor.py:86-131) branches on that literal substring before falling
    through to its unconditional queries.

    public defaults to False to match Feed.public's own column default
    (app/models.py:4062, `db.Column(db.Boolean, default=False, ...)`). None
    of the three 5a literals this factory replaced set `public=`, so they all
    read back False; a factory default of True would have silently changed
    what those rows are.

    `local=True` still sets `ap_id` (unlike `make_user(local=True)` and
    `make_community`, both of which leave `ap_id` `None`), so a local feed is
    local only via the second disjunct of `Feed.is_local()`
    (`app/models.py:4201-4202`, `self.ap_id is None or
    self.profile_id().startswith(current_app.config['SERVER_URL'])`), never
    the first.
    """
    host = 'test.piefed.local' if local else instance.domain
    feed = Feed(name=name, title=name, instance_id=instance.id,
                public=public,
                ap_id=f'{name}@{host}', ap_domain=host,
                ap_profile_id=f'https://{host}/f/{name}',
                ap_public_url=f'https://{host}/f/{name}',
                ap_fetched_at=utcnow())
    if with_keys:
        private_key, public_key = RsaKeys.generate_keypair()
        feed.private_key = private_key
        feed.public_key = public_key
    db.session.add(feed)
    db.session.commit()
    return feed


def make_local_feed(name: str = 'localfeed', public: bool = False) -> Feed:
    """A Feed webfinger can actually resolve: `ap_id` is None.

    `make_feed` cannot be used here. It sets `ap_id` unconditionally -- even at
    `local=True`, as its own docstring records -- and webfinger's lookup filters
    on `ap_id=None` (among other guards), so no feed `make_feed` builds is
    reachable by webfinger at any argument. `make_feed`'s `ap_id` is
    load-bearing elsewhere (find_remote_actor branches on `/f/` in
    `ap_profile_id`), so this is a sibling rather than a change to it.

    `public` defaults to False to match Feed.public's own column default
    (app/models.py:4098); callers pass it explicitly either way, because an
    assertion resting on a declared default proves nothing.
    """
    feed = Feed(name=name, title=name, instance_id=1, public=public,
                ap_profile_id=f"https://test.piefed.local/f/{name}",
                ap_public_url=f"https://test.piefed.local/f/{name}")
    db.session.add(feed)
    db.session.commit()
    return feed


def make_feed_item(feed: Feed, community: Community) -> FeedItem:
    """One community's membership of a feed. Add creates these; Remove deletes them."""
    item = FeedItem(feed_id=feed.id, community_id=community.id)
    db.session.add(item)
    db.session.commit()
    return item


def make_feed_member(user: User, feed: Feed, is_owner: bool = False) -> FeedMember:
    """A user's subscription to a feed.

    `is_owner` and `is_banned` both default to False on the model
    (app/models.py:4034-4035), which is what Feed.subscribed() reads to
    return SUBSCRIPTION_MEMBER rather than OWNER or BANNED.
    """
    member = FeedMember(feed_id=feed.id, user_id=user.id, is_owner=is_owner)
    db.session.add(member)
    db.session.commit()
    return member


def make_community_join_request(user: User, community: Community,
                                joined_via_feed: bool = False) -> CommunityJoinRequest:
    """The row Accept and Reject consume. `uuid` defaults to uuid4; the
    a.gup.pe Accept path looks the row up by the LAST path segment of the
    activity's string object, so tests build that string from this uuid."""
    jr = CommunityJoinRequest(user_id=user.id, community_id=community.id,
                              joined_via_feed=joined_via_feed)
    db.session.add(jr)
    db.session.commit()
    return jr


def make_feed_join_request(user: User, feed: Feed) -> FeedJoinRequest:
    jr = FeedJoinRequest(user_id=user.id, feed_id=feed.id)
    db.session.add(jr)
    db.session.commit()
    return jr


def make_user_follow_request(requestor: User, target: User) -> UserFollowRequest:
    """user_id is the requestor; follow_id is who they asked to follow."""
    jr = UserFollowRequest(user_id=requestor.id, follow_id=target.id)
    db.session.add(jr)
    db.session.commit()
    return jr


def peer_instance(domain: str = 'peer.example') -> Instance:
    """The Instance row find_instance_id looks up, created before the call so
    that function takes its found-existing path instead of inserting a sparse
    row and calling new_instance_profile (which fetches the peer's nodeinfo).

    A thin wrapper over make_instance: the value is the reason, not the
    construction. It was written out three times over -- once each in the
    Person, Group and Feed actor_json_to_model test files -- before being
    promoted here.
    """
    return make_instance(domain)


def seed_community_owner(domain: str = 'peer.example') -> Instance:
    """Creates the Instance (id=1) and local User (id=1) that make_community's
    hardcoded instance_id=1 / user_id=1 columns require to exist first, and
    returns the Instance.

    Returning the Instance is the single contract, deliberately: the two
    copies this replaced had drifted to two different ones under one name --
    find_community's tests returned the Instance so they could hang a remote
    actor off it, find_flair_or_create's returned None because its own tests
    never needed the row. A caller that does not want the Instance can ignore
    it; a caller that does cannot conjure it back from a None. Nothing here
    varies by call site, so nothing needs a flag or a second name.
    """
    instance = make_instance(domain)
    make_user(None, 'communityowner', local=True)
    return instance


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


def make_community_ban(user: User, community: Community, banned_by: User = None,
                       reason: str = '', ban_until=None) -> CommunityBan:
    """The row `ban_user` checks for before creating its own, and the row
    `unban_user` deletes.

    `CommunityBan` has a COMPOSITE primary key -- `user_id` and
    `community_id` together (app/models.py) -- and no `id` column, so a test
    that wants to assert the row is gone queries by both keys rather than by
    an id it never received.

    `banned_by` defaults to the community's own owner rather than to None,
    because a NULL `banned_by` is not a state `ban_user` can produce and a
    fixture that manufactures one would test a shape production never sees.
    """
    ban = CommunityBan(
        user_id=user.id,
        community_id=community.id,
        banned_by=banned_by.id if banned_by else community.user_id,
        reason=reason,
        ban_until=ban_until,
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
    to add the follow and boost disjuncts at all (app/utils.py:3830). A follow row
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


def make_chat_message(sender: User, recipient: User, ap_id: str, *,
                      body: str = 'a message', deleted: bool = False) -> ChatMessage:
    """A ChatMessage for Undo/Delete's PM-restore branch, which queries
    `ChatMessage` by `ap_id` and `sender_id` together (the branch reached when
    `find_liked_object` finds no post or comment for the id).

    conversation_id is left NULL deliberately: the column is nullable
    (app/models.py:287) and nothing on the restore path reads it, so building a
    Conversation would be scaffolding no test asserts on.
    """
    message = ChatMessage(
        sender_id=sender.id,
        recipient_id=recipient.id,
        body=body,
        ap_id=ap_id,
        deleted=deleted,
    )
    db.session.add(message)
    db.session.commit()
    return message


def make_conversation(sender: User, recipient: User) -> Conversation:
    """A two-party conversation, built the way process_chat builds one.

    `user_id` records the initiator; membership is what makes it findable.
    `members` is a backref from User.conversations (app/models.py:1053-1054,
    `secondary=conversation_member`), so appending here writes the association
    rows that `Conversation.find_existing_conversation`'s raw SQL joins on. A
    conversation missing either row is invisible to that lookup, which is why
    both are appended rather than relying on `user_id` alone.
    """
    conversation = Conversation(user_id=sender.id)
    conversation.members.append(sender)
    conversation.members.append(recipient)
    db.session.add(conversation)
    db.session.commit()
    return conversation


def mark_post_read(user: User, post: Post) -> None:
    """Insert into the `read_posts` association table.

    `read_posts` has no ORM model -- it is a plain db.Table (app/models.py:933) --
    so the row is inserted directly through it, mirroring grant_permission's use of
    user_role.insert(). get_deduped_post_ids reads
    `SELECT read_post_id FROM "read_posts" WHERE user_id = :user_id` when the
    viewer's hide_read_posts is set (app/utils.py:3887), and get_instance_stickies
    reads the identical query under the same condition (app/utils.py:4041).
    """
    db.session.execute(read_posts.insert().values(user_id=user.id, read_post_id=post.id))
    db.session.commit()


def hide_post(user: User, post: Post) -> None:
    """Insert into the `hidden_posts` association table.

    `hidden_posts` has no ORM model either (app/models.py:942). get_deduped_post_ids
    reads `SELECT hidden_post_id FROM "hidden_posts" WHERE user_id = :user_id`
    unconditionally for every authenticated viewer (app/utils.py:3890), and
    get_instance_stickies reads the identical query unconditionally too
    (app/utils.py:4037).
    """
    db.session.execute(hidden_posts.insert().values(user_id=user.id, hidden_post_id=post.id))
    db.session.commit()


def make_community_flair(community: Community, name: str = 'flair', ap_id: str = None) -> CommunityFlair:
    """A CommunityFlair scoped to `community`, with no post attached.

    find_flair_or_create (app/activitypub/util.py) resolves and updates
    CommunityFlair rows at the community level only -- it never reads or
    writes Post.flair -- so tests targeting it need just this, not the
    Post (and the User and Community a Post drags in) that make_post_flair
    below builds solely to reach the post_flair attachment step
    find_flair_or_create never touches. make_post_flair is written in terms
    of this factory rather than duplicating the CommunityFlair construction
    a second time.
    """
    flair = CommunityFlair(community_id=community.id, flair=name, ap_id=ap_id)
    db.session.add(flair)
    db.session.commit()
    return flair


def make_post_flair(post: Post, name: str = 'flair') -> CommunityFlair:
    """A CommunityFlair scoped to `post`'s community, attached to `post`.

    There is no PostFlair model: flair is a community-level CommunityFlair
    (app/models.py:4151) attached to posts through the `post_flair` many-to-many
    table (app/models.py:345), exposed as the `Post.flair` relationship. This
    factory follows that shape rather than a per-post model. get_deduped_post_ids's
    blocked-flair filter reads `post_flair` directly (`SELECT post_id FROM
    "post_flair" WHERE flair_id IN :blocked_flair_ids`, app/utils.py:3923).
    """
    flair = make_community_flair(post.community, name)
    post.flair.append(flair)
    db.session.commit()
    return flair


def make_flair_block(user: User, flair: CommunityFlair) -> CommunityFlairBlock:
    """app/utils.py's get_deduped_post_ids reads CommunityFlairBlock rows filtered
    on user_id and community_id, then excludes posts carrying any blocked
    community_flair_id (app/utils.py:3919-3924) -- matches this factory's columns.
    community_id is taken from `flair` so the block always targets the community
    the flair belongs to, matching how the query filters
    CommunityFlairBlock.community_id.in_(community_ids).
    """
    block = CommunityFlairBlock(user_id=user.id, community_id=flair.community_id,
                                community_flair_id=flair.id)
    db.session.add(block)
    db.session.commit()
    return block


def make_activitypub_log(activity_id: str, *, direction: str = 'in',
                         activity_type: str = 'Create', activity_json: str = None,
                         result: str = 'success', exception_message: str = None) -> ActivityPubLog:
    """The row `activities_json` and `activity_result` both read.

    `activity_id` is the FULL URI both endpoints match on, not a bare id:
    `activities_json` builds `f"{SERVER_URL}/activities/{type}/{id}"` and
    `activity_result` builds `f'https://{id}'` from a path parameter, so a
    caller must pass whichever form the endpoint under test will construct.

    `activity_json` is a JSON **string**, not a dict -- `activities_json` calls
    `json.loads` on it -- and it is nullable, which is a branch that endpoint
    takes (`activity_json = {}`). `result` and `exception_message` are what
    `activity_result` branches on; `exception_message` defaults to None so a
    test asserting on the disclosure must set it explicitly rather than rest
    on a default.
    """
    log = ActivityPubLog(
        direction=direction,
        activity_id=activity_id,
        activity_type=activity_type,
        activity_json=activity_json,
        result=result,
        exception_message=exception_message,
        created_at=utcnow(),
    )
    db.session.add(log)
    db.session.commit()
    return log


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


def make_poll(post: Post, *, mode: str = 'single', local_only: bool = False,
              end_poll: datetime = None) -> Poll:
    """The Poll row the Create/Update arm resolves with
    `session.query(Poll).get(post.id)`.

    `post_id` is Poll's PRIMARY KEY (app/models.py:3745), so a post has at most
    one poll and the identity map returns the same object for the same post --
    which is what makes the `.get()` lookup in the arm work at all.

    `end_poll` defaults to None rather than a future date: nothing on the
    dispatcher's vote path consults it, and inventing a deadline here would put
    a value in the fixture that no test asserts on.
    """
    poll = Poll(post_id=post.id, mode=mode, local_only=local_only, end_poll=end_poll)
    db.session.add(poll)
    db.session.commit()
    return poll


def make_poll_choice(post: Post, choice_text: str, *, sort_order: int = 0) -> PollChoice:
    """One option on `post`'s poll.

    The Create/Update arm matches an incoming vote by `choice_text` against the
    `name` field of the inbound Note, so callers should pass the exact text the
    activity will carry.
    """
    choice = PollChoice(post_id=post.id, choice_text=choice_text, sort_order=sort_order)
    db.session.add(choice)
    db.session.commit()
    return choice


# The public key every peer actor document below carries. actor_json_to_model
# copies activity_json['publicKey']['publicKeyPem'] straight into a Text column
# and never parses it, so a marker string is enough and avoids paying
# RsaKeys.generate_keypair()'s ~1s per document.
PEER_ACTOR_PUBLIC_KEY_PEM = '-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----\n'

# Where each actor type's id lives on a peer, following the Lemmy/PieFed URL
# convention the rest of this suite uses (/u/, /c/, /f/).
_PEER_ACTOR_PATH = {'Person': 'u', 'Service': 'u', 'Group': 'c', 'Feed': 'f'}


def peer_actor_json(actor_type: str = 'Person', name: str = 'alice',
                    server: str = 'peer.example',
                    fields: dict = None,
                    omit: Iterable[str] = ()) -> dict:
    """A peer's actor document, shaped as actor_json_to_model receives it.

    Not a database-row builder like the rest of this module: it returns the
    plain dict that actor_json_to_model's `activity_json` parameter takes, so
    a test drives production code with `actor_json_to_model(peer_actor_json(...),
    address, server)`.

    Shared by the Person/Service, Group and Feed branches of that function.
    Each branch reads an overlapping but different set of keys, so the baseline
    here is per-type and holds ONLY the keys that branch reads unconditionally
    -- the keys whose absence raises KeyError rather than falling back to a
    default:

      Person/Service  type, id, preferredUsername, publicKey.publicKeyPem
      Group           the above, plus name, inbox, outbox
      Feed            the above, plus following

    `inbox` earns its place in the Group and Feed baselines even though it is
    read through a conditional: the expression is
    `activity_json['endpoints']['sharedInbox'] if 'endpoints' in activity_json
    else activity_json['inbox']`, whose else-arm has no further fallback, so a
    document with neither key raises KeyError. A test wanting the sharedInbox
    arm passes an 'endpoints' key; a test wanting no inbox at all passes
    omit=('inbox',) and expects the KeyError handler.

    `attributedTo` is deliberately NOT in the Feed baseline, even though the
    Feed branch dereferences the resulting owners_url over HTTP. It is read
    through `if 'attributedTo' ... elif 'moderators' ... else owners_url =
    None`, so its absence is a default and not a KeyError -- putting it in the
    baseline would hand every Feed test the first arm and leave the 'moderators'
    elif and the None else permanently unreachable. Feed tests must pass
    fields={'attributedTo': ...} or fields={'moderators': ...} explicitly, and
    a Feed document with neither reaches get_request(None).

    Everything else those branches read is optional, and deliberately left out
    of the baseline: a document that always carried every optional key would
    exercise none of the defaults while branch coverage still reported the
    guards as covered. Tests opt in per key through `fields`.

    `fields` is a mapping merged over the baseline. It is a mapping rather than
    **kwargs because several keys these branches read are not Python
    identifiers -- 'lemmy:tagsForPosts' and '@context' among them -- and could
    not be passed as keyword arguments at all.

    `omit` names top-level keys to delete after the merge, which is how a test
    reaches the absent side of a guard over a key the baseline supplies (no
    'type' at all, no publicKey, no outbox). A sentinel value would not do:
    None is a value these branches genuinely distinguish from absence, since
    several guards read `'x' in activity_json and activity_json['x'] is not
    None`.

    `name` doubles as the actor's preferredUsername and as the last path
    segment of its id, matching how real peers publish actors; pass
    fields={'id': ...} to break that correspondence, which is what the tests
    for the guard comparing the id's host against `server` need.

    A key in `omit` that the document does not carry raises KeyError. The
    lenient `document.pop(key, None)` this replaced made a misspelling a
    silent no-op: `omit=('publickey',)` deleted nothing, the document kept
    its publicKey, and a test named for the missing-key branch passed while
    exercising the ordinary happy path instead. Since `omit` is applied
    after `fields` is merged, a key `fields` supplied is omittable too --
    the check is against the finished document, not against the baseline.
    """
    path = _PEER_ACTOR_PATH.get(actor_type, 'u')
    actor_id = f'https://{server}/{path}/{name}'
    document = {
        'type': actor_type,
        'id': actor_id,
        'preferredUsername': name,
        'publicKey': {'id': f'{actor_id}#main-key', 'owner': actor_id,
                      'publicKeyPem': PEER_ACTOR_PUBLIC_KEY_PEM},
    }
    if actor_type in ('Group', 'Feed'):
        document['name'] = name.replace('_', ' ').title()
        document['inbox'] = f'{actor_id}/inbox'
        document['outbox'] = f'{actor_id}/outbox'
    if actor_type == 'Feed':
        document['following'] = f'{actor_id}/following'
    if fields:
        document.update(fields)
    for key in omit:
        if key not in document:
            raise KeyError(
                f'peer_actor_json: cannot omit {key!r}, this {actor_type} document '
                f'has no such key. It carries: {sorted(document)}')
        del document[key]
    return document


# The host every activity envelope below is published from, and the two URIs
# derived from it. remote_object_to_json and verify_object_from_source both
# key their host comparisons off these, and the sub-projects covering the
# activity handlers will need the same pair, so they are named once here
# rather than restated per file.
PEER_OBJECT_HOST = 'remote.example'
PEER_ACTOR_URI = f'https://{PEER_OBJECT_HOST}/u/alice'
PEER_OBJECT_URI = f'https://{PEER_OBJECT_HOST}/objects/1'


def announce_activity(object_uri: str = PEER_OBJECT_URI, actor: str = PEER_ACTOR_URI,
                      host: str = PEER_OBJECT_HOST) -> dict:
    """An Announce envelope naming `object_uri`, as the inbox receives it.

    Every activity handler in this area starts by unwrapping an envelope of
    this shape, so this is deliberately the smallest one that is still a
    well-formed Announce rather than a fixture tuned to one function:
    'actor' and 'object' are what verify_object_from_source reads, and 'id'
    and 'type' are what makes it recognisable as an activity at all.

    `actor` and `object_uri` are separate parameters because the guards this
    feeds are precisely about them disagreeing -- an object hosted somewhere
    other than the announcing actor's host is the case worth constructing.
    """
    return {'id': f'https://{host}/activities/1', 'type': 'Announce',
            'actor': actor, 'object': object_uri}


def note_document(attributed_to: str = PEER_ACTOR_URI, uri: str = PEER_OBJECT_URI,
                  fields: dict = None) -> dict:
    """The Note an announce_activity's 'object' URI dereferences to.

    'attributedTo' is a parameter for the same reason 'actor' is on
    announce_activity: the interesting inputs are the ones where the object's
    stated author and the URI it was served from name different hosts.

    `fields` is a mapping merged over the baseline, following peer_actor_json's
    convention above. The baseline carries NO addressing, which is deliberate:
    activitypub_visibility (app/activitypub/util.py) classifies an object with
    neither 'to' nor 'cc' as 'direct', and create_post refuses a 'direct'
    object outright -- so a test that wants the document to become a Post has
    to opt in with fields={'to': [AS_PUBLIC_URI]} and is thereby forced to say
    so. Giving the baseline public addressing would have hidden that refusal
    behind every caller.
    """
    document = {'id': uri, 'type': 'Note', 'content': 'hello', 'attributedTo': attributed_to}
    if fields:
        document.update(fields)
    return document


# The addressing value activitypub_visibility (app/activitypub/util.py) reads
# as 'public' when it appears in an object's 'to'. Its AS_PUBLIC tuple also
# accepts the 'as:Public' and 'Public' spellings; this is the canonical one.
AS_PUBLIC_URI = 'https://www.w3.org/ns/activitystreams#Public'


def seed_signing_site() -> Site:
    """A Site row (id 1) carrying a real RSA keypair, required before the
    401 branch's signed_get_request call can run at all.
    """
    site = make_site()
    private_key, _public_key = RsaKeys.generate_keypair()
    site.private_key = private_key
    db.session.commit()
    return site


def serve_remote_object(http_mock, uri: str = PEER_OBJECT_URI, document: dict = None,
                        status: int = 200, content: bytes = None):
    """Register the ONE respx route a resolver's fetch of `uri` will hit, and
    return the document it serves.

    The fetch half of the fetch-plus-database shape the three remote-object
    resolvers need (resolve_remote_post, create_resolved_object's callers, and
    resolve_remote_post_from_search, all in app/activitypub/util.py). Those
    functions fetch, then parse, then write, so every test of them needs a
    mocked HTTP conversation as well as a database assertion; this is the
    first half and `resolvable_remote_author` below is the second.

    What it guarantees:

    - Exactly one GET route for `uri` exists on `http_mock`, answering with
      `status`. Nothing else is registered, so any OTHER request the code
      under test makes -- an actor fetch, a nodeinfo probe, an image
      dereference -- is an unmatched request, which the session-scoped autouse
      `block_outbound_http` router raises on rather than letting it reach the
      network. A resolver that fetches more than the caller intended fails
      loudly instead of silently passing.
    - `http_mock`'s assert_all_called=True means the converse holds too: a
      route registered here and never fetched fails the test at teardown, so
      this helper is never a silent no-op.
    - With `content` unset the body is `document` serialised as JSON, and the
      SAME dict object is returned, so a caller can assert on identity between
      what the peer served and what the resolver stored. `document` defaults to
      `note_document(uri=uri)` -- a well-formed Note whose 'attributedTo' is
      PEER_ACTOR_URI, i.e. on PEER_OBJECT_HOST, the host the default `uri` is
      also on. That default is the shape that gets THROUGH
      create_resolved_object's author-host comparison; a test wanting the
      refusing side passes its own document.
    - `content` overrides the body with raw bytes and returns None, which is
      how a test reaches remote_object_to_json's parse-failure return without
      also changing the status code.

    `document` is passed through untouched -- `{}` and `[]` are legitimate
    arguments, since a peer serving a 200 with an empty JSON body is exactly
    how remote_object_to_json returns a value that is falsy but not None.
    """
    if content is not None:
        http_mock.get(uri).respond(status, content=content,
                                   headers={'content-type': 'application/json'})
        return None
    if document is None:
        document = note_document(uri=uri)
    http_mock.get(uri).respond(status, json=document)
    return document


def resolvable_remote_author(instance: Instance, name: str = 'alice') -> User:
    """A remote User row that find_actor_or_create resolves WITHOUT any HTTP.

    The database half of the fetch-plus-database shape. `make_user` alone is
    not enough: find_actor_or_create finds the row and then calls
    schedule_actor_refresh (app/activitypub/actor.py), which fires
    refresh_user_profile for any non-local actor whose `ap_fetched_at` is None
    or older than a day. Under eager Celery that refresh runs inline and
    fetches the actor's profile, which -- with only the object route from
    `serve_remote_object` registered -- surfaces as an unmatched request.

    So this stamps `ap_fetched_at` to now, which is the one thing that makes
    the actor lookup a pure database read. The row's identity URIs come from
    `make_user`, i.e. https://{instance.domain}/users/{name}: pass an author
    URI of exactly that form as the served document's 'attributedTo' for the
    author-host comparison in create_resolved_object to pass.
    """
    user = make_user(instance, name)
    user.ap_fetched_at = utcnow()
    db.session.commit()
    return user


def inbox_activity(actor, *, activity_type: str = 'Like', object_uri: str = None, **fields) -> dict:
    """The minimum an activity needs to get past shared_inbox's field check.

    That check is `not 'id' in request_json or not 'type' ... or not 'actor'
    ... or not 'object'`, so all four are always present here and a test that
    wants one missing deletes it explicitly -- which reads as the deviation it
    is. `id` is unique per call because shared_inbox writes it to Redis for 90
    seconds to suppress duplicates; a fixed id would make tests interfere
    through Redis rather than through anything they assert about.
    """
    activity = {
        'id': f'{actor.ap_profile_id}/activities/{uuid.uuid4().hex}',
        'type': activity_type,
        'actor': actor.ap_profile_id,
        'object': object_uri or f'https://{actor.instance.domain}/objects/1',
    }
    activity.update(fields)
    return activity


def signed_inbox_post(client, activity: dict, sender, *, path: str = '/inbox',
                      body: bytes = None):
    """POST `activity` to `path` with a REAL HTTP signature made by `sender`.

    Uses production's own signing code. `signed_request(send_via_async=True)`
    returns (uri, headers, body_bytes) instead of sending, which is the whole
    reason these tests can be honest: the signature the gate verifies is one
    the application made, not one a test faked, and
    HttpSignature.verify_request is never patched anywhere in this suite.

    `sender` must have been built with `make_user(..., with_keys=True)` -- a
    keyless user dies at signing with "'NoneType' object has no attribute
    'encode'", because signed_request calls .encode() on the private key.

    `body` overrides the bytes actually sent while leaving the signature
    alone, which is how a test produces a request whose digest no longer
    matches its body.

    There is deliberately NO `host` override. One existed until a
    whole-branch review removed it: it was resolved as
    `host = host or SERVER_NAME` before use, so it fed the SIGNED URI and the
    sent `Host` header from the same value and could not produce the
    mismatch its docstring advertised (`HttpSignature.signed_request` already
    sets `headers["Host"]` from that same URI, app/activitypub/signature.py:449,
    so the re-assignment was a no-op too, and the `if host is not None:` guard
    around it was dead after the `or`). Nothing called it. A Host-mismatch
    test written from that parameter would have proved nothing; write one by
    hand instead, overriding `Host` in the headers AFTER signing.
    """
    from app.activitypub.signature import HttpSignature
    host = current_app.config['SERVER_NAME']
    _uri, headers, body_bytes = HttpSignature.signed_request(
        f'https://{host}{path}', activity, sender.private_key,
        f'{sender.ap_profile_id}#main-key', send_via_async=True)
    return client.post(path, data=body if body is not None else body_bytes,
                       headers=headers, content_type='application/activity+json')


def make_user_registration(user: User, answer: str = 'why', status: int = 0) -> UserRegistration:
    """A pending application. `warning` is left None -- it is the column
    app/shared/tasks/users.py writes, so a test that asserts on it needs it
    to start empty.
    """
    application = UserRegistration(user_id=user.id, answer=answer, status=status)
    db.session.add(application)
    db.session.commit()
    return application


def make_banned_instance(domain: str) -> BannedInstances:
    """A row that makes `instance_banned(domain)` return True.

    `app/utils.py:2335` opens its OWN task session and queries
    `BannedInstances` by exact domain, then separately by a `*` wildcard
    pattern. This factory covers the exact-match arm; the wildcard arm is not
    exercised by the outbound delivery gates that call it.
    """
    banned = BannedInstances(domain=domain)
    db.session.add(banned)
    db.session.commit()
    return banned
