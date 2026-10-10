"""S3.1: the instance actor's advertised outbox exists (spec 2026-10-10-ap-security-track-design.md)."""
from tests.factories import make_site

AP = {'Accept': 'application/activity+json'}


def test_instance_actor_outbox_is_an_empty_ordered_collection(app, db_session):
    make_site()
    with app.test_client() as client:
        response = client.get('/actor/outbox', headers=AP)

    assert response.status_code == 200
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'public, max-age=60'
    body = response.json
    assert body['type'] == 'OrderedCollection'
    assert body['id'] == 'https://test.piefed.local/actor/outbox'
    assert body['totalItems'] == 0
    assert body['orderedItems'] == []


def test_instance_actor_outbox_matches_what_the_actor_document_advertises(app, db_session):
    make_site()
    with app.test_client() as client:
        actor = client.get('/actor', headers=AP).json
        outbox = client.get('/actor/outbox', headers=AP).json

    assert actor['outbox'] == outbox['id']


def test_instance_actor_outbox_pages_like_every_other_outbox(app, db_session):
    make_site()
    with app.test_client() as client:
        page = client.get('/actor/outbox?page=1', headers=AP)
        bad = client.get('/actor/outbox?page=x', headers=AP)

    assert page.status_code == 200
    assert page.json['type'] == 'OrderedCollectionPage'
    assert page.json['partOf'] == 'https://test.piefed.local/actor/outbox'
    assert page.json['orderedItems'] == []
    assert 'next' not in page.json
    assert bad.status_code == 400
