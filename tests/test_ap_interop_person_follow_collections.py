"""Interop C1 item 6 (spec 2026-10-11): a Person names its `followers` and `following` collections, and both are
count-only (owner ruling, as D175 serves a community's followers): a real `totalItems`, an empty `orderedItems`, so
no follower or followed account is disclosed.

The follow table stores both directions under one pair of columns. A remote follower of a local user is an inward
row (local_user_id = the followed user); a local follower is the follower's own outward row (remote_user_id = the
followed user). The old route counted every row with local_user_id = the user, so the people a user follows were
listed as their followers.
"""
import pytest

from app import db
from app.models import Site, UserBlock, utcnow
from tests.factories import make_community, make_community_member, make_follow, make_instance, make_user
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get

SERVER = 'https://test.piefed.local'
SUFFIXES = ['followers', 'following']


def _alice():
    seed_actors()
    return make_user(None, 'alice', local=True)


def test_the_person_document_names_both_collections(app, db_session):
    _alice()

    response = ap_get(app, '/u/alice')

    assert response.status_code == 200
    assert response.json['followers'] == f'{SERVER}/u/alice/followers'
    assert response.json['following'] == f'{SERVER}/u/alice/following'


def test_followers_counts_accepted_local_and_remote_followers_and_lists_none(app, db_session):
    alice = _alice()
    remote = make_instance('remote.example')
    bob = make_user(remote, 'bob')                  # remote, follows alice: inward row
    carol = make_user(None, 'carol', local=True)    # local, follows alice: carol's outward row
    dave = make_user(remote, 'dave')                # remote, request pending
    erin = make_user(make_instance('other.example'), 'erin')   # alice follows erin: NOT a follower
    make_follow(alice, bob, is_accepted=True, is_inward=True)
    make_follow(carol, alice, is_accepted=True, is_inward=False)
    make_follow(alice, dave, is_accepted=None, is_inward=True)
    make_follow(alice, erin, is_accepted=True, is_inward=False)

    response = ap_get(app, '/u/alice/followers')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == f'{SERVER}/u/alice/followers'
    assert response.json['totalItems'] == 2
    assert response.json['orderedItems'] == []
    assert 'items' not in response.json
    body = response.get_data(as_text=True)
    assert bob.ap_public_url not in body and '/u/carol' not in body and erin.ap_public_url not in body


def test_followers_skips_a_banned_or_deleted_follower_and_one_the_user_blocked(app, db_session):
    alice = _alice()
    remote = make_instance('remote.example')
    banned, deleted, blocked = (make_user(remote, name) for name in ('banned', 'deleted', 'blocked'))
    banned.banned = True
    deleted.deleted = True
    db.session.add(UserBlock(blocker_id=alice.id, blocked_id=blocked.id))   # D182: the owner's block hides
    db.session.commit()
    for follower in (banned, deleted, blocked):
        make_follow(alice, follower, is_accepted=True, is_inward=True)

    assert ap_get(app, '/u/alice/followers').json['totalItems'] == 0


def test_following_counts_users_followed_and_communities_joined(app, db_session):
    alice = _alice()
    remote = make_instance('remote.example')
    bob = make_user(remote, 'bob')                  # followed, accepted
    carol = make_user(None, 'carol', local=True)    # followed, accepted (local follow)
    dave = make_user(remote, 'dave')                # followed, still pending
    erin = make_user(remote, 'erin')                # follows alice: NOT followed by her
    make_follow(alice, bob, is_accepted=True, is_inward=False)
    make_follow(alice, carol, is_accepted=True, is_inward=False)
    make_follow(alice, dave, is_accepted=None, is_inward=False)
    make_follow(alice, erin, is_accepted=True, is_inward=True)
    make_community_member(alice, make_community(name='books'))
    banned_from = make_community_member(alice, make_community(name='films'))
    banned_from.is_banned = True
    db.session.commit()

    response = ap_get(app, '/u/alice/following')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'
    assert response.json['id'] == f'{SERVER}/u/alice/following'
    assert response.json['totalItems'] == 3     # bob, carol, books
    assert response.json['orderedItems'] == []
    body = response.get_data(as_text=True)
    assert bob.ap_public_url not in body and '/c/books' not in body


def test_following_skips_a_banned_community_and_one_deleted_on_its_home_server(app, db_session):
    """Task 9 review. A membership of a banned community never counted, untested; one of a community its home
    server deleted (ap_deleted_at, interop C1 item 10) counted until the row went."""
    alice = _alice()
    make_community_member(alice, make_community(name='books'))
    banned = make_community(name='films')
    banned.banned = True
    gone = make_community(name='memes', host='remote.example')
    gone.ap_deleted_at = utcnow()
    make_community_member(alice, banned)
    make_community_member(alice, gone)
    db.session.commit()

    response = ap_get(app, '/u/alice/following')

    assert response.json['totalItems'] == 1     # books


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_both_collections_use_the_collection_cache_policy(app, db_session, suffix):
    _alice()

    response = ap_get(app, f'/u/alice/{suffix}')

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=60'
    assert response.headers['Vary'] == 'Accept, Accept-Encoding'   # Flask-Compress appends Accept-Encoding


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_the_lookup_ignores_case_and_answers_the_canonical_id(app, db_session, suffix):
    _alice()

    assert ap_get(app, f'/u/ALICE/{suffix}').json['id'] == f'{SERVER}/u/alice/{suffix}'


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_a_browser_is_sent_to_the_profile(app, db_session, suffix):
    """D177, as every other collection."""
    _alice()
    with app.test_client() as client:
        response = client.get(f'/u/alice/{suffix}', headers={'Accept': 'text/html'})

    assert (response.status_code, response.headers['Location']) == (302, '/u/alice')


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_a_deleted_user_is_gone(app, db_session, suffix):
    """S3.2, as user_profile: account deletion sets both flags, and `deleted` decides."""
    alice = _alice()
    alice.deleted = True
    alice.banned = True
    db.session.commit()

    response = ap_get(app, f'/u/alice/{suffix}')

    assert response.status_code == 410
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_a_banned_user_stays_404_so_the_ban_is_reversible(app, db_session, suffix):
    alice = _alice()
    alice.banned = True
    db.session.commit()

    assert ap_get(app, f'/u/alice/{suffix}').status_code == 404


@pytest.mark.parametrize('path', ['/u/nosuch/{}', '/u/bob/{}', '/u/bob@remote.example/{}'])
@pytest.mark.parametrize('suffix', SUFFIXES)
def test_an_unknown_or_remote_user_is_404(app, db_session, path, suffix):
    """Only a local user's collections are served here; a remote one lives on its own server."""
    _alice()
    make_user(make_instance('remote.example'), 'bob')

    assert ap_get(app, path.format(suffix)).status_code == 404


@pytest.mark.parametrize('suffix', SUFFIXES)
def test_secure_mode_refuses_an_unsigned_fetch(app, db_session, suffix):
    """The app-wide secure-mode hook covers the new route too: it is a collection, not an actor document, so it is
    refused rather than served in minimal form."""
    _alice()
    db.session.get(Site, 1).require_signed_fetch = True
    db.session.commit()

    response = ap_get(app, f'/u/alice/{suffix}')

    assert response.status_code == 401
