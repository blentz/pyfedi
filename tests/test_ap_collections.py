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
    """max-age=10. Its seven siblings use 5, 15, 120 and -- for
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

    `community_featured` applies no such filter -- that asymmetry is pinned in
    the next task.
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
