"""Relay forwards with no verifiable LD signature are fetched from the origin (spec 2026-10-13)."""
import uuid

import pytest

from app import db
from app.models import BannedInstances
from app.relays import RELAY_ACCEPTED
from app.relays import inbound
from tests.test_relays_inbound import NOTE_URI, make_relay, note

pytestmark = pytest.mark.usefixtures('site', 'redis_double')

AUTHOR = 'https://other.example/users/alice'


@pytest.fixture
def queued(app, monkeypatch):
    """Record what would be fetched; never run the task."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    calls = []
    monkeypatch.setattr(inbound, 'process_relayed_announce', lambda *args: calls.append(args))
    return calls


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(inbound, 'log_incoming_ap', lambda *args: lines.append(args))
    return lines


def forward(obj=None, activity_type='Create', actor=AUTHOR):
    return {'id': f'{AUTHOR}/activities/{uuid.uuid4().hex}', 'type': activity_type, 'actor': actor,
            'object': note() if obj is None else obj}


class TestAcceptUnsignedForward:

    def test_a_create_is_queued_for_fetch_from_its_origin(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)

        assert inbound.accept_unsigned_forward(relay, forward(), None) == ('', 200)

        assert queued == [(relay.id, NOTE_URI)]
        assert logged[-1][4] == 'relayed object queued for fetch from its origin'

    def test_a_bare_string_object_is_queued_without_the_pre_filter(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)

        assert inbound.accept_unsigned_forward(relay, forward(obj=NOTE_URI), None) == ('', 200)

        assert queued == [(relay.id, NOTE_URI)]

    @pytest.mark.parametrize('activity_type', ['Update', 'Delete', 'Like', 'Announce'])
    def test_any_other_type_is_ignored(self, app, queued, logged, activity_type):
        relay = make_relay(state=RELAY_ACCEPTED)

        assert inbound.accept_unsigned_forward(relay, forward(activity_type=activity_type), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == f'relayed {activity_type} without a verifiable signature'

    @pytest.mark.parametrize('obj', [{'type': 'Note'}, {'id': 7}, 42, ''])
    def test_an_object_without_an_id_is_ignored(self, app, queued, logged, obj):
        relay = make_relay(state=RELAY_ACCEPTED)

        assert inbound.accept_unsigned_forward(relay, forward(obj=obj), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == 'relayed object has no id'

    def test_the_pre_filter_refuses_a_reply_to_a_post_this_instance_lacks(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        obj = note(inReplyTo='https://other.example/notes/unknown')

        assert inbound.accept_unsigned_forward(relay, forward(obj=obj), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == 'relayed reply to a post this instance does not have'

    def test_an_object_on_another_host_than_the_actor_is_never_fetched(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        obj = note(uri='https://elsewhere.example/notes/1')

        assert inbound.accept_unsigned_forward(relay, forward(obj=obj), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == 'relayed object host does not match actor'

    def test_an_object_from_a_banned_instance_is_never_fetched(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        db.session.add(BannedInstances(domain='other.example'))
        db.session.commit()

        assert inbound.accept_unsigned_forward(relay, forward(), None) == ('', 200)

        assert queued == []
        assert logged[-1][4] == 'relayed object from a banned instance'

    def test_a_retried_forward_is_queued_once(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        activity = forward()

        inbound.accept_unsigned_forward(relay, activity, None)
        assert inbound.accept_unsigned_forward(relay, activity, None) == ('', 200)

        assert len(queued) == 1
        assert logged[-1][4] == 'Already aware of this activity'

    def test_a_queueing_failure_releases_the_claim_so_the_retry_is_processed(self, app, logged, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', True)
        relay = make_relay(state=RELAY_ACCEPTED)
        activity = forward()

        def boom(*args):
            raise RuntimeError('broker down')

        monkeypatch.setattr(inbound, 'process_relayed_announce', boom)
        with pytest.raises(RuntimeError):
            inbound.accept_unsigned_forward(relay, activity, None)
        done = []
        monkeypatch.setattr(inbound, 'process_relayed_announce', lambda *args: done.append(args))

        assert inbound.accept_unsigned_forward(relay, activity, None) == ('', 200)
        assert done == [(relay.id, NOTE_URI)]

    def test_outside_debug_the_task_goes_to_the_background_queue(self, app, logged, monkeypatch):
        monkeypatch.setitem(app.config, 'DEBUG', False)
        relay = make_relay(state=RELAY_ACCEPTED)
        sent = []
        monkeypatch.setattr(inbound.process_relayed_announce, 'apply_async',
                            lambda args, queue: sent.append((args, queue)))

        inbound.accept_unsigned_forward(relay, forward(), None)

        assert sent == [((relay.id, NOTE_URI), 'background')]


class TestTheClaimBelongsToTheRelay:

    def test_a_relay_forward_does_not_claim_the_authors_activity_id(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        activity = forward()

        inbound.accept_unsigned_forward(relay, activity, None)

        assert inbound.claim_activity(activity['id'], AUTHOR) is True

    @pytest.mark.parametrize('kwargs', [{'activity_type': 'Update'},
                                        {'obj': note(uri='https://elsewhere.example/notes/1')}])
    def test_a_refused_forward_holds_no_claim(self, app, queued, logged, kwargs):
        relay = make_relay(state=RELAY_ACCEPTED)
        activity = forward(**kwargs)

        inbound.accept_unsigned_forward(relay, activity, None)

        assert inbound.claim_activity(activity['id'], relay.actor_id) is True

    def test_a_failing_pre_filter_strands_no_claim(self, app, queued, logged, monkeypatch):
        relay = make_relay(state=RELAY_ACCEPTED)
        activity = forward()
        working = inbound.relayed_activity_allowed

        def boom(*args):
            raise RuntimeError('db down')

        monkeypatch.setattr(inbound, 'relayed_activity_allowed', boom)
        with pytest.raises(RuntimeError):
            inbound.accept_unsigned_forward(relay, activity, None)
        monkeypatch.setattr(inbound, 'relayed_activity_allowed', working)

        assert inbound.accept_unsigned_forward(relay, activity, None) == ('', 200)
        assert queued == [(relay.id, NOTE_URI)]


class TestHostGate:

    def test_the_host_comparison_ignores_case(self, app, queued, logged):
        relay = make_relay(state=RELAY_ACCEPTED)
        obj = note(uri='https://OTHER.example/notes/1')

        inbound.accept_unsigned_forward(relay, forward(obj=obj), None)

        assert queued == [(relay.id, 'https://OTHER.example/notes/1')]

    @pytest.mark.parametrize('uri, actor', [('https:///notes/1', AUTHOR),
                                            (NOTE_URI, {'id': AUTHOR})])
    def test_an_empty_host_or_a_non_string_actor_is_refused(self, app, queued, logged, uri, actor):
        relay = make_relay(state=RELAY_ACCEPTED)

        inbound.accept_unsigned_forward(relay, forward(obj=uri, actor=actor), None)

        assert queued == []
        assert logged[-1][4] == 'relayed object host does not match actor'
