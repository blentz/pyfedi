"""Phase S4 of the security track: an activity a Group Announces from another host is not trusted on the Group's
signature alone. Spec: docs/superpowers/specs/2026-10-10-ap-security-track-design.md, "Phase S4".

verify_announced_inner is tested directly; the dispatcher tests prove the hook sits before the inner actor is
looked up, and that an Announced list gets the same check for every element.
"""
import uuid

import pytest

import app.activitypub.routes as activitypub_routes
from app.activitypub.util import verify_announced_inner
from app.models import ActivityPubLog, User
from tests.test_inbox_dispatch_announce import _seed_announcing_community
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
