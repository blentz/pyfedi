"""Interop C1 item 8 (docs/superpowers/specs/2026-10-11-ap-interop-c1-design.md): an array-valued `type`.

JSON-LD lets `type` be an array. The inbox compared it as a string, so an array activity type was dropped
silently, and an array object type reached `'Unacceptable type (create): ' + object_type`, a TypeError that
failed the Celery task. The inbox now reduces each array to one type on a copy it dispatches; the JSON that is
stored, logged and LD-verified stays as it was sent.
"""
import json

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import replay_inbox_request
from app.activitypub import util as activitypub_util
from app.activitypub.util import NO_STRING_TYPE_MESSAGE, normalise_activity_types, verify_ld_accepted_activity
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
    announce = {'type': 'Announce', 'object': {'type': 'Create', 'object': {'type': ['Note', 'as:Note']}}}
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


def test_a_handler_written_row_stores_an_array_delivery_as_sent(app, db_session, monkeypatch):
    """process_upvote writes this row itself, from the normalised copy it was handed; the row keeps what was sent."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()
    activity = inbox_activity(actor, activity_type=['Like', 'litepub:EmojiReact'],
                              object_uri='https://peer.example/objects/missing')

    dispatch(activity)

    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Unfound object https://peer.example/objects/missing'
    assert json.loads(log.activity_json)['type'] == ['Like', 'litepub:EmojiReact']


def test_a_handler_written_row_of_an_all_string_delivery_is_unchanged(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')
    actor.ap_fetched_at = utcnow()
    db.session.commit()

    dispatch(inbox_activity(actor, activity_type='Like', object_uri='https://peer.example/objects/missing'))

    assert json.loads(ActivityPubLog.query.one().activity_json)['type'] == 'Like'


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


# ---- a gate and the handler behind it read the same type ----------------------------------------------------------

@pytest.mark.parametrize('value', [['Create', 'Delete'], ['Like', 'Dislike'], ['ChatMessage', 'Note']])
def test_an_array_naming_two_dispatched_types_is_refused(value):
    assert normalise_activity_types({'type': value, 'object': 'https://peer.example/o/1'}) is None


def test_an_array_naming_one_dispatched_type_and_extensions_resolves_to_it():
    assert normalise_activity_types({'type': ['Foo', 'Create', 'Create']})['type'] == 'Create'


def test_the_dispatcher_refuses_an_ambiguous_array_type(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    actor = make_user(make_instance('peer.example'), 'alice')

    dispatch(inbox_activity(actor, activity_type=['Create', 'Delete']))

    log = ActivityPubLog.query.one()
    assert log.exception_message == NO_STRING_TYPE_MESSAGE
    assert json.loads(log.activity_json)['type'] == ['Create', 'Delete']


def test_an_ld_accepted_activity_with_an_array_type_is_refused():
    verified, reason = verify_ld_accepted_activity({'type': ['Delete'], 'actor': 'a', 'object': 'a'})
    assert verified is None and 'not a string' in reason


def test_an_ld_signed_array_self_delete_still_needs_the_origin_to_agree(app, db_session, monkeypatch):
    """`type: ['Delete']` once skipped verify_ld_accepted_activity's origin check and purged the account."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    victim = make_user(make_instance('peer.example'), 'victim')
    asked = []
    monkeypatch.setattr(activitypub_util, '_origin_refusal_of_delete',
                        lambda object_id, label: asked.append(object_id) or 'origin still serves it')
    activity = {'id': 'https://peer.example/a/d1', 'type': ['Delete'], 'actor': victim.ap_profile_id,
                'object': victim.ap_profile_id, 'removeData': True}

    activitypub_routes.process_delete_request(activity, True, ld_signed=True)

    assert asked == [victim.ap_profile_id]
    assert not db.session.get(type(victim), victim.id).deleted
    assert 'LD-signed activity dropped' in ActivityPubLog.query.one().exception_message


def test_an_array_self_delete_is_applied_when_it_passes(app, db_session):
    make_site()
    victim = make_user(make_instance('peer.example'), 'victim')

    activitypub_routes.process_delete_request({'id': 'https://peer.example/a/d1', 'type': ['Delete'],
                                               'actor': victim.ap_profile_id, 'object': victim.ap_profile_id}, False)

    db.session.expire_all()
    assert db.session.get(type(victim), victim.id).deleted


def test_a_self_delete_with_an_ambiguous_type_is_refused(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    make_site()
    victim = make_user(make_instance('peer.example'), 'victim')

    activitypub_routes.process_delete_request({'id': 'https://peer.example/a/d1', 'type': ['Delete', 'Create'],
                                               'actor': victim.ap_profile_id, 'object': victim.ap_profile_id}, True)

    assert not db.session.get(type(victim), victim.id).deleted
    assert ActivityPubLog.query.one().exception_message == NO_STRING_TYPE_MESSAGE


@pytest.mark.parametrize('kind', [['OrderedCollection'], ['Foo', 'Collection']])
def test_a_nested_collection_with_an_array_type_is_refused(app, db_session, monkeypatch, kind):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    announce = {'id': 'https://peer.example/a/1', 'type': 'Announce', 'actor': 'https://peer.example/c/news'}

    activitypub_routes.process_announced_objects(
        announce, [{'id': 'https://peer.example/c1', 'type': kind, 'orderedItems': ['https://peer.example/p/1']}],
        announce['id'], announce, True)

    assert ActivityPubLog.query.one().exception_message == 'Announced list contains a nested list or collection'


def test_a_refetched_object_with_an_array_type_is_normalised(monkeypatch):
    actor = 'https://peer.example/u/alice'
    fetched = {'id': 'https://peer.example/o/1', 'type': ['Note', 'as:Note'], 'attributedTo': actor}
    monkeypatch.setattr(activitypub_util, 'verify_object_from_source', lambda request: ({'object': fetched}, None))

    replaced, reason = activitypub_util._verify_announced_content(
        {'actor': actor, 'object': {'id': 'https://peer.example/o/1', 'type': 'Note'}}, 'peer.example')

    assert reason is None and replaced['object']['type'] == 'Note'
    assert fetched['type'] == ['Note', 'as:Note']


def test_a_refetched_object_with_an_ambiguous_type_is_refused(monkeypatch):
    actor = 'https://peer.example/u/alice'
    fetched = {'id': 'https://peer.example/o/1', 'type': ['Note', 'Page'], 'attributedTo': actor}
    monkeypatch.setattr(activitypub_util, 'verify_object_from_source', lambda request: ({'object': fetched}, None))

    replaced, reason = activitypub_util._verify_announced_content(
        {'actor': actor, 'object': {'id': 'https://peer.example/o/1', 'type': 'Note'}}, 'peer.example')

    assert replaced is None and 'no single usable type' in reason


def test_an_ld_signed_create_is_processed_and_forwarded_with_the_origins_copy(app, db_session, monkeypatch):
    """What reaches process_new_content, which forwards it, is the origin's content and a normalised type, never
    the content or the array that came with the LD signature."""
    author = _create_setup(app, monkeypatch)
    calls = record_moderation(monkeypatch, 'process_new_content')
    object_id = 'https://peer.example/post/1'
    fetched = {'id': object_id, 'type': ['Note', 'as:Note'], 'content': 'what the origin serves',
               'attributedTo': author.ap_profile_id}
    monkeypatch.setattr(activitypub_util, 'verify_object_from_source', lambda request: ({'object': fetched}, None))
    activity = create_activity(author, {'id': object_id, 'type': ['Note'], 'content': 'spoofed',
                                        'attributedTo': author.ap_profile_id})

    activitypub_routes.process_inbox_request(activity, True, ld_signed=True)

    args, _kwargs = calls['process_new_content'][0]
    assert args[3]['object']['content'] == 'what the origin serves'
    assert args[3]['object']['type'] == 'Note'


def test_an_object_fetched_by_uri_with_an_array_type_is_dispatched(app, db_session, monkeypatch):
    author = _create_setup(app, monkeypatch)
    calls = record_moderation(monkeypatch, 'process_new_content')
    object_id = 'https://peer.example/post/1'
    fetched = {'id': object_id, 'type': ['Note'], 'attributedTo': author.ap_profile_id}
    monkeypatch.setattr(activitypub_routes, 'verify_object_from_source',
                        lambda activity: (activity.update(object=fetched), None) and (activity, None))

    dispatch(create_activity(author, object_id))

    args, _kwargs = calls['process_new_content'][0]
    assert args[3]['object']['type'] == 'Note'


def test_an_object_fetched_by_uri_with_an_ambiguous_type_is_refused(app, db_session, monkeypatch):
    author = _create_setup(app, monkeypatch)
    fetched = {'id': 'https://peer.example/post/1', 'type': ['Note', 'Page']}
    monkeypatch.setattr(activitypub_routes, 'verify_object_from_source',
                        lambda activity: (activity.update(object=fetched), None) and (activity, None))

    dispatch(create_activity(author, 'https://peer.example/post/1'))

    assert ActivityPubLog.query.one().exception_message == NO_STRING_TYPE_MESSAGE
