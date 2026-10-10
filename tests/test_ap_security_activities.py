"""Security track S0.2: /activities/<type>/<id> serves a logged outbound activity
only to someone its own addressing entitles. It used to serve every logged
activity -- DMs and followers-only ones included -- to anyone with the id.
"""
import json

import pytest

from app import db
from app.models import Instance
from tests.factories import (make_activitypub_log, make_community, make_community_member, make_follow, make_instance,
                             make_site, make_user, signed_get_headers)

pytestmark = pytest.mark.usefixtures('redis_double')

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
SERVER = 'https://test.piefed.local'


def _instance(domain):
    return db.session.query(Instance).filter_by(domain=domain).first() or make_instance(domain)


def _remote(domain, name):
    return make_user(_instance(domain), name, with_keys=True)


def _local(name):
    user = make_user(_instance('test.piefed.local'), name, local=True)
    user.ap_profile_id = f'{SERVER}/u/{name}'
    db.session.commit()
    return user


def _log(kind, ident, **activity):
    activity_id = f'{SERVER}/activities/{kind}/{ident}'
    make_activitypub_log(activity_id, direction='out', activity_type=kind.title(),
                         activity_json=json.dumps({'id': activity_id, 'type': kind.title(), **activity}))
    return f'/activities/{kind}/{ident}'


def _get(app, path, headers=None):
    with app.test_client() as client:
        return client.get(path, headers=headers or {})


def _assert_refused(response):
    assert response.status_code == 404
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.data == b''


def test_a_public_activity_is_served_unsigned(app, db_session):
    make_site()
    author = _local('author')
    path = _log('create', 'pub', actor=author.ap_profile_id, to=[PUBLIC])
    response = _get(app, path)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=120'
    assert response.json['to'] == [PUBLIC]


def test_a_dm_is_refused_to_an_anonymous_requester(app, db_session):
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'dm1', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    _assert_refused(_get(app, path))


def test_a_dm_is_served_privately_to_its_signed_addressee(app, db_session):
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'dm2', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    response = _get(app, path, signed_get_headers(path, recipient))
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    # the compression layer appends Accept-Encoding, so check the tokens rather than the exact string
    assert {'Accept', 'Signature'} <= {token.strip() for token in response.headers['Vary'].split(',')}
    assert response.json['to'] == [recipient.ap_profile_id]


def test_a_dm_is_refused_to_a_signed_stranger_on_another_host(app, db_session):
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    stranger = _remote('other.example', 'mallory')
    path = _log('create', 'dm3', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    _assert_refused(_get(app, path, signed_get_headers(path, stranger)))


def test_a_peer_instance_actor_verifying_our_activity_is_served(app, db_session):
    """Review Focus 2: a peer refetches one of our activities to verify it and
    signs with ITS instance actor, not with the addressee. It already received
    the activity through its shared inbox, so serving it exposes nothing new."""
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    peer_instance_actor = _remote('peer.example', 'instanceactor')
    path = _log('create', 'dm4', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    response = _get(app, path, signed_get_headers(path, peer_instance_actor))
    assert response.status_code == 200


def test_a_followers_addressed_activity_is_served_to_a_signer_on_a_followers_host(app, db_session):
    make_site()
    author = _local('author')
    follower = _remote('peer.example', 'bob')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    peer_instance_actor = _remote('peer.example', 'instanceactor')
    path = _log('create', 'fo1', actor=author.ap_profile_id, to=[f'{author.public_url()}/followers'])
    assert _get(app, path, signed_get_headers(path, peer_instance_actor)).status_code == 200


def test_a_followers_addressed_activity_is_refused_to_a_non_follower_host(app, db_session):
    make_site()
    author = _local('author')
    stranger = _remote('other.example', 'mallory')
    path = _log('create', 'fo2', actor=author.ap_profile_id, to=[f'{author.public_url()}/followers'])
    _assert_refused(_get(app, path, signed_get_headers(path, stranger)))


def test_an_anonymous_request_right_after_an_entitled_one_is_still_refused(app, db_session):
    """The view used to be @cache.cached on the URL alone, which would hand an
    entitled requester's 200 to the next anonymous caller."""
    make_site()
    author = _local('author')
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'dm5', actor=author.ap_profile_id, to=[recipient.ap_profile_id])
    assert _get(app, path, signed_get_headers(path, recipient)).status_code == 200
    _assert_refused(_get(app, path))


def test_an_activity_with_no_addressing_is_refused(app, db_session):
    make_site()
    author = _local('author')
    path = _log('create', 'none', actor=author.ap_profile_id)
    _assert_refused(_get(app, path))


def test_an_activity_whose_actor_is_not_local_still_uses_its_addressing(app, db_session):
    """local_actor is None, so rule 4 is off; rules 1-3 still decide."""
    make_site()
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'noactor', actor='https://gone.example/u/x', to=[recipient.ap_profile_id])
    assert _get(app, path, signed_get_headers(path, recipient)).status_code == 200


def test_a_stored_remote_community_actor_never_entitles_through_its_followers_collection(app, db_session):
    """find_actor_by_url resolves stored remote actors too. Rule 4 counts OUR members of a community, which is
    meaningless for a remote one: a signer hosting one of them must not be entitled."""
    make_site()
    signer = _remote('peer.example', 'bob')  # first row: make_community owns itself to user 1 / instance 1
    community = make_community('far', host='other.example')
    community.ap_id = 'far@other.example'
    db.session.commit()
    make_community_member(signer, community)
    path = _log('create', 'remotecomm', actor=community.ap_profile_id, to=[community.ap_followers_url])
    _assert_refused(_get(app, path, signed_get_headers(path, signer)))


def test_a_stored_remote_user_actor_never_entitles_through_its_followers_collection(app, db_session):
    make_site()
    author = _remote('other.example', 'carol')
    follower = _remote('peer.example', 'bob')
    make_follow(author, follower, is_accepted=True, is_inward=True)
    path = _log('create', 'remoteuser', actor=author.ap_profile_id, to=[f'{author.public_url()}/followers'])
    _assert_refused(_get(app, path, signed_get_headers(path, follower)))


def test_an_activity_with_a_non_string_actor_is_judged_on_its_addressing(app, db_session):
    make_site()
    recipient = _remote('peer.example', 'bob')
    path = _log('create', 'dictactor', actor={'id': 'x'}, to=[recipient.ap_profile_id])
    _assert_refused(_get(app, path))


def test_a_row_with_an_empty_document_is_refused(app, db_session):
    make_site()
    make_activitypub_log(f'{SERVER}/activities/create/empty', direction='out', activity_json='{}')
    _assert_refused(_get(app, '/activities/create/empty'))


def test_a_row_with_a_null_document_is_refused(app, db_session):
    make_site()
    make_activitypub_log(f'{SERVER}/activities/create/null', direction='out', activity_json=None)
    _assert_refused(_get(app, '/activities/create/null'))


def test_a_row_whose_document_is_not_an_object_is_refused(app, db_session):
    make_site()
    make_activitypub_log(f'{SERVER}/activities/create/list', direction='out', activity_json='["x"]')
    _assert_refused(_get(app, '/activities/create/list'))


def test_a_missing_row_is_refused_identically(app, db_session):
    make_site()
    _assert_refused(_get(app, '/activities/create/missing'))


def test_a_vote_with_only_an_audience_is_served_to_the_audience_host_only(app, db_session):
    """Pins today's spec rule for our outbound votes, which carry only
    `audience: <community>` (app/shared/tasks/likes.py). The community's host
    is entitled by rule 3; a third host that saw the vote through the
    community's Announce is not. If the owner rules that a public community
    audience counts as public, flip the second assertion."""
    make_site()
    voter = _local('voter')
    community_host_actor = _remote('lemmy.example', 'instanceactor')
    third_host_actor = _remote('third.example', 'instanceactor')
    path = _log('like', 'v1', actor=voter.ap_profile_id, object='https://lemmy.example/post/1',
                audience='https://lemmy.example/c/news')
    assert _get(app, path, signed_get_headers(path, community_host_actor)).status_code == 200
    _assert_refused(_get(app, path, signed_get_headers(path, third_host_actor)))
