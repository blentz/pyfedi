"""Phase S4 of the security track: an activity a Group Announces from another host is not trusted on the Group's
signature alone. Spec: docs/superpowers/specs/2026-10-10-ap-security-track-design.md, "Phase S4".

verify_announced_inner is tested directly; the dispatcher tests prove the hook sits before the inner actor is
looked up, and that an Announced list gets the same check for every element.
"""
import uuid

import pytest

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


@pytest.mark.parametrize('actor', ['MISSING', 42, {'type': 'Person'}, 'not a url'])
def test_a_cross_origin_inner_activity_with_no_usable_actor_is_dropped(actor):
    inner = _like('https://other.example/activities/like/1', actor)
    if actor == 'MISSING':
        del inner['actor']

    assert verify_announced_inner(_announce(inner)) == (None, 'Announced activity host does not match its actor')


def test_an_inner_activity_without_an_id_is_left_to_the_dispatchers_own_refusal():
    """D127 (R:1036) refuses an id-less inner activity and logs it; this check must not change that message."""
    inner = {'type': 'Like', 'actor': 'https://third.example/u/mallory'}

    assert verify_announced_inner(_announce(inner)) == (inner, None)


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
