"""S3.4: the community outbox is gated like the community actor document (spec 2026-10-10)."""
import pytest

from app import db
from tests.factories import make_community
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get


@pytest.mark.parametrize('flag', ['private', 'local_only'])
def test_a_non_federating_community_outbox_is_403(app, db_session, flag):
    seed_actors('test.piefed.local')
    community = make_community(name='hidden')
    setattr(community, flag, True)
    db.session.commit()

    assert ap_get(app, '/c/hidden/outbox').status_code == 403
    assert ap_get(app, '/c/hidden').status_code == 403   # the actor document already agreed


def test_an_ordinary_community_outbox_is_still_served(app, db_session):
    seed_actors('test.piefed.local')
    make_community(name='open')

    response = ap_get(app, '/c/open/outbox')

    assert response.status_code == 200
    assert response.json['type'] == 'OrderedCollection'


COLLECTION_SUFFIXES = ['featured', 'moderators', 'followers']


@pytest.mark.parametrize('suffix', COLLECTION_SUFFIXES)
@pytest.mark.parametrize('flag', ['private', 'local_only'])
def test_a_non_federating_community_collection_is_403(app, db_session, flag, suffix):
    seed_actors('test.piefed.local')
    community = make_community(name='hidden')
    setattr(community, flag, True)
    db.session.commit()

    assert ap_get(app, f'/c/hidden/{suffix}').status_code == 403


@pytest.mark.parametrize('suffix', COLLECTION_SUFFIXES)
def test_an_ordinary_community_collection_is_still_served(app, db_session, suffix):
    seed_actors('test.piefed.local')
    make_community(name='open')

    assert ap_get(app, f'/c/open/{suffix}').status_code == 200
