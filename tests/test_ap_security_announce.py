"""Phase S4 of the security track: an activity a Group Announces from another host is not trusted on the Group's
signature alone. Spec: docs/superpowers/specs/2026-10-10-ap-security-track-design.md, "Phase S4".

verify_announced_inner is tested directly; the dispatcher tests prove the hook sits before the inner actor is
looked up, and that an Announced list gets the same check for every element.
"""
import copy
import uuid

import httpx
import pytest
from sqlalchemy import inspect as sa_inspect

import app.activitypub.routes as activitypub_routes
from app import db
from app.activitypub.signature import LDSignature, default_context
from app.activitypub.util import ANNOUNCED_DELETE_DEFERRED, cross_origin_delete_delay, verify_announced_inner
from app.models import ActivityPubLog, InstanceRole, Post, User, utcnow
from app.utils import set_setting
from tests.factories import AS_PUBLIC_URI, a_keypair, make_community_member, make_instance, make_post, \
    make_post_reply, make_user, note_document, serve_remote_object
from tests.test_inbox_dispatch_announce import _seed_announcing_community
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch

GROUP = 'https://peer.example/c/microblogs'


def _announce(inner, group=GROUP):
    return {'id': f'{group}/activities/announce/{uuid.uuid4().hex}', 'type': 'Announce', 'actor': group,
            'object': inner}


def _like(activity_id, actor, object_uri='https://peer.example/post/1'):
    return {'id': activity_id, 'type': 'Like', 'actor': actor, 'object': object_uri}


def _messages(activity):
    return [log.exception_message for log in ActivityPubLog.query.filter_by(activity_id=activity['id']).all()]


def test_a_same_origin_inner_activity_is_trusted_on_the_groups_signature():
    inner = _like('https://peer.example/activities/like/1', 'https://elsewhere.example/u/bob')

    assert verify_announced_inner(_announce(inner)) == (inner, None)


@pytest.mark.parametrize('group, inner_id', [
    ('https://Peer.Example:443/c/microblogs', 'https://peer.example/activities/like/1'),
    ('https://peer.example/c/microblogs', 'https://PEER.example:443/activities/like/1'),
])
def test_hosts_differing_only_by_case_or_port_are_the_same_origin(group, inner_id):
    """Review Focus 3."""
    inner = _like(inner_id, 'https://elsewhere.example/u/bob')

    assert verify_announced_inner(_announce(inner, group=group)) == (inner, None)


def test_a_cross_origin_inner_activity_whose_actor_is_on_another_host_is_dropped():
    inner = _like('https://other.example/activities/like/1', 'https://third.example/u/mallory')

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced activity host does not match its actor')


@pytest.mark.parametrize('actor', ['https://Other.Example:443/u/bob',
                                   {'id': 'https://other.example/u/bob', 'type': 'Person'}])
def test_a_cross_origin_vote_with_a_consistent_actor_is_trusted(actor):
    """The accepted residual: votes and other small activities get host consistency only."""
    inner = _like('https://other.example/activities/like/1', actor)

    assert verify_announced_inner(_announce(inner)) == (inner, None)


@pytest.mark.parametrize('actor', [42, {'type': 'Person'}, 'not a url'])
def test_a_cross_origin_inner_activity_with_no_usable_actor_is_dropped(actor):
    inner = _like('https://other.example/activities/like/1', actor)

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced activity host does not match its actor')


def test_a_cross_origin_inner_object_with_no_actor_is_dropped_for_that_reason():
    """A bare object naming only attributedTo has no actor to be consistent with; say so, not 'does not match'."""
    inner = {'id': 'https://other.example/post/1', 'type': 'Page', 'attributedTo': 'https://other.example/u/bob'}

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced activity from another host has no actor')


def test_a_same_origin_inner_object_with_no_actor_is_left_to_the_dispatcher():
    inner = {'id': 'https://peer.example/post/1', 'type': 'Page', 'attributedTo': 'https://peer.example/u/bob'}

    assert verify_announced_inner(_announce(inner)) == (inner, None)


UNUSABLE_IDS = [None, 42, {}, '']


def test_an_inner_activity_without_an_id_is_dropped_with_the_d127_message():
    """R-INNERID: refused here, before the actor lookup, with the text D127 (R:1075) logged after it."""
    inner = {'type': 'Like', 'actor': 'https://third.example/u/mallory', 'object': 'https://peer.example/post/1'}

    assert verify_announced_inner(_announce(inner)) == (None, 'Announce object has no id or type')


@pytest.mark.parametrize('inner_id', UNUSABLE_IDS)
def test_an_inner_activity_whose_id_is_not_a_non_empty_string_is_dropped(inner_id):
    """R-INNERID: D127 and object_has_missing_fields test only that the key is present, so {'id': None} got past both."""
    inner = _like(inner_id, 'https://third.example/u/mallory')

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced activity id is not a non-empty string')


def test_an_inner_activity_whose_id_has_no_host_is_dropped():
    inner = _like('urn:uuid:1234', 'https://other.example/u/bob')

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced activity id has no host')


def test_the_dispatcher_drops_and_logs_an_inconsistent_announced_activity(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    activity = _announce(_like('https://other.example/activities/like/1', 'https://third.example/u/mallory'),
                         group=community.ap_profile_id)

    dispatch(activity)

    assert _messages(activity) == ['Announced activity host does not match its actor']
    # The check runs before the inner actor is looked up: the spoofed actor was neither fetched
    # (block_outbound_http would have raised) nor stored.
    assert User.query.filter_by(ap_profile_id='https://third.example/u/mallory').first() is None


def test_every_element_of_an_announced_list_gets_the_same_check(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    activity = _announce([_like('https://other.example/activities/like/1', 'https://third.example/u/mallory'),
                          _like('https://other.example/activities/like/2', 'https://fourth.example/u/trent')],
                         group=community.ap_profile_id)

    dispatch(activity)

    assert _messages(activity) == ['Announced activity host does not match its actor'] * 2


def _record_actor_lookups(monkeypatch):
    looked_up = []
    real = activitypub_routes.find_actor_or_create_cached

    def recording(actor, *args, **kwargs):
        looked_up.append(actor)
        return real(actor, *args, **kwargs)

    monkeypatch.setattr(activitypub_routes, 'find_actor_or_create_cached', recording)
    return looked_up


MALLORY = 'https://third.example/u/mallory'


@pytest.mark.parametrize('inner_id', ['MISSING'] + UNUSABLE_IDS)
def test_the_dispatcher_drops_an_inner_activity_without_a_usable_id_before_looking_up_its_actor(
        app, db_session, monkeypatch, inner_id):
    """R-INNERID: the forged vote {'id': None, 'actor': mallory} reached process_upvote before this check."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    inner = _like(inner_id, MALLORY)
    if inner_id == 'MISSING':
        del inner['id']
    activity = _announce(inner, group=community.ap_profile_id)
    looked_up = _record_actor_lookups(monkeypatch)

    dispatch(activity)

    expected = 'Announce object has no id or type' if inner_id == 'MISSING' \
        else 'Announced activity id is not a non-empty string'
    assert _messages(activity) == [expected]
    assert MALLORY not in looked_up
    assert User.query.filter_by(ap_profile_id=MALLORY).first() is None


def test_the_dispatcher_drops_an_announced_list_element_without_a_usable_id_before_looking_up_its_actor(
        app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    no_id = _like('unused', MALLORY)
    del no_id['id']
    activity = _announce([_like(None, MALLORY), _like(42, MALLORY), _like({}, MALLORY), no_id],
                         group=community.ap_profile_id)
    looked_up = _record_actor_lookups(monkeypatch)

    dispatch(activity)

    assert sorted(_messages(activity)) == sorted(['Announced activity id is not a non-empty string'] * 3
                                                 + ['Announce object has no id or type'])
    assert MALLORY not in looked_up
    assert User.query.filter_by(ap_profile_id=MALLORY).first() is None


CAROL = 'https://other.example/users/carol'
OBJECT_ID = 'https://other.example/objects/1'


def _carol():
    """A remote author stored here with a real keypair; ap_fetched_at stops an inline profile refresh."""
    author = make_user(make_instance('other.example'), 'carol', with_keys=True)
    author.ap_fetched_at = utcnow()
    db.session.commit()
    assert author.ap_profile_id == CAROL
    return author


def _create(actor=CAROL, obj=None, activity_type='Create'):
    return {'@context': default_context(), 'id': f'https://other.example/activities/{activity_type.lower()}/1',
            'type': activity_type, 'actor': actor,
            'object': obj if obj is not None else note_document(attributed_to=actor, uri=OBJECT_ID)}


def _ld_sign(inner, private_key, actor=CAROL):
    inner['signature'] = LDSignature.create_signature(inner, private_key, f'{actor}#main-key')
    return inner


def _origin_copy(http_mock, attributed_to=CAROL, uri=OBJECT_ID, fields=None):
    return serve_remote_object(http_mock, uri=OBJECT_ID, document=note_document(
        attributed_to=attributed_to, uri=uri, fields={'content': 'what the origin says', **(fields or {})}))


@pytest.mark.parametrize('activity_type', ['Create', 'Update'])
def test_a_valid_inner_ld_signature_is_no_shortcut_the_origin_copy_is_used(app, db_session, http_mock,
                                                                            no_network_ld_signing, activity_type):
    """Ruling R-NOLDSHORTCUT: an LD signature binds the normalized graph, not the JSON pyfedi reads, so even a
    valid one by the stored actor is not trusted -- the object is always replaced by the copy its origin serves."""
    carol = _carol()
    inner = _ld_sign(_create(activity_type=activity_type), carol.private_key)
    served = _origin_copy(http_mock)

    result, reason = verify_announced_inner(_announce(inner))

    assert reason is None
    assert result['object'] == served
    assert result['id'] == inner['id']
    assert result['type'] == activity_type


def test_a_bad_inner_ld_signature_is_refetched_like_any_other(app, db_session, http_mock, no_network_ld_signing):
    _carol()
    wrong_key, _public = a_keypair()
    inner = _ld_sign(_create(), wrong_key)
    served = _origin_copy(http_mock)

    result, reason = verify_announced_inner(_announce(inner))

    assert reason is None
    assert result['object'] == served


def test_a_signature_by_an_actor_not_stored_here_fetches_no_actor(app, db_session, http_mock, no_network_ld_signing):
    """http_mock serves only the object: a fetch of the stranger's actor document would be an unmatched request."""
    stranger = 'https://other.example/users/nobody'
    key, _public = a_keypair()
    inner = _ld_sign(_create(actor=stranger), key, actor=stranger)
    served = _origin_copy(http_mock, attributed_to=stranger)

    result, reason = verify_announced_inner(_announce(inner))

    assert reason is None
    assert result['object'] == served
    assert User.query.filter_by(ap_profile_id=stranger).first() is None


@pytest.mark.parametrize('signature', ['garbage', 42, None, {}, [],
                                       {'type': 'RsaSignature2017', 'creator': f'{CAROL}#main-key',
                                        'created': '2026-10-10T00:00:00Z', 'signatureValue': '!!not base64!!'}])
def test_an_attacker_shaped_signature_field_is_ignored_and_the_object_refetched(app, db_session, http_mock,
                                                                                signature):
    _carol()
    inner = {**_create(), 'signature': signature}
    served = _origin_copy(http_mock)

    result, reason = verify_announced_inner(_announce(inner))

    assert reason is None
    assert result['object'] == served


def _tamper_with_inline_context_remap(inner):
    """Keeps the signed graph: 'content' becomes an ignored blank-node term carrying the forgery, and
    'realcontent' carries the signed text under as:content."""
    note = inner['object']
    note['@context'] = {'content': '_:ignored', 'realcontent': 'https://www.w3.org/ns/activitystreams#content'}
    note['realcontent'] = note['content']
    note['content'] = 'forged by the announcing group'
    return inner


def _tamper_with_an_unmapped_term(inner):
    """A term no context maps is dropped from the graph, so adding it leaves the signature valid."""
    inner['object']['piefedForgedFlag'] = True
    return inner


@pytest.mark.parametrize('tamper', [_tamper_with_inline_context_remap, _tamper_with_an_unmapped_term])
def test_a_tampered_ld_signed_create_still_verifies_but_the_origin_copy_is_used(app, db_session, http_mock,
                                                                                no_network_ld_signing, tamper):
    """Finding 1: the tampering keeps the LD signature valid, which is why it is no shortcut."""
    carol = _carol()
    inner = tamper(_ld_sign(_create(), carol.private_key))
    LDSignature.verify_signature(copy.deepcopy(inner), carol.public_key)    # the forgery passes LD verification
    served = _origin_copy(http_mock)

    result, reason = verify_announced_inner(_announce(inner))

    assert reason is None
    assert result['object'] == served


def test_the_dispatcher_stores_the_origin_content_not_an_ld_signed_forgery(app, db_session, monkeypatch, http_mock,
                                                                         no_network_ld_signing):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    carol = _carol()
    addressing = {'to': [AS_PUBLIC_URI], 'audience': community.ap_profile_id}
    inner = _ld_sign(_create(obj=note_document(attributed_to=CAROL, uri=OBJECT_ID, fields=addressing)),
                     carol.private_key)
    inner = _tamper_with_inline_context_remap(inner)
    _origin_copy(http_mock, fields=addressing)

    dispatch(_announce(inner, group=community.ap_profile_id))

    post = Post.query.filter_by(ap_id=OBJECT_ID).one()
    assert 'what the origin says' in post.body_html
    assert 'forged' not in post.body_html
    assert post.user_id == carol.id


@pytest.mark.parametrize('attributed_to', [{'id': CAROL, 'type': 'Person'},
                                           ['https://other.example/users/dave', {'id': CAROL, 'type': 'Person'}],
                                           ['https://other.example/users/dave', CAROL]])
def test_an_origin_copy_attributed_to_the_activitys_actor_in_any_form_is_accepted(app, db_session, http_mock,
                                                                                  attributed_to):
    served = _origin_copy(http_mock, attributed_to=attributed_to)

    result, reason = verify_announced_inner(_announce(_create()))

    assert reason is None
    assert result['object'] == served


@pytest.mark.parametrize('attributed_to', ['https://other.example/users/dave',
                                           {'id': 'https://other.example/users/dave', 'type': 'Person'},
                                           ['https://other.example/users/dave', {'type': 'Person'}, 42]])
def test_an_origin_copy_by_another_author_on_the_same_host_is_dropped(app, db_session, http_mock, attributed_to):
    """Finding 3: the origin vouches for the host, not for the author the announcing Group names."""
    _origin_copy(http_mock, attributed_to=attributed_to)

    assert verify_announced_inner(_announce(_create())) == (
        None, 'the fetched object is not attributed to the Announced activity actor')


@pytest.mark.parametrize('served_id', ['https://elsewhere.example/objects/1', 'https://other.example/objects/2'])
def test_an_origin_copy_with_a_different_id_is_dropped(app, db_session, http_mock, served_id):
    """Finding 4: the id the origin serves must be the one asked for, or the host invariant is bypassed."""
    _origin_copy(http_mock, uri=served_id)

    assert verify_announced_inner(_announce(_create())) == (
        None, 'the fetched object id does not match the Announced object id')


def test_an_unsigned_inner_activity_naming_its_object_by_uri_is_checked_against_the_origin(app, db_session, http_mock):
    served = serve_remote_object(http_mock, uri=OBJECT_ID, document=note_document(attributed_to=CAROL, uri=OBJECT_ID))

    result, reason = verify_announced_inner(_announce(_create(obj=OBJECT_ID)))

    assert reason is None
    assert result['object'] == served


def test_an_object_on_another_host_than_its_activity_is_dropped(app, db_session):
    inner = _create(obj=note_document(attributed_to=CAROL, uri='https://elsewhere.example/objects/1'))

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced object is on a different host than its activity')


@pytest.mark.parametrize('obj', [{'type': 'Note', 'content': 'no id'}, 42])
def test_an_inner_activity_whose_object_has_no_id_is_dropped(app, db_session, obj):
    assert verify_announced_inner(_announce(_create(obj=obj))) == (None, 'Announced object has no id')


def test_an_origin_copy_attributed_to_another_host_is_dropped(app, db_session, http_mock):
    serve_remote_object(http_mock, uri=OBJECT_ID,
                        document=note_document(attributed_to='https://elsewhere.example/u/x', uri=OBJECT_ID))

    assert verify_announced_inner(_announce(_create())) == (
        None, 'the fetched object is attributed to a different host than its URI')


def test_an_unreachable_origin_drops_the_activity(app, db_session, http_mock):
    http_mock.get(OBJECT_ID).mock(side_effect=httpx.ConnectError('down'))

    assert verify_announced_inner(_announce(_create())) == (None, 'the object could not be fetched')


def test_the_dispatcher_drops_and_logs_a_cross_origin_create_its_origin_disowns(app, db_session, monkeypatch, http_mock):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    serve_remote_object(http_mock, uri=OBJECT_ID, document={}, status=404)
    activity = _announce(_create(), group=community.ap_profile_id)

    dispatch(activity)

    assert _messages(activity) == ['the object fetch returned HTTP 404']
    assert Post.query.count() == 0


def test_a_refetched_object_naming_its_author_only_by_attributed_to_keeps_the_activitys_actor(
        app, db_session, monkeypatch, http_mock):
    """Controller carry-over: D52's 'actor' check runs before the S4 hook, so the replacement the hook hands back
    must still carry the inner activity's actor -- the dispatcher reads request_json['object']['actor'] next. The
    origin's document has no 'actor', only 'attributedTo'; the activity stays the Create and keeps its actor, and
    the post is stored from the origin's copy."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    _carol()
    served = serve_remote_object(http_mock, uri=OBJECT_ID, document=note_document(
        attributed_to=CAROL, uri=OBJECT_ID,
        fields={'content': 'what the origin says', 'to': [AS_PUBLIC_URI], 'audience': community.ap_profile_id}))
    assert 'actor' not in served
    inner = _create(obj=note_document(attributed_to=CAROL, uri=OBJECT_ID, fields={'content': 'forged'}))

    result, reason = verify_announced_inner(_announce(inner))
    assert reason is None
    assert result['actor'] == CAROL
    assert result['type'] == 'Create'
    assert result['object'] == served

    activity = _announce(inner, group=community.ap_profile_id)
    dispatch(activity)

    post = Post.query.filter_by(ap_id=OBJECT_ID).one()
    assert 'what the origin says' in post.body_html


DELETED_ID = 'https://other.example/objects/1'
MOD_DELETE_ID = 'https://mods.example/activities/delete/1'


def _delete(obj=DELETED_ID):
    return {'id': 'https://other.example/activities/delete/1', 'type': 'Delete', 'actor': CAROL, 'object': obj}


def _mod_delete(actor, obj=DELETED_ID):
    return {'id': MOD_DELETE_ID, 'type': 'Delete', 'actor': actor, 'object': obj, 'summary': 'rule violation'}


def _remote(domain, name):
    """A remote user stored here; ap_fetched_at stops an inline profile refresh."""
    user = make_user(make_instance(domain), name)
    user.ap_fetched_at = utcnow()
    db.session.commit()
    return user


def _moderated_community(role='moderator'):
    """The announcing community (peer.example) with dave, on a third host, as a moderator or owner."""
    _instance, community = _seed_announcing_community()
    dave = _remote('mods.example', 'dave')
    member = make_community_member(dave, community, is_moderator=(role == 'moderator'))
    member.is_owner = role == 'owner'
    db.session.commit()
    return community, dave


# --- everyone who is not a moderator: the origin must no longer serve the object ---

@pytest.mark.parametrize('status', [404, 410])
def test_an_announced_delete_is_processed_once_the_origin_no_longer_serves_the_object(app, db_session, http_mock,
                                                                                     status):
    http_mock.get(DELETED_ID).respond(status)
    inner = _delete()

    assert verify_announced_inner(_announce(inner)) == (inner, None)


def test_an_announced_delete_may_name_its_object_by_tombstone(app, db_session, http_mock):
    http_mock.get(DELETED_ID).respond(410)
    inner = _delete(obj={'id': DELETED_ID, 'type': 'Tombstone'})

    assert verify_announced_inner(_announce(inner)) == (inner, None)


def test_an_announced_delete_of_an_object_the_origin_still_serves_is_dropped(app, db_session, http_mock):
    http_mock.get(DELETED_ID).respond(200, json=note_document(attributed_to=CAROL, uri=DELETED_ID))

    assert verify_announced_inner(_announce(_delete())) == (None, 'Announced Delete of an object that still exists')


@pytest.mark.parametrize('status', [401, 403, 500])
def test_any_other_origin_answer_drops_the_delete(app, db_session, http_mock, status):
    http_mock.get(DELETED_ID).respond(status)

    assert verify_announced_inner(_announce(_delete())) == (None, f'Announced Delete: the object answers HTTP {status}')


def test_an_unreachable_origin_drops_the_delete_without_a_retry(app, db_session, http_mock):
    """D775: no retry on inbox processing."""
    route = http_mock.get(DELETED_ID).mock(side_effect=httpx.ConnectError('down'))

    assert verify_announced_inner(_announce(_delete())) == (None, 'Announced Delete: the object could not be fetched')
    assert route.call_count == 1


@pytest.mark.parametrize('obj', ['MISSING', {'type': 'Tombstone'}, 42])
def test_an_announced_delete_naming_no_object_is_dropped(app, db_session, obj):
    inner = _delete()
    if obj == 'MISSING':
        del inner['object']
    else:
        inner['object'] = obj

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced Delete names no object')


def test_a_same_origin_delete_is_not_fetched(app, db_session, http_mock):
    """http_mock has no routes: a fetch would be an unmatched request."""
    inner = _delete()

    assert verify_announced_inner(_announce(inner, group='https://other.example/c/news')) == (inner, None)


# --- moderators and Group-host admins: deferred, never fetched ---

@pytest.mark.parametrize('role', ['moderator', 'owner'])
def test_a_moderators_cross_origin_delete_is_deferred_not_fetched(app, db_session, http_mock, role):
    """http_mock has no routes: a fetch would be an unmatched request."""
    community, dave = _moderated_community(role)
    announce = _announce(_mod_delete(dave.ap_profile_id), group=community.ap_profile_id)

    assert verify_announced_inner(announce, community) == (None, ANNOUNCED_DELETE_DEFERRED)


def test_an_admin_of_the_communitys_host_is_deferred(app, db_session, http_mock):
    """What counts is an InstanceRole admin row for the community's own instance (Community.is_instance_admin)."""
    _instance, community = _seed_announcing_community()
    erin = _remote('mods.example', 'erin')
    db.session.add(InstanceRole(instance_id=community.instance_id, user_id=erin.id, role='admin'))
    db.session.commit()
    announce = _announce(_mod_delete(erin.ap_profile_id), group=community.ap_profile_id)

    assert verify_announced_inner(announce, community) == (None, ANNOUNCED_DELETE_DEFERRED)


def test_an_admin_of_an_unrelated_instance_takes_the_refetch_path(app, db_session, http_mock):
    _instance, community = _seed_announcing_community()
    erin = _remote('mods.example', 'erin')
    db.session.add(InstanceRole(instance_id=erin.instance_id, user_id=erin.id, role='admin'))
    db.session.commit()
    http_mock.get(DELETED_ID).respond(200, json=note_document(attributed_to=CAROL, uri=DELETED_ID))
    announce = _announce(_mod_delete(erin.ap_profile_id), group=community.ap_profile_id)

    assert verify_announced_inner(announce, community) == (None, 'Announced Delete of an object that still exists')


def test_a_moderator_announced_by_a_feed_takes_the_refetch_path(app, db_session, http_mock):
    """No announcing community (a feed, or a podcast twin): nobody's moderator status can apply."""
    _community, dave = _moderated_community()
    http_mock.get(DELETED_ID).respond(410)
    inner = _mod_delete(dave.ap_profile_id)

    assert verify_announced_inner(_announce(inner)) == (inner, None)


def test_an_actor_not_stored_here_is_never_fetched_and_takes_the_refetch_path(app, db_session, http_mock):
    """Only the object is fetched: an unknown actor is looked up, never created."""
    _instance, community = _seed_announcing_community()
    http_mock.get(DELETED_ID).respond(410)
    inner = _mod_delete('https://mods.example/users/nobody')

    assert verify_announced_inner(_announce(inner, group=community.ap_profile_id), community) == (inner, None)
    assert User.query.filter_by(ap_profile_id='https://mods.example/users/nobody').first() is None


def test_a_moderator_delete_naming_no_object_is_dropped_before_any_role_check(app, db_session):
    community, dave = _moderated_community()
    inner = _mod_delete(dave.ap_profile_id, obj=42)

    assert verify_announced_inner(_announce(inner, group=community.ap_profile_id), community) == (
        None, 'Announced Delete names no object')


# --- the dispatcher ---

def test_the_dispatcher_drops_and_logs_a_delete_of_an_object_still_served(app, db_session, monkeypatch, http_mock):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _instance, community = _seed_announcing_community()
    http_mock.get(DELETED_ID).respond(200, json=note_document(attributed_to=CAROL, uri=DELETED_ID))
    activity = _announce(_delete(), group=community.ap_profile_id)

    dispatch(activity)

    assert _messages(activity) == ['Announced Delete of an object that still exists']


def test_the_dispatcher_schedules_a_moderators_delete_with_the_configured_delay(app, db_session, monkeypatch,
                                                                               http_mock):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, dave = _moderated_community()
    post = make_post(community, _carol(), ap_id=DELETED_ID)
    set_setting('cross_origin_delete_delay', 45)
    scheduled = []
    monkeypatch.setattr(activitypub_routes.apply_delayed_delete, 'apply_async',
                        lambda args=None, kwargs=None, countdown=None, **options: scheduled.append((args, countdown)))
    activity = _announce(_mod_delete(dave.ap_profile_id), group=community.ap_profile_id)

    dispatch(activity)

    assert scheduled == [((activity, True), 45)]
    assert _messages(activity) == [
        'Announced Delete by a moderator: applied after 45 seconds unless its home server deletes it first']
    db.session.refresh(post)
    assert post.deleted is False    # nothing is applied before the delay


# --- apply_delayed_delete ---

def test_a_deferred_delete_of_a_live_post_is_applied_as_a_moderator_removal(app, db_session, monkeypatch):
    community, dave = _moderated_community()
    post = make_post(community, _carol(), ap_id=DELETED_ID)
    calls = record_moderation(monkeypatch, 'delete_post_or_comment', 'announce_activity_to_followers')
    announce = _announce(_mod_delete(dave.ap_profile_id), group=community.ap_profile_id)

    activitypub_routes.apply_delayed_delete(announce, True)

    assert len(calls['delete_post_or_comment']) == 1
    user_arg, to_delete_arg, store_ap_json_arg, _request_json, reason_arg = calls['delete_post_or_comment'][0][0]
    assert sa_inspect(user_arg).identity[0] == dave.id
    assert sa_inspect(to_delete_arg).identity[0] == post.id
    assert store_ap_json_arg is True
    assert reason_arg == 'rule violation'
    assert calls['announce_activity_to_followers'] == []    # an Announced Delete is never re-announced


@pytest.mark.parametrize('by_tombstone', [False, True])
def test_a_deferred_delete_of_a_live_reply_goes_back_through_the_dispatcher(app, db_session, monkeypatch,
                                                                            by_tombstone):
    community, dave = _moderated_community()
    post = make_post(community, _carol(), ap_id=DELETED_ID)
    reply = make_post_reply(post, post.author)
    reply.ap_id = 'https://other.example/comment/1'
    db.session.commit()
    processed = []
    monkeypatch.setattr(activitypub_routes, 'process_inbox_request', lambda *a, **k: processed.append((a, k)))
    obj = {'id': reply.ap_id, 'type': 'Tombstone'} if by_tombstone else reply.ap_id
    announce = _announce(_mod_delete(dave.ap_profile_id, obj=obj), group=community.ap_profile_id)

    activitypub_routes.apply_delayed_delete(announce, False)

    assert processed == [((announce, False), {'announce_verified': True})]


@pytest.mark.parametrize('state', ['already deleted', 'never stored'])
def test_a_deferred_delete_the_home_server_beat_is_superseded(app, db_session, monkeypatch, state):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, dave = _moderated_community()
    if state == 'already deleted':
        post = make_post(community, _carol(), ap_id=DELETED_ID)
        post.deleted = True
        db.session.commit()
    processed = []
    monkeypatch.setattr(activitypub_routes, 'process_inbox_request', lambda *a, **k: processed.append((a, k)))
    announce = _announce(_mod_delete(dave.ap_profile_id), group=community.ap_profile_id)

    activitypub_routes.apply_delayed_delete(announce, True)

    assert processed == []
    assert _messages(announce) == ["Delete superseded by the home server's"]


# --- the delay setting ---

def test_the_delay_defaults_to_90_seconds(app, db_session):
    assert cross_origin_delete_delay() == 90


@pytest.mark.parametrize('stored, expected', [
    (0, 0), (45, 45), (3600, 3600), ('120', 120),
    ('soon', 90), (None, 90), (-5, 90), (3601, 90),
])
def test_the_delay_setting_is_coerced_and_bounded(app, db_session, stored, expected):
    set_setting('cross_origin_delete_delay', stored)

    assert cross_origin_delete_delay() == expected
