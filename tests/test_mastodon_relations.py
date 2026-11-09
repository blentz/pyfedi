"""A person's relations from a Mastodon app (plan W1.3): follow, unfollow, block, unblock, relationships and follow
requests, through the shared functions (mutes: tests/test_mutes.py)."""
from unittest.mock import patch

import pytest

import app.mastodon.relations as relations_module
from app import db
from app.models import Notification, UserBlock, UserFollower
from app.oauth_server import tokens
from tests.factories import make_follow, make_instance, make_user


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow', None)
    me = make_user(None, 'relater', local=True, with_keys=True)
    open_person = make_user(None, 'openperson', local=True)
    locked = make_user(None, 'lockedperson', local=True)
    locked.ap_manually_approves_followers = True
    db.session.commit()
    access, _refresh = tokens.issue_token(registered, me, 'read write follow')
    return {'me': me, 'open': open_person, 'locked': locked, 'app': registered, 'token': access,
            'client': app.test_client()}


def _post(world, path, token=None):
    return world['client'].post(path, headers={'Authorization': f"Bearer {token or world['token']}"})


def _get(world, path, **query):
    return world['client'].get(path, headers={'Authorization': f"Bearer {world['token']}"}, query_string=query)


def test_follow_and_unfollow_an_open_account(world):
    other = world['open']
    followed = _post(world, f'/api/v1/accounts/{other.id}/follow')
    assert followed.status_code == 200, followed.get_data(as_text=True)
    assert followed.get_json()['following'] is True and followed.get_json()['requested'] is False
    assert _post(world, f'/api/v1/accounts/{other.id}/follow').get_json()['following'] is True   # idempotent
    assert Notification.query.filter_by(user_id=other.id).count() == 1
    unfollowed = _post(world, f'/api/v1/accounts/{other.id}/unfollow')
    assert unfollowed.get_json()['following'] is False


def test_following_an_account_that_approves_followers_is_requested(world):
    relationship = _post(world, f"/api/v1/accounts/{world['locked'].id}/follow").get_json()
    assert relationship['following'] is False and relationship['requested'] is True


def test_block_and_unblock(world):
    other = world['open']
    assert _post(world, f'/api/v1/accounts/{other.id}/block').get_json()['blocking'] is True
    assert UserBlock.query.filter_by(blocker_id=world['me'].id, blocked_id=other.id).count() == 1
    assert _post(world, f'/api/v1/accounts/{other.id}/unblock').get_json()['blocking'] is False


def test_a_shared_refusal_is_a_422_with_its_reason(world):
    response = _post(world, f"/api/v1/accounts/{world['me'].id}/block")
    assert response.status_code == 422 and response.get_json()['error'] == 'cannot_block_self'


def test_a_fault_is_a_500_with_no_text(world, monkeypatch):
    monkeypatch.setattr(relations_module.shared_user, 'follow_user', lambda *args: 1 / 0)
    response = _post(world, f"/api/v1/accounts/{world['open'].id}/follow")
    assert response.status_code == 500 and response.get_json() == {'error': 'Something went wrong'}


@pytest.mark.parametrize('account_id', ['99999999', '99999999999'], ids=['nobody', 'huge'])
def test_an_account_nobody_holds_is_not_found(world, account_id):
    assert _post(world, f'/api/v1/accounts/{account_id}/follow').status_code == 404


def test_relationships_say_both_ways(world):
    me, other, locked = world['me'], world['open'], world['locked']
    make_follow(me, other)
    make_follow(other, me)
    db.session.add(UserBlock(blocker_id=locked.id, blocked_id=me.id))
    db.session.commit()
    answer = _get(world, '/api/v1/accounts/relationships',
                  **{'id[]': [str(other.id), str(locked.id), 'x', '99999999']}).get_json()
    assert [relation['id'] for relation in answer] == [str(other.id), str(locked.id)]
    assert answer[0]['following'] is True and answer[0]['followed_by'] is True and answer[0]['muting'] is False
    assert answer[1]['blocked_by'] is True and answer[1]['following'] is False


def test_follow_requests_listed_authorized_and_rejected(world):
    me = world['me']
    asking, other_asking = make_user(make_instance('far.example'), 'asker'), make_user(make_instance('far2.example'),
                                                                                       'asker2')
    db.session.add_all([UserFollower(local_user_id=me.id, remote_user_id=asking.id, is_inward=True, is_accepted=None),
                        UserFollower(local_user_id=me.id, remote_user_id=other_asking.id, is_inward=True,
                                     is_accepted=None)])
    db.session.commit()
    listed = _get(world, '/api/v1/follow_requests').get_json()
    assert sorted(account['username'] for account in listed) == ['asker', 'asker2']
    with patch('app.activitypub.signature.send_post_request') as send:
        accepted = _post(world, f'/api/v1/follow_requests/{asking.id}/authorize').get_json()
        rejected = _post(world, f'/api/v1/follow_requests/{other_asking.id}/reject').get_json()
    assert accepted['followed_by'] is True and rejected['followed_by'] is False
    assert [call.args[1]['type'] for call in send.call_args_list] == ['Accept', 'Reject']
    assert _get(world, '/api/v1/follow_requests').get_json() == []
    assert _post(world, f'/api/v1/follow_requests/{asking.id}/reject').status_code == 200   # answered again: still one
    assert _post(world, '/api/v1/follow_requests/99999999/authorize').status_code == 404
    assert _post(world, '/api/v1/follow_requests/99999999999/authorize').status_code == 404


def test_each_needs_its_scope(world):
    reader, _refresh = tokens.issue_token(world['app'], world['me'], 'read')
    assert _post(world, f"/api/v1/accounts/{world['open'].id}/follow", token=reader).status_code == 403


def test_a_local_requesters_answer_needs_no_message_sent(world):
    neighbour = make_user(None, 'neighbour', local=True)
    db.session.add(UserFollower(local_user_id=world['me'].id, remote_user_id=neighbour.id, is_inward=True,
                                is_accepted=None))
    db.session.commit()
    with patch('app.activitypub.signature.send_post_request') as send:
        assert _post(world, f'/api/v1/follow_requests/{neighbour.id}/authorize').get_json()['followed_by'] is True
    send.assert_not_called()
