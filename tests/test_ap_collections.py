"""tests/test_ap_collections.py"""
import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.models import Post
from tests.factories import make_community, make_post, make_user
from tests.test_actor_profiles import seed_actors


def collection_get(app, path):
    """GET a collection endpoint through the real route.

    No Accept header is sent and none is needed: unlike the three actor-profile
    endpoints, NONE of these eight checks `is_activitypub_request()`, so all of
    them return ActivityPub JSON to any caller. That asymmetry is registered by
    the final task; this helper exists to make it visible rather than to work
    around it.
    """
    with app.test_client() as client:
        return client.get(path)


def seed_local_community(name='books'):
    """A local community the collection lookups can resolve.

    The lookups filter `name=<actor>, banned=False, ap_id=None` -- note they
    match on `name`, NOT on `ap_profile_id` the way `community_profile` does, so
    the host does not matter here the way it did in sub-project 9.
    """
    community = make_community(name=name, host='test.piefed.local')
    db.session.commit()
    return community


def test_a_local_community_outbox_is_served(app, db_session):
    """The ordinary path. `community_outbox` has NO `'@' in actor` check and no
    `is_activitypub_request()` check, so this succeeds with no header at all --
    both asymmetries against the feed collections, which abort(400) on a remote
    actor.
    """
    seed_actors()
    seed_local_community('books')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == 'https://test.piefed.local/c/books/outbox'


def test_an_unknown_community_outbox_is_404(app, db_session):
    """`else: abort(404)`. This is the shape three of the four FEED collections
    get wrong -- they 500 instead. Pinned there, correct here.
    """
    seed_actors()

    response = collection_get(app, '/c/nosuch/outbox')

    assert response.status_code == 404


def test_a_banned_community_outbox_is_404(app, db_session):
    """`banned=False` in the lookup. Set explicitly -- `Community.banned`
    defaults to False, so leaving it alone would assert nothing.

    Note `community_profile`'s LOCAL lookup has no such guard (registered as
    D158); this endpoint's does. The two disagree about the same community.
    """
    seed_actors()
    community = seed_local_community('books')
    community.banned = True
    db.session.commit()

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 404


def test_a_remote_community_outbox_is_404(app, db_session):
    """`ap_id=None`. `make_community` never sets `ap_id`, so this test sets it
    explicitly -- otherwise the clause is unkillable, a pattern this campaign
    has hit six times.
    """
    seed_actors()
    community = seed_local_community('books')
    community.ap_id = 'books@peer.example'
    db.session.commit()

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 404


def test_the_community_outbox_sets_its_cache_control(app, db_session):
    """max-age=10. Its eight siblings use 5, 15, 120 and -- for
    `community_featured` -- nothing at all, with no evident rationale. The
    values are asserted exactly so the spread is visible in the suite.
    """
    seed_actors()
    seed_local_community('books')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=10'


def test_sticky_posts_come_before_the_rest(app, db_session, monkeypatch):
    """Two queries, concatenated sticky-first. `post_to_activity` is doubled to
    return an identifiable marker so ORDER is observable -- the real delegate
    builds a large document and is its own future slice.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    plain = make_post(community, user, 'https://test.piefed.local/post/1')
    sticky = make_post(community, user, 'https://test.piefed.local/post/2')
    sticky.sticky = True
    plain.sticky = False
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: f'AP:{post.ap_id}')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    items = response.json['orderedItems']
    assert items == ['AP:https://test.piefed.local/post/2',
                     'AP:https://test.piefed.local/post/1']


def test_a_deleted_post_is_excluded(app, db_session, monkeypatch):
    """`Post.deleted == False`, applied to BOTH queries. Set explicitly --
    `Post.deleted` defaults to False.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.deleted = True
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: 'AP')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []
    assert response.json['totalItems'] == 0


def test_a_post_under_review_is_excluded(app, db_session, monkeypatch):
    """`Post.status > POST_STATUS_REVIEWING` (0, app/constants.py:21).
    `Post.status` defaults to 1, which PASSES the filter, so the excluded side
    needs status set to 0 explicitly.

    `community_featured` filters only `deleted=False`, with no status clause
    at all, so the same post is treated differently by the two endpoints.
    This test proves only what `community_outbox` does.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.status = 0
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: 'AP')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []


def test_a_post_in_another_community_is_excluded(app, db_session, monkeypatch):
    """`Post.community_id == community.id`, applied to both queries. Without
    this test the community filter is unkillable, since every other test seeds
    exactly one community.
    """
    seed_actors()
    community = seed_local_community('books')
    other = make_community(name='films', host='test.piefed.local')
    user = make_user(None, 'author', local=True)
    make_post(other, user, 'https://test.piefed.local/post/1')
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: 'AP')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []


def test_a_deleted_sticky_post_is_excluded(app, db_session, monkeypatch):
    """`Post.deleted == False`, in the STICKY query specifically.
    `test_a_deleted_post_is_excluded`'s post is `sticky=False` (the column
    default), so it only ever reaches the REMAINING query and cannot prove
    the STICKY query enforces this filter too. This test is identical to it
    except `sticky` is also set to True, which routes the post into the
    STICKY query instead. Both `sticky` and `deleted` are set explicitly --
    neither's column default would exercise what is being tested here.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.sticky = True
    post.deleted = True
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: 'AP')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []


def test_a_sticky_post_under_review_is_excluded(app, db_session, monkeypatch):
    """`Post.status > POST_STATUS_REVIEWING`, in the STICKY query
    specifically. `test_a_post_under_review_is_excluded`'s post is
    `sticky=False` (the column default), so it only ever reaches the
    REMAINING query and cannot prove the STICKY query enforces this filter
    too. This test is identical to it except `sticky` is also set to True,
    which routes the post into the STICKY query instead. Both `sticky` and
    `status` are set explicitly -- `Post.status` defaults to 1, which PASSES
    the filter, so only an explicit `status=0` exercises the excluded side.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.sticky = True
    post.status = 0
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: 'AP')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []


def test_a_sticky_post_in_another_community_is_excluded(app, db_session, monkeypatch):
    """`Post.community_id == community.id`, in the STICKY query specifically.
    `test_a_post_in_another_community_is_excluded`'s post is `sticky=False`
    (the column default), so it only ever reaches the REMAINING query and
    cannot prove the STICKY query enforces this filter too. This test is
    identical to it except the foreign post also has `sticky = True` set
    explicitly, which would route it into the STICKY query if that query's
    community filter were missing.
    """
    seed_actors()
    community = seed_local_community('books')
    other = make_community(name='films', host='test.piefed.local')
    user = make_user(None, 'author', local=True)
    post = make_post(other, user, 'https://test.piefed.local/post/1')
    post.sticky = True
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_activity',
                        lambda post, community: 'AP')

    response = collection_get(app, '/c/books/outbox')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []


def test_the_featured_collection_lists_sticky_posts(app, db_session, monkeypatch):
    """`community_featured` selects `sticky=True, deleted=False` and renders each
    with `post_to_page` -- a DIFFERENT delegate from `community_outbox`'s
    `post_to_activity`, which is why both are doubled separately.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.sticky = True
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_page',
                        lambda post: f'PAGE:{post.ap_id}')

    response = collection_get(app, '/c/books/featured')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['orderedItems'] == ['PAGE:https://test.piefed.local/post/1']


def test_a_non_sticky_post_is_not_featured(app, db_session, monkeypatch):
    """`sticky=True`. Set explicitly on the excluded post -- `Post.sticky`
    defaults to False, so the absence would otherwise rest on that default.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.sticky = False
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_page', lambda post: 'PAGE')

    response = collection_get(app, '/c/books/featured')

    assert response.status_code == 200
    assert response.json['orderedItems'] == []


def test_a_featured_post_under_review_is_published_anyway(app, db_session, monkeypatch):
    """PINS a defect. `community_outbox` filters
    `Post.status > POST_STATUS_REVIEWING`; `community_featured` filters only
    `deleted=False`. So a sticky post still under review is HIDDEN from the
    outbox and PUBLISHED in the featured collection -- the same post, two
    endpoints, opposite answers.

    Status is set to 0 (POST_STATUS_REVIEWING) explicitly; the column defaults
    to 1, which would pass any filter.
    """
    seed_actors()
    community = seed_local_community('books')
    user = make_user(None, 'author', local=True)
    post = make_post(community, user, 'https://test.piefed.local/post/1')
    post.sticky = True
    post.status = 0
    db.session.commit()
    monkeypatch.setattr(activitypub_routes, 'post_to_page', lambda post: 'PAGE')

    response = collection_get(app, '/c/books/featured')

    assert response.status_code == 200
    assert response.json['orderedItems'] == ['PAGE']


def test_the_featured_collection_sets_no_cache_control(app, db_session, monkeypatch):
    """PINS a defect. Every one of the eight sibling collections sets a
    Cache-Control header; this one sets none, so caching falls to whatever the
    deployment's default is.

    Asserted as absence rather than as a value, which is what makes it
    discriminating: adding any Cache-Control would fail this test.
    """
    seed_actors()
    seed_local_community('books')
    monkeypatch.setattr(activitypub_routes, 'post_to_page', lambda post: 'PAGE')

    response = collection_get(app, '/c/books/featured')

    assert response.status_code == 200
    assert 'Cache-Control' not in response.headers


def test_an_unknown_community_featured_is_404(app, db_session):
    seed_actors()

    response = collection_get(app, '/c/nosuch/featured')

    assert response.status_code == 404


def test_the_moderators_collection_lists_moderator_urls(app, db_session):
    """`community_moderators(community.id)` (app/utils.py) queries CommunityMember
    rows filtered on `is_owner OR is_moderator`, then unconditionally appends a
    SYNTHETIC, never-persisted CommunityMember for `community.user_id` whenever
    that id is not already among the query results -- so the community's owner
    is always resolved as a moderator, membership row or not (see the next
    test). To keep this test about the `is_moderator` filter alone, the seeded
    moderator is ALSO made the community's owner (`community.user_id = mod.id`
    after `make_community_member`), which puts them in the query result and
    short-circuits the append -- otherwise a second, synthetic entry for the
    real owner (`communityowner`, user id 1 from `seed_actors`) would also
    appear in `orderedItems`, and `totalItems` would be 2, not 1.

    `make_community_member(user, community, is_moderator=False)` (tests/factories.py)
    already matches the brief's call shape.
    """
    site, instance = seed_actors()
    community = seed_local_community('books')
    from tests.factories import make_community_member
    mod = make_user(instance, 'mod', local=True)
    make_community_member(mod, community, is_moderator=True)
    community.user_id = mod.id
    db.session.commit()

    response = collection_get(app, '/c/books/moderators')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['totalItems'] == 1
    assert response.json['orderedItems'] == [mod.public_url()]


def test_a_community_with_no_explicit_moderators_still_lists_its_owner(app, db_session):
    """Registers a fact the brief did not anticipate. `community_moderators`
    appends a never-persisted CommunityMember for `community.user_id` whenever
    that id is absent from its query result (app/utils.py). `seed_local_community`
    gives every community `user_id=1` -- the `communityowner` user `seed_actors`
    creates -- and never gives that user a real CommunityMember row, so this is
    the only way an unmoderated community's moderators collection is reachable
    through these factories: it is never actually empty. `totalItems` is 1 and
    `orderedItems` holds the owner's URL, not 0 and `[]` as the brief assumed.
    """
    seed_actors()
    seed_local_community('books')
    from app.models import User
    owner = User.query.filter_by(user_name='communityowner').first()

    response = collection_get(app, '/c/books/moderators')

    assert response.status_code == 200
    assert response.json['totalItems'] == 1
    assert response.json['orderedItems'] == [owner.public_url()]


def test_a_non_moderator_member_is_not_listed(app, db_session):
    """`community_moderators` filters on `is_owner OR is_moderator`. A plain
    member satisfies neither, so this is what makes that filter killable --
    weakening it to include everyone would put `member.public_url()` into
    `orderedItems`.

    Asserted as `not in` rather than `orderedItems == []`: the community's
    owner is synthesized into the result regardless (see the fact above), so
    an exact-list assertion would be confounded by a URL this test isn't about.
    """
    site, instance = seed_actors()
    community = seed_local_community('books')
    from tests.factories import make_community_member
    member = make_user(instance, 'member', local=True)
    make_community_member(member, community, is_moderator=False)
    db.session.commit()

    response = collection_get(app, '/c/books/moderators')

    assert response.status_code == 200
    assert member.public_url() not in response.json['orderedItems']


def test_the_moderators_collection_sets_a_two_minute_cache(app, db_session):
    """max-age=120 -- the longest of the nine (eight plus `community_followers`,
    which this task also covers), against `community_outbox`'s 10 and
    `feed_outbox`'s 5, for data that changes less often than either.
    """
    seed_actors()
    seed_local_community('books')

    response = collection_get(app, '/c/books/moderators')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=120'


def test_an_unknown_community_moderators_is_404(app, db_session):
    seed_actors()

    response = collection_get(app, '/c/nosuch/moderators')

    assert response.status_code == 404


def test_the_community_followers_collection_counts_its_members(app, db_session):
    """`totalItems` is `community_members(community.id)` (app/activitypub/util.py)
    -- a raw SQL COUNT of `community_member` rows joined to `user`, filtered on
    `u.banned is false`, `u.deleted is false` and `cm.is_banned is false`. It is
    a genuine count of persisted rows, unlike `community_moderators`, which
    synthesizes an owner entry that is never written to the table -- so seeding
    exactly one CommunityMember row makes this an exact count, tightened from
    the brief's `>= 1` now that the helper has been read.

    `member.deleted` is set explicitly: `make_user` sets `banned=False`
    explicitly but leaves `deleted` at the column default, and no assertion
    here may rest on that default.
    """
    site, instance = seed_actors()
    community = seed_local_community('books')
    from tests.factories import make_community_member
    member = make_user(instance, 'member', local=True)
    member.deleted = False
    make_community_member(member, community, is_moderator=False)
    db.session.commit()

    response = collection_get(app, '/c/books/followers')

    assert response.status_code == 200
    assert response.json['type'] == 'Collection'
    assert response.json['totalItems'] == 1


def test_the_community_followers_items_list_is_always_empty(app, db_session):
    """PINS a defect, and it is the SECOND of three followers collections to
    have it. `totalItems` is a real count (`community_members`, a genuine SQL
    COUNT of persisted rows) while `items` is hardcoded `[]` in the route
    itself (app/activitypub/routes.py), so the document says "one follower"
    and lists none.

    `feed_followers` does the same, per the brief. `user_followers` -- the
    third -- populates its items with real follower URLs and filters blocked
    and unaccepted follows (verified by reading app/activitypub/routes.py
    directly; neither sibling collection is exercised by a test in this file
    yet). Two of three contradict themselves; one does not.

    `member.deleted` is set explicitly for the same reason as the previous
    test: `make_user` leaves it at the column default, and no assertion here
    may rest on that default.
    """
    site, instance = seed_actors()
    community = seed_local_community('books')
    from tests.factories import make_community_member
    member = make_user(instance, 'member', local=True)
    member.deleted = False
    make_community_member(member, community, is_moderator=False)
    db.session.commit()

    response = collection_get(app, '/c/books/followers')

    assert response.status_code == 200
    assert response.json['totalItems'] == 1
    assert response.json['items'] == []


def test_an_unknown_community_followers_is_404(app, db_session):
    seed_actors()

    response = collection_get(app, '/c/nosuch/followers')

    assert response.status_code == 404


def test_the_community_followers_collection_sets_a_ten_second_cache(app, db_session):
    seed_actors()
    seed_local_community('books')

    response = collection_get(app, '/c/books/followers')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=10'


def _follow(local_user, follower, accepted=True):
    """A UserFollower row: `follower` follows `local_user`.

    `is_accepted` has NO declared default (the model comments None = request
    pending), so it is passed explicitly here and the endpoint's
    `is_accepted == True` filter is not vacuous.
    """
    from app.models import UserFollower
    row = UserFollower(local_user_id=local_user.id, remote_user_id=follower.id,
                       is_accepted=accepted)
    db.session.add(row)
    db.session.commit()
    return row


def test_a_users_followers_are_listed(app, db_session):
    """`user_followers` guards on `user is not None AND user.ap_followers_url`.
    `User.ap_followers_url` has no declared default and `make_user` never sets
    it, so a plain local user 404s -- every positive test here must set it.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    bob = make_user(instance, 'bob')
    db.session.commit()
    _follow(alice, bob)

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.json['type'] == 'Collection'
    assert response.json['id'] == 'https://test.piefed.local/u/alice/followers'
    assert response.json['totalItems'] == 1
    assert bob.ap_public_url in response.json['items']


def test_a_user_without_a_followers_url_is_404(app, db_session):
    """The SECOND conjunct of `user is not None and user.ap_followers_url`.
    The user exists and is local, so only the missing column can cause the 404 --
    which is what makes this test the one that kills that conjunct.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = None
    db.session.commit()

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 404


def test_a_banned_user_has_no_followers_collection(app, db_session):
    """`banned=False` in the user lookup. Set explicitly -- `User.banned`
    defaults to False and `make_user` also sets it explicitly, so leaving it
    alone would assert nothing about this clause.

    Not part of the brief's six tests; added because Step 3's first mutation
    (dropping `banned=False` from the lookup) has no other test in this group
    that seeds a banned user, and would otherwise be unkillable.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    alice.banned = True
    db.session.commit()

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 404


def test_an_unaccepted_follow_is_not_listed(app, db_session):
    """`UserFollower.is_accepted == True`. Passed False explicitly rather than
    left None, so the test states its premise.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    bob = make_user(instance, 'bob')
    db.session.commit()
    _follow(alice, bob, accepted=False)

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.json['totalItems'] == 0


def test_a_blocked_follower_is_not_listed(app, db_session):
    """The outer join against UserBlock excludes a follower who has blocked the
    ACCOUNT OWNER -- the REVERSE of what the route's own comment claims
    ("except those that are blocked by user", which reads as the owner
    blocking the follower).

    Traced from the query: `User` is joined via `UserFollower.remote_user_id`,
    so in each row `User` is the FOLLOWER, not the account owner. The
    UserBlock outer-join condition is
    `(User.id == UserBlock.blocker_id) & (UserFollower.local_user_id == UserBlock.blocked_id)`,
    i.e. `blocker_id == follower.id` and `blocked_id == owner.id`. The
    `UserBlock.id == None` filter then excludes exactly the rows where such a
    block exists -- so a follower is hidden when THEY blocked the owner, not
    when the owner blocked them. This is a genuine comment/code disagreement,
    seeded here to match the CODE, not the comment.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    bob = make_user(instance, 'bob')
    db.session.commit()
    _follow(alice, bob)
    from app.models import UserBlock
    db.session.add(UserBlock(blocker_id=bob.id, blocked_id=alice.id))
    db.session.commit()

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.json['totalItems'] == 0


def test_the_followers_collection_sets_cache_and_vary(app, db_session):
    """`user_followers` is the ONLY one of the nine collections that sets
    `Vary: Accept` -- and the only thing it varies on is nothing, since none
    of the nine negotiates on Accept. Registered as an asymmetry; asserted
    here so it is visible.

    Flask-Compress appends Accept-Encoding to every response, so the observed
    value is 'Accept, Accept-Encoding', not the bare 'Accept' the route sets.
    """
    site, instance = seed_actors()
    alice = make_user(instance, 'alice', local=True)
    alice.ap_followers_url = 'https://test.piefed.local/u/alice/followers'
    db.session.commit()

    response = collection_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=15'
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'


def test_an_unknown_user_followers_is_404(app, db_session):
    seed_actors()

    response = collection_get(app, '/u/nosuch/followers')

    assert response.status_code == 404


def _seed_local_feed(name='news', public=True):
    """A local feed the collection lookups resolve, with the AP URL columns set.

    `Feed.ap_followers_url`/`ap_following_url`/`ap_outbox_url` have NO declared
    defaults and `make_local_feed` does not set them -- `feed_outbox` and
    `feed_following` use them directly as the response `id`, so it comes back
    None unless a test sets it. `feed_followers`, covered here, builds its `id`
    from `SERVER_URL` instead and never reads `ap_followers_url`, but the three
    columns are set unconditionally so this helper is a single contract for
    Tasks 7-9 too.
    """
    from tests.factories import make_local_feed
    feed = make_local_feed(name, public=public)
    base = f'https://test.piefed.local/f/{name}'
    feed.ap_followers_url = f'{base}/followers'
    feed.ap_following_url = f'{base}/following'
    feed.ap_outbox_url = f'{base}/outbox'
    db.session.commit()
    return feed


def _feed_member(feed, user):
    from app.models import FeedMember
    row = FeedMember(feed_id=feed.id, user_id=user.id)
    db.session.add(row)
    db.session.commit()
    return row


def test_a_feed_followers_collection_counts_its_members(app, db_session):
    """`totalItems` is a real `FeedMember` count
    (`FeedMember.query.filter_by(feed_id=feed.id).count()`,
    app/activitypub/routes.py)."""
    site, instance = seed_actors()
    feed = _seed_local_feed('news', public=True)
    member = make_user(instance, 'member', local=True)
    _feed_member(feed, member)

    response = collection_get(app, '/f/news/followers')

    assert response.status_code == 200
    assert response.json['type'] == 'Collection'
    assert response.json['totalItems'] == 1


def test_the_feed_followers_items_list_is_always_empty(app, db_session):
    """PINS a defect. `totalItems` is a real count but `items` is hardcoded
    `[]` in the route itself, so the document says "one follower" and lists
    none.

    Hiding follower lists is a defensible privacy choice, but reporting a
    non-zero count beside an empty list is self-contradictory: a consumer
    cannot tell "hidden" from "none". `community_followers` (covered in this
    file) does the same. `user_followers` (also covered in this file), by
    contrast, populates its items and filters blocked and unaccepted follows.
    Two of the three contradict themselves; one does not.
    """
    site, instance = seed_actors()
    feed = _seed_local_feed('news', public=True)
    member = make_user(instance, 'member', local=True)
    _feed_member(feed, member)

    response = collection_get(app, '/f/news/followers')

    assert response.status_code == 200
    assert response.json['totalItems'] == 1
    assert response.json['items'] == []


def test_a_remote_feed_followers_request_is_400(app, db_session):
    """`'@' in actor` -> abort(400). All four feed collections have this check;
    none of the community or user collections in this file does.
    """
    seed_actors()

    response = collection_get(app, '/f/news@peer.example/followers')

    assert response.status_code == 400


def test_an_unknown_feed_followers_is_404(app, db_session):
    """`feed_followers` is the ONLY feed collection with a correct
    `if feed is not None: ... else: abort(404)`. Its three siblings all look the
    feed up the same way and then get an unknown feed wrong, but by TWO
    DIFFERENT mechanisms -- verified by reading app/activitypub/routes.py, and
    none of the three is exercised by a test in this file yet:

      * `feed_outbox` and `feed_following` have NO None check at all. They read
        `feed.public` directly, so an unknown feed raises AttributeError.
      * `feed_moderators_route` DOES have `if feed is not None:`, and simply has
        no `else`. An unknown feed falls off the end, the view returns None, and
        Flask raises TypeError -- the same failure this test's own guard
        produces when its `else: abort(404)` is deleted.

    Both mechanisms surface as a 500 rather than a 404, but they are not the
    same bug and a fix for one is not a fix for the other.
    """
    seed_actors()

    response = collection_get(app, '/f/nosuch/followers')

    assert response.status_code == 404


def test_the_feed_followers_collection_sets_its_cache_control(app, db_session):
    seed_actors()
    _seed_local_feed('news', public=True)

    response = collection_get(app, '/f/news/followers')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=15'


def test_a_non_public_feed_still_has_a_followers_collection(app, db_session):
    """PINS a defect. `feed_followers` never reads `feed.public` at all.
    `feed_outbox` and `feed_following` (app/activitypub/routes.py) both guard
    `if not feed.public: abort(403)` right after the same lookup -- verified by
    reading their code; neither is exercised by a test in this file yet, so
    this test claims only what `feed_followers` itself does, not what tests
    prove about its siblings.

    `public=False` is passed explicitly: it is also `Feed.public`'s column
    default, and `make_local_feed`'s own default, so leaving it implicit would
    assert nothing about this endpoint's behaviour.
    """
    seed_actors()
    _seed_local_feed('news', public=False)

    response = collection_get(app, '/f/news/followers')

    assert response.status_code == 200


def test_a_feed_moderators_collection_lists_its_owner(app, db_session):
    """Feeds have a single owner, wrapped in a list "in case we want to expand
    that in the future" per the source comment (app/activitypub/routes.py).
    Rendered as `ap_profile_id`, where `community_moderators_route` renders
    `public_url()` -- both asserted below, not just claimed.

    `Feed.user_id` (app/models.py) has NO declared default, and neither
    `make_local_feed` nor `_seed_local_feed` sets it, so it is None unless a
    test sets it explicitly -- unlike `seed_local_community`, which gives
    every community `user_id=1` (the `communityowner` user `seed_actors`
    creates) for free. Left at None, `db.session.query(User).get(feed.user_id)`
    is `.get(None)`, which returns None, and the very next line --
    `moderator.ap_profile_id` -- raises `AttributeError: 'NoneType' object has
    no attribute 'ap_profile_id'`: confirmed by running this test with the
    `feed.user_id` assignment below removed. So a real owner is created and
    assigned here, which the brief's literal test body omitted.
    """
    site, instance = seed_actors()
    feed = _seed_local_feed('news', public=True)
    owner = make_user(instance, 'feedowner', local=True)
    feed.user_id = owner.id
    db.session.commit()

    response = collection_get(app, '/f/news/moderators')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['totalItems'] == 1
    assert response.json['orderedItems'] == [owner.ap_profile_id]


def test_an_unknown_feed_moderators_returns_500(app, db_session):
    """PINS a crash, remotely reachable. DO NOT FIX -- a later task does.

    `feed_moderators_route` opens `if feed is not None:` and has NO `else`, so
    an unknown feed falls off the end of the function, returns None, and Flask
    raises. `feed_followers` -- twenty lines away in the same file -- gets this
    right with `else: abort(404)`. `feed_outbox` and `feed_following` (also
    uncovered here) get it wrong too, but by a DIFFERENT mechanism: they have
    no None check at all and read `feed.public` directly, so an unknown feed
    raises AttributeError instead. Both are pinned in later tasks by that
    other mechanism; this test pins only `feed_moderators_route`'s.

    Any instance can trigger this with GET /f/<anything>/moderators.

    THE 500 DOES NOT MATERIALISE AS A RESPONSE. `tests/conftest.py` sets
    `TESTING = True` on the test app with no `PROPAGATE_EXCEPTIONS` override,
    so Flask's `propagate_exceptions` property (which falls back to
    `testing or debug` when unset) is True, and `handle_exception` re-raises
    the exception instead of turning it into a 500 response; the test
    client's default `raise_server_exceptions=True` then lets it escape
    `collection_get` entirely. So the observable failure through this
    suite's client is the exception itself -- confirmed by running this
    test -- not a `response.status_code`: `TypeError: The view function for
    'activitypub.feed_moderators_route' did not return a valid response. The
    function either returned None or ended without a return statement.` This
    is a deviation from the brief, which assumed a 500 response; the brief
    said to report this rather than force it, so this test asserts what
    actually happens.
    """
    seed_actors()

    with pytest.raises(TypeError, match='did not return a valid response'):
        collection_get(app, '/f/nosuch/moderators')


def test_a_remote_feed_moderators_request_is_400(app, db_session):
    """`'@' in actor` -> abort(400), checked BEFORE the None-check that lets
    the 500 above through -- so a remote actor never reaches the broken path.
    """
    seed_actors()

    response = collection_get(app, '/f/news@peer.example/moderators')

    assert response.status_code == 400
