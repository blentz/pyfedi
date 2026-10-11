"""Interop C1 item 8 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): an array-valued `type`.

JSON-LD lets `type` be an array. The inbox compared it as a string, so an array activity type was dropped
silently, and an array object type reached `'Unacceptable type (create): ' + object_type`, a TypeError that
failed the Celery task. The inbox now reduces each array to one type on a copy it dispatches; the JSON that is
stored, logged and LD-verified stays as it was sent.
"""
import json
from types import SimpleNamespace

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import replay_inbox_request
from app.activitypub.util import NO_STRING_TYPE_MESSAGE, current_as_sent_json, normalise_activity_types
from app.models import ActivityPubLog
from app.utils import utcnow
from tests.conftest import ld_signed_body, unsigned_but_precheck_clean_headers
from tests.factories import inbox_activity, make_community, make_instance, make_site, make_user, seed_community_owner
from tests.test_inbox_dispatch_create_update import create_activity
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch

pytestmark = pytest.mark.usefixtures('redis_double')

LOCAL_ACTOR = 'https://test.piefed.local/u/bob'


# ---- normalise_activity_types ----------------------------------------------------------------------------------

@pytest.mark.parametrize('value, expected', [
    (['Like'], 'Like'),
    (['litepub:EmojiReact', 'Like'], 'Like'),   # a type the inbox dispatches on beats an earlier unknown one
    (['Foo', 'Bar'], 'Foo'),                     # none it dispatches on: the first string
    ([7, 'Note'], 'Note'),                       # non-strings are skipped
])
def test_an_array_activity_type_becomes_one_string(value, expected):
    activity = {'id': 'https://peer.example/a/1', 'type': value, 'actor': 'https://peer.example/u/alice',
                'object': 'https://peer.example/o/1'}
    assert normalise_activity_types(activity)['type'] == expected


def test_the_activity_it_was_given_is_never_changed():
    activity = {'id': 'https://peer.example/a/1', 'type': ['Create'], 'actor': 'https://peer.example/u/alice',
                'object': {'id': 'https://peer.example/o/1', 'type': ['Note', 'as:Note']}}
    original = json.loads(json.dumps(activity))

    normalised = normalise_activity_types(activity)

    assert normalised['type'] == 'Create' and normalised['object']['type'] == 'Note'
    assert normalised is not activity and normalised['object'] is not activity['object']
    assert activity == original


def test_an_activity_whose_types_are_all_strings_is_returned_as_it_is():
    activity = {'type': 'Like', 'object': {'type': 'Note'}}
    assert normalise_activity_types(activity) is activity


def test_an_announced_activitys_object_type_is_normalised():
    announce = {'type': 'Announce', 'object': {'type': 'Create', 'object': {'type': ['Note', 'Article']}}}
    assert normalise_activity_types(announce)['object']['object']['type'] == 'Note'


def test_an_array_announce_type_still_reaches_the_announced_object():
    announce = {'type': ['Announce'], 'object': {'type': 'Create', 'object': {'type': ['Page']}}}
    normalised = normalise_activity_types(announce)
    assert normalised['type'] == 'Announce' and normalised['object']['object']['type'] == 'Page'


def test_the_object_of_an_object_is_left_alone_outside_an_announce():
    """Only an Announce's inner object is something the dispatcher reads the type of."""
    undo = {'type': ['Undo'], 'object': {'type': 'Like', 'object': {'type': ['Note']}}}
    assert normalise_activity_types(undo)['object']['object']['type'] == ['Note']


@pytest.mark.parametrize('holder', ['activity', 'object', 'announced object'])
def test_an_array_with_no_string_is_refused(holder):
    announce = {'type': 'Announce', 'object': {'type': 'Create', 'object': {'type': 'Note'}}}
    target = {'activity': announce, 'object': announce['object'],
              'announced object': announce['object']['object']}[holder]
    target['type'] = [{'@id': 'x'}, 3]
    assert normalise_activity_types(announce) is None


# ---- the dispatcher ----------------------------------------------------------------------------------------------

def _create_setup(app, monkeypatch):
    """The Create/Update arm's prerequisites, as tests/test_inbox_dispatch_create_update.py builds them."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    monkeypatch.setattr(activitypub_routes, 'find_community', lambda request_json, author=None: community)
    monkeypatch.setattr(activitypub_routes, 'ensure_domains_match', lambda activity: True)
    return author


def test_an_array_activity_type_is_dispatched(app, db_session, monkeypatch):
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()
    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote', lambda *args, **kwargs: calls.append(args))
    activity = inbox_activity(actor, activity_type=['Like', 'litepub:EmojiReact'])

    dispatch(activity)

    assert len(calls) == 1
    assert calls[0][2]['type'] == 'Like'
    assert activity['type'] == ['Like', 'litepub:EmojiReact']


def test_an_array_object_type_is_dispatched(app, db_session, monkeypatch):
    author = _create_setup(app, monkeypatch)
    calls = record_moderation(monkeypatch, 'process_new_content')

    dispatch(create_activity(author, {'type': ['Note', 'as:Note'], 'id': 'https://peer.example/post/1'}))

    assert len(calls['process_new_content']) == 1
    args, _kwargs = calls['process_new_content'][0]
    assert args[3]['object']['type'] == 'Note'


def test_an_unknown_array_object_type_falls_through_and_is_logged(app, db_session, monkeypatch):
    """No exception, and the stored JSON is the document as sent."""
    author = _create_setup(app, monkeypatch)

    dispatch(create_activity(author, {'type': ['Foo', 'Bar'], 'id': 'https://peer.example/x/1'}))

    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert log.exception_message == 'Unacceptable type (create): Foo'
    assert json.loads(log.activity_json)['object']['type'] == ['Foo', 'Bar']


@pytest.mark.parametrize('object_type, shown', [(5, '5'), (None, 'None'), ({'@id': 'Note'}, "{'@id': 'Note'}")])
def test_the_unacceptable_type_log_line_survives_any_type_value(app, db_session, monkeypatch, object_type, shown):
    author = _create_setup(app, monkeypatch)

    dispatch(create_activity(author, {'type': object_type, 'id': 'https://peer.example/x/1'}))

    assert ActivityPubLog.query.one().exception_message == f'Unacceptable type (create): {shown}'


def test_the_dispatcher_refuses_an_array_of_non_strings(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    activity = inbox_activity(actor, activity_type=[1, 2])

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.result == 'failure' and log.exception_message == NO_STRING_TYPE_MESSAGE
    assert json.loads(log.activity_json)['type'] == [1, 2]


# ---- shared_inbox and replay -------------------------------------------------------------------------------------

@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_inbox_request', lambda *a, **kw: calls.append((a, kw)))
    return calls


def _post_unsigned(app, activity):
    body = json.dumps(activity).encode('utf8')
    with app.test_client() as client:
        return client.post('/inbox', data=body, headers=unsigned_but_precheck_clean_headers(body),
                           content_type='application/activity+json')


def test_the_inbox_refuses_an_array_of_non_strings_before_anything_else(app, db_session, dispatched):
    make_site()
    activity = {'id': 'https://peer.example/a/1', 'type': [1, 2], 'actor': 'https://peer.example/u/alice',
                'object': 'https://peer.example/o/1'}

    response = _post_unsigned(app, activity)

    assert response.status_code == 200 and dispatched == []
    assert ActivityPubLog.query.one().exception_message == NO_STRING_TYPE_MESSAGE


def test_the_inbox_reads_an_array_announce_type(app, db_session, dispatched):
    """An Announce of an activity by a local actor is dropped as a duplicate. With `type: ['Announce']` the check
    used to be skipped and the request went on to fail its signature checks instead."""
    make_site()
    activity = {'id': 'https://peer.example/a/1', 'type': ['Announce'], 'actor': 'https://peer.example/c/news',
                'object': {'id': 'https://test.piefed.local/activities/1', 'type': 'Create',
                           'actor': LOCAL_ACTOR, 'object': 'https://test.piefed.local/post/1'}}

    response = _post_unsigned(app, activity)

    assert response.status_code == 200 and dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Activity about local content which is already present'


def test_the_inbox_reads_an_array_delete_type_for_an_account_deletion(app, db_session, dispatched):
    make_site()
    activity = {'id': 'https://peer.example/a/1', 'type': ['Delete'], 'actor': 'https://peer.example/u/nobody',
                'object': 'https://peer.example/u/nobody'}

    response = _post_unsigned(app, activity)

    assert response.status_code == 200 and dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Does not exist here'


def test_the_ld_signature_is_checked_against_the_document_as_sent(app, signing_peer, dispatched,
                                                                  no_network_ld_signing):
    """Only an LD signature, so it alone decides. It was made over `type: ['Like']`; checking it against a
    normalised copy would fail with 400. The dispatcher gets the original and normalises its own copy."""
    body = ld_signed_body(signing_peer, activity_type=['Like'])
    with app.test_client() as client:
        response = client.post('/inbox', data=body, headers=unsigned_but_precheck_clean_headers(body),
                               content_type='application/activity+json')

    assert response.status_code == 200
    assert len(dispatched) == 1
    assert dispatched[0][0][0]['type'] == ['Like']
    assert dispatched[0][1] == {'ld_signed': True}


def test_an_ld_signed_array_create_still_gets_its_origin_refetch(app, db_session, http_mock, monkeypatch):
    """Review Focus: verify_ld_accepted_activity replaces a Create's object by its origin's copy, choosing by type.
    Normalising after it would let `type: ['Create']` skip the refetch and be processed as sent."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    object_id = 'https://peer.example/objects/1'
    origin = http_mock.get(object_id).respond(404)
    activity = inbox_activity(actor, activity_type=['Create'],
                              object={'id': object_id, 'type': 'Note', 'attributedTo': actor.ap_profile_id})

    activitypub_routes.process_inbox_request(activity, True, ld_signed=True)

    assert origin.called
    assert 'LD-signed activity dropped' in ActivityPubLog.query.one().exception_message


def test_replay_reads_an_array_announce_type(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)

    replay_inbox_request({'id': 'https://peer.example/a/1', 'type': ['Announce'],
                          'actor': 'https://peer.example/c/news',
                          'object': {'id': 'https://test.piefed.local/activities/1', 'type': 'Create',
                                     'actor': LOCAL_ACTOR, 'object': 'https://test.piefed.local/post/1'}})

    assert ActivityPubLog.query.one().exception_message == \
        'REPLAY: Activity about local content which is already present'


# ---- stored and forwarded as sent --------------------------------------------------------------------------------

def test_an_ld_signed_array_type_is_stored_as_sent(app, db_session, monkeypatch):
    """Replay re-verifies the LD signature against the stored JSON, so the array must still be there."""
    author = _create_setup(app, monkeypatch)
    monkeypatch.setattr(activitypub_routes, 'verify_ld_accepted_activity', lambda activity: (activity, None))

    activitypub_routes.process_inbox_request(
        create_activity(author, {'type': ['Foo', 'Bar'], 'id': 'https://peer.example/x/1'}), True, ld_signed=True)

    log = ActivityPubLog.query.one()
    assert log.exception_message.endswith('Unacceptable type (create): Foo')
    assert json.loads(log.activity_json)['object']['type'] == ['Foo', 'Bar']


def test_dispatch_knows_the_document_as_sent_only_for_the_length_of_the_task(app, db_session, monkeypatch):
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()
    seen = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote', lambda *a, **kw: seen.append(current_as_sent_json.get()))
    activity = inbox_activity(actor, activity_type=['Like'])

    dispatch(activity)

    assert seen == [activity]
    assert current_as_sent_json.get() is None


def _forwarded(activity):
    sent = []
    instance = SimpleNamespace(id=2, online=lambda: True, inbox='https://peer.example/inbox', software='other')
    community = SimpleNamespace(is_local=lambda: True, public_url=lambda: 'https://test.piefed.local/c/news',
                                following_instances=lambda **kw: [instance],
                                private_key='k', ap_profile_id='https://test.piefed.local/c/news')
    creator = SimpleNamespace(banned=False, instance_id=1)
    monkey = pytest.MonkeyPatch()
    with monkey.context() as m:
        m.setattr(activitypub_routes, 'send_to_remote_instance_fast', lambda inbox, key, key_id, payload: sent.append(payload))
        m.setattr(activitypub_routes, 'instance_banned', lambda inbox: False)
        m.setattr(activitypub_routes, 'awaken_dormant_instance', lambda instance: None)
        activitypub_routes.announce_activity_to_followers(community, creator, activity)
    return sent[0]['object']


def test_a_forwarded_activity_is_the_one_as_sent(app):
    sent = {'id': 'https://peer.example/a/1', 'type': ['Like'], 'object': 'https://test.piefed.local/post/1'}
    token = current_as_sent_json.set(sent)
    try:
        forwarded = _forwarded(normalise_activity_types(sent))
    finally:
        current_as_sent_json.reset(token)

    assert forwarded['type'] == ['Like']


def test_another_activity_is_forwarded_as_given(app):
    token = current_as_sent_json.set({'id': 'https://peer.example/a/1', 'type': ['Like']})
    try:
        forwarded = _forwarded({'id': 'https://peer.example/a/2', 'type': 'Like'})
    finally:
        current_as_sent_json.reset(token)

    assert forwarded['id'].endswith('/a/2')


def test_a_relayed_array_announce_is_refused_and_stored_as_sent(app, db_session, monkeypatch):
    """The relay filter reads the normalised type, so `['Announce']` is a boost; the log keeps what was sent."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')

    activitypub_routes.process_inbox_request(inbox_activity(actor, activity_type=['Announce']), True, relay_id=1)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'relayed boost'
    assert json.loads(log.activity_json)['type'] == ['Announce']


def test_a_relayed_array_like_is_dispatched(app, db_session, monkeypatch):
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()
    calls = []
    monkeypatch.setattr(activitypub_routes, 'process_upvote', lambda *args, **kwargs: calls.append(args))

    activitypub_routes.process_inbox_request(inbox_activity(actor, activity_type=['Like']), True, relay_id=1)

    assert len(calls) == 1
