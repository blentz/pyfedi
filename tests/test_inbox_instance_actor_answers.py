"""Interop D24 proactive sync: an Accept or Reject of the instance actor's Follow changes only the sync row."""
import uuid

import pytest
from flask import current_app

from app import db
from app.constants import APLOG_ACCEPT
from app.discovery import SYNC_ACCEPTED, SYNC_PENDING, SYNC_REJECTED
from app.discovery.instance_actor import follow_activity, instance_actor_answer
from app.models import ActivityPubLog, CommunityMember, DiscoverySync, utcnow
from tests.factories import inbox_activity, make_community, make_community_join_request, make_instance, make_user
from tests.test_inbox_dispatch_preamble import dispatch

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def founder(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)


@pytest.fixture
def pending(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    instance = make_instance('tube.example', software='peertube')
    community = make_community('zqchan', host='tube.example')
    community.instance_id = instance.id
    community.ap_fetched_at = utcnow()
    row = DiscoverySync(community_id=community.id, follow_target=community.ap_profile_id,
                        follow_uuid=str(uuid.uuid4()), follow_state=SYNC_PENDING)
    db.session.add(row)
    db.session.commit()
    return row, community


def answer(community, kind, obj):
    return inbox_activity(community, activity_type=kind, object=obj)


@pytest.mark.parametrize('kind, state', [('Accept', SYNC_ACCEPTED), ('Reject', SYNC_REJECTED)])
def test_an_answer_with_the_follow_object_sets_the_state_and_adds_no_member(pending, kind, state):
    row, community = pending
    members_before = community.subscriptions_count

    dispatch(answer(community, kind, follow_activity(row)))

    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == state
    assert CommunityMember.query.filter_by(community_id=community.id).count() == 0
    assert community.subscriptions_count == members_before
    assert ActivityPubLog.query.one().result == 'success'


@pytest.mark.parametrize('kind, state', [('Accept', SYNC_ACCEPTED), ('Reject', SYNC_REJECTED)])
def test_an_answer_with_the_follow_id_as_a_string_sets_the_state(pending, kind, state):
    row, community = pending

    dispatch(answer(community, kind, follow_activity(row)['id']))

    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == state


def test_an_answer_whose_follow_object_has_no_id_matches_by_target(pending):
    row, community = pending
    follow = follow_activity(row)
    del follow['id']

    dispatch(answer(community, 'Accept', follow))

    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == SYNC_ACCEPTED


@pytest.mark.parametrize('kind', ['Accept', 'Reject'])
def test_an_answer_signed_by_another_host_is_ignored(pending, kind):
    """Review Focus 1: anyone can name our uuid; only the followed host may answer."""
    row, community = pending
    other = make_instance('evil.example', software='peertube')
    impostor = make_community('impostor', host='evil.example')
    impostor.instance_id = other.id
    impostor.ap_fetched_at = utcnow()
    db.session.commit()

    dispatch(answer(impostor, kind, follow_activity(row)))

    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == SYNC_PENDING
    assert ActivityPubLog.query.one().result == 'ignored'


@pytest.mark.parametrize('kind', ['Accept', 'Reject'])
def test_an_answer_to_an_instance_actor_follow_we_do_not_hold_is_ignored(pending, kind):
    row, community = pending
    follow = follow_activity(row)
    follow['id'] = f"{current_app.config['SERVER_URL']}/activities/follow/{uuid.uuid4()}"

    dispatch(answer(community, kind, follow))

    assert ActivityPubLog.query.one().result == 'ignored'
    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == SYNC_PENDING


def test_a_users_join_request_string_accept_still_takes_the_old_path(pending):
    """Review Focus 2: same /activities/follow/ prefix, different table."""
    row, community = pending
    joiner = make_user(make_instance('people.example'), 'joiner')
    join_request = make_community_join_request(joiner, community)

    dispatch(answer(community, 'Accept', f"{current_app.config['SERVER_URL']}/activities/follow/{join_request.uuid}"))

    assert CommunityMember.query.filter_by(user_id=joiner.id, community_id=community.id).count() == 1
    db.session.expire_all()
    assert db.session.get(DiscoverySync, community.id).follow_state == SYNC_PENDING


@pytest.mark.parametrize('obj', [None, 7, {'type': 'Follow', 'actor': 'https://x.example/u/a'}, {'actor': None}])
def test_objects_that_are_not_ours_return_not_ours(pending, obj):
    assert instance_actor_answer({'object': obj}, 'https://tube.example/video-channels/zqchan') == (False, None)


def test_a_string_object_with_no_matching_row_is_not_ours(pending):
    assert instance_actor_answer({'object': 'https://x.example/activities/follow/nope'},
                                 'https://tube.example/x') == (False, None)


def test_a_follow_of_ours_with_no_usable_id_or_target_is_ours_but_has_no_row(pending):
    row, community = pending
    follow = {'type': 'Follow', 'actor': follow_activity(row)['actor'], 'object': {'id': community.ap_profile_id}}

    assert instance_actor_answer({'object': follow}, community.ap_profile_id) == (True, None)
