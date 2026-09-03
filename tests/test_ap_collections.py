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
