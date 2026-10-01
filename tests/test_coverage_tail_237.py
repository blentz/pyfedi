"""Round 237: the admin's two irreversible federation actions.

`app/admin/routes.py` holds the buttons that act on other servers' behalf or on this
server's whole database. Two of them had no rows:

    /admin/activity_json/<id>/replay   feeds a STORED inbox activity back through the
                                       inbox as though it had just arrived
    /admin/community/<id>/delete       bans the community immediately, then queues a task
                                       that unsubscribes every member and deletes it

Replay is worth rows because the activity it re-runs was written by a peer and has already
been processed once: an admin clicking it a second time re-applies whatever that activity
did. Delete is worth rows because the ban and the deletion are separate steps -- the ban
exists so the community disappears from the UI while the slow half runs -- and because the
remote and local arms of the task do different work.

The task's remote arm also calls `sleep(5)` between unsubscribing and deleting, which is
patched here: a row is not the place to wait five seconds, and what it is waiting for is
outbound federation, not anything a test can observe.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.admin.routes import (unsubscribe_everyone_then_delete,
                              unsubscribe_everyone_then_delete_task)
from app.models import ActivityPubLog, Community, CommunityMember, Site
from tests.factories import (grant_permission, make_community, make_community_member,
                             make_instance, make_user)


def csrf(app, client):
    """A real CSRF token. `login_required(csrf=True)` validates the token itself,
    independently of `WTF_CSRF_ENABLED` -- which conftest sets False -- so a POST without
    one is refused BEFORE any authorization check, and an authorization row would pass for
    the wrong reason.
    """
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    admin = make_user(api_baseline.instance_local, 'siteadmin', local=True)
    admin.verified = True
    admin.private_key = 'x'
    grant_permission(admin, 'change instance settings')
    grant_permission(admin, 'administer all communities')
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)
        session['_fresh'] = True
    return SimpleNamespace(app=app, site=site, admin=admin, client=client,
                           anonymous=app.test_client(), baseline=api_baseline)


# --------------------------------------------------------------------------
# Replaying a stored activity
# --------------------------------------------------------------------------


class TestReplayingAStoredActivity:

    @pytest.fixture
    def stored(self, env):
        activity = ActivityPubLog(
            direction='in', activity_id='https://peer.example/activities/1',
            activity_type='Create', result='failure',
            activity_json=json.dumps({'id': 'https://peer.example/activities/1',
                                      'type': 'Create',
                                      'actor': 'https://peer.example/u/someone'}))
        db.session.add(activity)
        db.session.commit()
        env.activity = activity
        return env

    def test_the_stored_json_is_fed_back_through_the_inbox(self, stored):
        """The whole point of the button: an activity that failed is re-run with the same
        body it arrived with. The assertion is the PARSED json, not the string -- the route
        is what deserializes it, and handing the raw string to `replay_inbox_request` would
        be a different call that still 'worked'."""
        replayed = []

        with patch('app.admin.routes.replay_inbox_request',
                   side_effect=lambda payload: replayed.append(payload)):
            response = stored.client.get(
                f'/admin/activity_json/{stored.activity.id}/replay')

        assert response.status_code == 200
        assert response.get_data(as_text=True) == 'Ok'
        assert replayed == [{'id': 'https://peer.example/activities/1',
                             'type': 'Create',
                             'actor': 'https://peer.example/u/someone'}]

    @pytest.mark.parametrize('message', [
        'Could not verify HTTP signature: Invalid signature',
        'Could not verify LD signature: Invalid signature',
        'Precheck failed: Digest is incorrect',
    ])
    def test_an_activity_that_failed_signature_verification_is_not_replayed(self, stored, message):
        """D45, fixed (owner ruling 2026-09-30). Replay skips every signature check
        `shared_inbox` performs, so re-running a row the inbox logged as a signature
        failure would process unverified peer content as though it had passed. Such a
        row is now refused with a message saying why; the three messages are the ones
        `shared_inbox` logs at its precheck, HTTP-signature and LD-signature refusals."""
        stored.activity.exception_message = message
        db.session.commit()
        replayed = []

        with patch('app.admin.routes.replay_inbox_request',
                   side_effect=lambda payload: replayed.append(payload)):
            response = stored.client.get(
                f'/admin/activity_json/{stored.activity.id}/replay')

        assert response.status_code == 400
        assert 'signature' in response.get_data(as_text=True)
        assert replayed == []

    def test_an_activity_that_failed_for_another_reason_is_still_replayed(self, stored):
        """D45's boundary: only signature failures are refused; any other failure
        replays as before."""
        stored.activity.exception_message = 'Actor could not be found 1 - : x, actor object: None'
        db.session.commit()
        replayed = []

        with patch('app.admin.routes.replay_inbox_request',
                   side_effect=lambda payload: replayed.append(payload)):
            response = stored.client.get(
                f'/admin/activity_json/{stored.activity.id}/replay')

        assert response.status_code == 200
        assert len(replayed) == 1

    def test_an_activity_that_does_not_exist_is_a_404(self, stored):
        """`or abort(404)`. The id comes from the URL, and the line below it would be
        `AttributeError: 'NoneType' object has no attribute 'activity_json'`."""
        replayed = []

        with patch('app.admin.routes.replay_inbox_request',
                   side_effect=lambda payload: replayed.append(payload)):
            response = stored.client.get('/admin/activity_json/999999/replay')

        assert response.status_code == 404
        assert replayed == []

    def test_an_ordinary_account_cannot_replay_anything(self, stored):
        """`@permission_required('change instance settings')`. Replaying re-applies whatever
        the activity did -- a Delete, a ban, a vote -- so it is not a read-only admin page
        with a harmless failure mode."""
        ordinary = make_user(stored.baseline.instance_local, 'nosy', local=True)
        ordinary.verified = True
        db.session.commit()
        client = stored.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(ordinary.id)
            session['_fresh'] = True
        replayed = []

        with patch('app.admin.routes.replay_inbox_request',
                   side_effect=lambda payload: replayed.append(payload)):
            response = client.get(f'/admin/activity_json/{stored.activity.id}/replay')

        assert response.status_code == 302
        assert replayed == []


# --------------------------------------------------------------------------
# Deleting a community
# --------------------------------------------------------------------------


class TestDeletingACommunity:

    @pytest.fixture
    def seeded(self, env):
        peer = make_instance('deleter.example')
        remote = make_community('goneaway', host='deleter.example')
        remote.instance_id = peer.id
        remote.ap_id = 'goneaway@deleter.example'
        member = make_user(env.baseline.instance_local, 'joiner', local=True)
        db.session.commit()
        make_community_member(member, remote)
        db.session.commit()
        env.peer = peer
        env.remote = remote
        env.member = member
        return env

    def test_the_route_bans_the_community_before_queueing_the_slow_half(self, seeded):
        """The ban is not incidental: unsubscribing every member of a large community takes
        long enough that the community would otherwise stay listed, and joinable, while it
        is being torn down. It is committed BEFORE the task is dispatched."""
        banned_when_dispatched = []

        def observe(community_id):
            # Read through a SEPARATE connection: the route's own session would report the
            # attribute whether or not it had been committed, and what this row is about is
            # that the ban is durable before the slow half starts.
            from sqlalchemy import text

            with db.engine.connect() as connection:
                banned_when_dispatched.append(connection.execute(
                    text('SELECT banned FROM community WHERE id = :id'),
                    {'id': community_id}).scalar_one())

        with patch('app.admin.routes.unsubscribe_everyone_then_delete',
                   side_effect=observe):
            response = seeded.client.post(
                f'/admin/community/{seeded.remote.id}/delete',
                data={'csrf_token': csrf(seeded.app, seeded.client)})

        assert response.status_code == 302
        assert banned_when_dispatched == [True]

    def test_a_community_that_does_not_exist_is_a_404(self, seeded):
        with patch('app.admin.routes.unsubscribe_everyone_then_delete') as dispatch:
            response = seeded.client.post(
                '/admin/community/999999/delete',
                data={'csrf_token': csrf(seeded.app, seeded.client)})

        assert response.status_code == 404
        assert dispatch.call_count == 0

    def test_an_ordinary_account_cannot_delete_a_community(self, seeded):
        ordinary = make_user(seeded.baseline.instance_local, 'nosy', local=True)
        ordinary.verified = True
        db.session.commit()
        client = seeded.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(ordinary.id)
            session['_fresh'] = True

        with patch('app.admin.routes.unsubscribe_everyone_then_delete') as dispatch:
            response = client.post(
                f'/admin/community/{seeded.remote.id}/delete',
                data={'csrf_token': csrf(seeded.app, client)})

        assert response.status_code == 302
        assert dispatch.call_count == 0
        assert db.session.get(Community, seeded.remote.id) is not None

    def test_in_production_the_work_is_queued(self, seeded, monkeypatch):
        monkeypatch.setattr(seeded.app, 'debug', False)
        queued = []

        with patch('app.admin.routes.unsubscribe_everyone_then_delete_task') as task:
            task.delay.side_effect = lambda cid: queued.append(cid)
            unsubscribe_everyone_then_delete(seeded.remote.id)

        assert queued == [seeded.remote.id]

    def test_in_debug_it_runs_inline(self, seeded, monkeypatch):
        monkeypatch.setattr(seeded.app, 'debug', True)
        ran = []

        with patch('app.admin.routes.unsubscribe_everyone_then_delete_task') as task:
            task.side_effect = lambda cid: ran.append(cid)
            unsubscribe_everyone_then_delete(seeded.remote.id)

            assert task.delay.call_count == 0

        assert ran == [seeded.remote.id]


class TestTheDeletionTaskItself:

    @pytest.fixture
    def seeded(self, env):
        peer = make_instance('deleter.example')
        remote = make_community('goneaway', host='deleter.example')
        remote.instance_id = peer.id
        remote.ap_id = 'goneaway@deleter.example'
        local = make_community('stayingput')
        first = make_user(env.baseline.instance_local, 'joiner1', local=True)
        second = make_user(env.baseline.instance_local, 'joiner2', local=True)
        db.session.commit()
        make_community_member(first, remote)
        make_community_member(second, remote)
        make_community_member(first, local)
        db.session.commit()
        env.remote = remote
        env.local = local
        env.members = [first, second]
        return env

    def test_every_member_of_a_remote_community_is_unsubscribed_then_it_is_deleted(
            self, seeded):
        """The remote arm. Each member gets an Undo/Follow sent to the community's own
        server -- that is what `unsubscribe_from_community` does -- so the row asserts one
        call PER MEMBER, not merely that the function was reached."""
        unsubscribed = []
        community_id = seeded.remote.id

        with patch('app.admin.routes.unsubscribe_from_community',
                   side_effect=lambda community, user: unsubscribed.append(user.id)), \
                patch('app.admin.routes.sleep'):
            unsubscribe_everyone_then_delete_task(community_id)

        assert sorted(unsubscribed) == sorted(u.id for u in seeded.members)
        # The task commits in its OWN session (`get_task_session`), so this session's
        # identity map still holds the row it loaded earlier.
        db.session.expire_all()
        assert db.session.get(Community, community_id) is None

    def test_a_local_community_is_deleted_without_unsubscribing_anyone(self, seeded):
        """The `else`. A local community's members are on this instance, so there is
        nobody to tell -- the `...` in that arm is a todo for federating the delete OUT,
        and the row pins that nothing is sent today."""
        unsubscribed = []
        community_id = seeded.local.id

        with patch('app.admin.routes.unsubscribe_from_community',
                   side_effect=lambda community, user: unsubscribed.append(user.id)), \
                patch('app.admin.routes.sleep'):
            unsubscribe_everyone_then_delete_task(community_id)

        assert unsubscribed == []
        db.session.expire_all()
        assert db.session.get(Community, community_id) is None

    def test_the_membership_rows_go_with_it(self, seeded):
        """`delete_dependencies()` before `session.delete(community)`. A CommunityMember
        row left behind is a foreign key pointing at nothing, and the delete itself would
        fail on it."""
        community_id = seeded.remote.id

        with patch('app.admin.routes.unsubscribe_from_community'), \
                patch('app.admin.routes.sleep'):
            unsubscribe_everyone_then_delete_task(community_id)

        db.session.expire_all()
        assert CommunityMember.query.filter_by(community_id=community_id).count() == 0
        # The other community's membership is untouched.
        assert CommunityMember.query.filter_by(
            community_id=seeded.local.id).count() == 1

    def test_a_failure_rolls_back_rather_than_leaving_it_half_deleted(self, seeded):
        """`except Exception: session.rollback(); raise`. The task deletes dependencies and
        then the community in one transaction; a failure between them must not leave a
        community whose memberships are gone.

        The `session.rollback()` itself is an EQUIVALENT MUTANT: removing it changes
        nothing, because `finally: session.close()` rolls the session back anyway. It is
        kept because it states the intent at the point the decision is made, and because
        `raise` -- which is NOT equivalent, and is killed by this row -- depends on the
        session being clean. Recorded so the survivor is not read as a missing row.
        """
        community_id = seeded.remote.id

        with patch('app.admin.routes.unsubscribe_from_community',
                   side_effect=Exception('the peer is unreachable')), \
                patch('app.admin.routes.sleep'):
            with pytest.raises(Exception, match='unreachable'):
                unsubscribe_everyone_then_delete_task(community_id)

        db.session.expire_all()
        assert db.session.get(Community, community_id) is not None
        assert CommunityMember.query.filter_by(community_id=community_id).count() == 2
