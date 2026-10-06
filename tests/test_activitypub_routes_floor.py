"""app/activitypub/routes.py: the shared inbox gate and replay path, the arms
the per-arm inbox suites never reached (coverage-floor work).

Every test here is about a gate decision that is easy to get wrong silently:
which id is used to suppress duplicates, whether an allowed host is let
through a strong allowlist, and whether an actor with no Instance row crashes
the bookkeeping.
"""
import pytest

from app import db
from app.constants import ALLOWLIST_STRONG
from app.activitypub.routes import replay_inbox_request
from app.models import ActivityPubLog, AllowedInstances, Site
from tests.factories import inbox_activity, signed_inbox_post
from tests.test_inbox_gate_dispatch import _patch_dispatch_recorders

pytestmark = pytest.mark.usefixtures('redis_double')


def _announce_of(signing_peer, inner_id):
    """An Announce whose inner object is complete (id/type/actor/object) and
    comes from a REMOTE actor, so it is not 'already present' local content."""
    inner = {'id': inner_id, 'type': 'Note', 'actor': 'https://other.example/users/bob',
             'object': inner_id}
    return inbox_activity(signing_peer, activity_type='Announce', object=inner)


def test_announces_of_the_same_remote_object_are_deduplicated_by_the_inner_id(
        app, signing_peer, monkeypatch):
    """shared_inbox swaps `id` for the announced object's own id before the
    duplicate check, because several relays Announce one object under many
    outer ids. Two Announces with DIFFERENT outer ids but the same inner object
    must therefore dispatch once; the second is logged as already known.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    inbox_recorder, _ = _patch_dispatch_recorders(monkeypatch)
    inner_id = 'https://other.example/notes/42'

    with app.test_client() as client:
        first = signed_inbox_post(client, _announce_of(signing_peer, inner_id), signing_peer)
        second = signed_inbox_post(client, _announce_of(signing_peer, inner_id), signing_peer)

    assert first.status_code == 200 and second.status_code == 200
    assert len(inbox_recorder.inline) == 1
    assert [row.exception_message for row in ActivityPubLog.query.all()] == \
        ['Already aware of this activity']


def test_an_allowlisted_actor_passes_a_strong_allowlist(app, signing_peer, monkeypatch):
    """Under a strong allowlist the 403 is only for hosts with no
    AllowedInstances row; an actor whose host IS listed must go on to dispatch,
    or turning the allowlist on would lock out every peer including the ones
    the admin approved."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    inbox_recorder, _ = _patch_dispatch_recorders(monkeypatch)
    db.session.get(Site, 1).allowlist_mode = ALLOWLIST_STRONG
    db.session.add(AllowedInstances(domain='peer.example'))
    db.session.commit()

    with app.test_client() as client:
        response = signed_inbox_post(client, inbox_activity(signing_peer), signing_peer)

    assert response.status_code == 200
    assert len(inbox_recorder.inline) == 1


def test_a_delivery_from_an_actor_with_no_instance_skips_the_instance_bookkeeping(
        app, signing_peer, monkeypatch):
    """The last_seen/dormant/failures bookkeeping is for the sender's Instance
    row; an actor whose `instance_id` is NULL has none, and must be dispatched
    anyway rather than crash on `actor.instance.last_seen`."""
    monkeypatch.setitem(app.config, 'DEBUG', True)
    inbox_recorder, _ = _patch_dispatch_recorders(monkeypatch)
    peer_instance = signing_peer.instance
    peer_instance.failures = 3
    activity = inbox_activity(signing_peer)  # built first: the factory reads actor.instance
    signing_peer.instance_id = None
    db.session.commit()

    with app.test_client() as client:
        response = signed_inbox_post(client, activity, signing_peer)

    assert response.status_code == 200
    assert len(inbox_recorder.inline) == 1
    db.session.refresh(peer_instance)
    assert peer_instance.failures == 3


def test_replay_of_an_announce_of_remote_content_is_dispatched(app, signing_peer, monkeypatch):
    """replay_inbox_request's 'already present' refusal is only for an
    Announce whose inner object came from THIS server. A complete inner object
    from a remote actor must pass through to the dispatcher."""
    inbox_recorder, _ = _patch_dispatch_recorders(monkeypatch)
    request_json = _announce_of(signing_peer, 'https://other.example/notes/7')

    replay_inbox_request(request_json)

    assert inbox_recorder.inline == [((request_json, True), {})]
