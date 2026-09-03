"""tests/test_ap_collections.py"""
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
