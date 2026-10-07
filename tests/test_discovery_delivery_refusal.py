"""A Follow from the instance actor that a peer refuses outright marks its sync row, instead of leaving it
"Waiting for an answer" for a week (interop D24 proactive sync).

hell.cloud, 2026-10-07: of ~1,500 Follows, 96 got a 401 and 19 a 403 from peers that could not verify the
signature, and the status table showed them all as waiting. A 5xx is retried by the send queue and a transport
failure is transient, so neither is final."""
import uuid

import httpx
import pytest
from flask import current_app

from app import db
from app.discovery import SYNC_ACCEPTED, SYNC_FAILED, SYNC_PENDING
from app.discovery.instance_actor import follow_activity
from app.models import DiscoverySync
from tests.factories import make_community, make_instance, make_user
from tests.test_activitypub_signature import _Response, _deliver

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def row(db_session):
    make_user(make_instance('test.piefed.local', software='piefed'), 'founder', local=True)
    community = make_community('chan', host='tube.example')
    row = DiscoverySync(community_id=community.id, follow_target='https://tube.example/video-channels/chan',
                        follow_uuid=str(uuid.uuid4()), follow_state=SYNC_PENDING)
    db.session.add(row)
    db.session.commit()
    return row


def _state(row):
    db.session.expire_all()
    return db.session.get(DiscoverySync, row.community_id)


@pytest.mark.parametrize('status', [401, 403, 404, 406, 410])
def test_a_final_refusal_marks_the_row_failed_with_the_status(row, status):
    _deliver(_Response(status_code=status, text='{"detail":"nope"}'), body=follow_activity(row))

    fresh = _state(row)
    assert fresh.follow_state == SYNC_FAILED
    assert fresh.last_error == f'Follow refused: HTTP {status}'


@pytest.mark.parametrize('status', [500, 502, 429])
def test_a_retried_status_leaves_the_row_waiting(row, status):
    _deliver(_Response(status_code=status), body=follow_activity(row))
    assert _state(row).follow_state == SYNC_PENDING


def test_a_transport_failure_leaves_the_row_waiting(row):
    """post_request records a transport failure as status 404 internally; it is not a peer's refusal."""
    _deliver(raises=httpx.ConnectError('down'), body=follow_activity(row))
    assert _state(row).follow_state == SYNC_PENDING


def test_a_delivered_follow_leaves_the_row_waiting_for_its_accept(row):
    _deliver(_Response(status_code=202), body=follow_activity(row))
    assert _state(row).follow_state == SYNC_PENDING


def test_an_accept_that_already_arrived_is_not_overwritten(row):
    row.follow_state = SYNC_ACCEPTED
    db.session.commit()
    _deliver(_Response(status_code=403), body=follow_activity(row))
    assert _state(row).follow_state == SYNC_ACCEPTED


def test_a_users_refused_follow_touches_no_sync_row(row):
    body = dict(follow_activity(row), actor='https://test.piefed.local/u/someone')
    _deliver(_Response(status_code=403), body=body)
    assert _state(row).follow_state == SYNC_PENDING


@pytest.mark.parametrize('body', [
    {'type': 'Create', 'id': 'https://test.piefed.local/activities/create/1'},
    {'type': 'Follow', 'id': 'https://test.piefed.local/activities/like/1'},
])
def test_other_refused_activities_touch_no_sync_row(row, body):
    body = dict(body, actor=f"{current_app.config['SERVER_URL']}/actor")
    _deliver(_Response(status_code=403), body=body)
    assert _state(row).follow_state == SYNC_PENDING


def test_a_refused_follow_with_no_matching_row_is_harmless(row):
    body = dict(follow_activity(row), id=f"{current_app.config['SERVER_URL']}/activities/follow/{uuid.uuid4()}")
    _deliver(_Response(status_code=403), body=body)
    assert _state(row).follow_state == SYNC_PENDING
