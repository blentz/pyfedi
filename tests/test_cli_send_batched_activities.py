"""`flask send-batched-activities` (app/cli.py), which flushes the queued
`ActivityBatch` rows for each (instance, community) pair as Announces of a
list of objects."""
from app import cli, db
from app.models import ActivityBatch
from tests.factories import make_community, make_instance
from tests.test_actor_profiles import seed_actors


def test_a_flush_sends_at_most_100_objects_per_announce(app, db_session, monkeypatch):
    """D54, fixed (owner ruling). The flush put every queued row for a pair
    into ONE Announce, however many there were, while the inbound side now
    processes at most 100 objects of an announced list. 101 queued rows are
    now sent as two Announces, of 100 and 1, each with its own id, and every
    row is deleted.
    """
    seed_actors()
    peer = make_instance('batch-peer.example', 'piefed')
    peer.inbox = 'https://batch-peer.example/inbox'
    community = make_community(name='books', host='test.piefed.local')
    community.ap_followers_url = 'https://test.piefed.local/c/books/followers'
    db.session.commit()
    for n in range(101):
        db.session.add(ActivityBatch(instance_id=peer.id, community_id=community.id,
                                     payload={'id': f'https://test.piefed.local/activities/like/{n}'}))
    db.session.commit()
    sent = []
    monkeypatch.setattr('app.cli.send_post_request', lambda inbox, body, *args, **kwargs: sent.append((inbox, body)))

    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    result = app.test_cli_runner().invoke(args=['send-batched-activities'])

    assert result.exception is None
    assert [len(body['object']) for inbox, body in sent] == [100, 1]
    assert {inbox for inbox, body in sent} == {'https://batch-peer.example/inbox'}
    assert sent[0][1]['id'] != sent[1][1]['id']
    assert sent[0][1]['object'][0]['id'].endswith('/like/0')
    assert sent[1][1]['object'][0]['id'].endswith('/like/100')
    assert ActivityBatch.query.count() == 0
