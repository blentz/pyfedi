"""Mastodon's Web Push subscription (owner ruling 2026-11-09: push notifications, clearnet only): subscribe, read,
change and end one per token; refused on an onion; the VAPID key published on a clearnet domain alone."""
from types import SimpleNamespace

import pytest

import app.mastodon.instance as instance_module
import app.mastodon.push as push_module
from app import db, webpush
from app.models import PushSubscription
from app.oauth_server import tokens
from tests.factories import make_user
from tests.test_webpush import AUTH, UA_PUBLIC

ENDPOINT = 'https://push.example/send/abc'


@pytest.fixture
def world(app, api_baseline, monkeypatch):
    monkeypatch.setattr(webpush, 'is_invalid_get_request_uri', lambda uri: False)   # no DNS in a test
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read write follow push', None)
    me = make_user(None, 'pusher', local=True)
    access, _refresh = tokens.issue_token(registered, me, 'read push')
    return {'me': me, 'app': registered, 'token': access, 'client': app.test_client(), 'flask': app}


def _call(world, method, token=None, **kwargs):
    return getattr(world['client'], method)('/api/v1/push/subscription',
                                            headers={'Authorization': f"Bearer {token or world['token']}"}, **kwargs)


def _json_body(**alerts):
    return {'subscription': {'endpoint': ENDPOINT, 'keys': {'p256dh': UA_PUBLIC, 'auth': AUTH}},
            'data': {'alerts': alerts, 'policy': 'followed'}}


def test_subscribing_with_json_answers_mastodons_subscription(world):
    response = _call(world, 'post', json=_json_body(mention=True, follow='true', favourite=False))
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body['endpoint'] == ENDPOINT and body['policy'] == 'followed' and body['server_key'] == webpush.server_key()
    assert body['alerts']['mention'] is True and body['alerts']['follow'] is True and body['alerts']['status'] is False
    stored = PushSubscription.query.one()
    assert stored.user_id == world['me'].id and stored.p256dh == UA_PUBLIC


def test_subscribing_with_a_form_as_tusky_sends_it(world):
    response = _call(world, 'post', data={'subscription[endpoint]': ENDPOINT, 'subscription[keys][p256dh]': UA_PUBLIC,
                                          'subscription[keys][auth]': AUTH, 'data[alerts][mention]': 'true',
                                          'data[policy]': 'all'})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()['alerts']['mention'] is True


def test_a_new_subscription_replaces_the_tokens_old_one(world):
    _call(world, 'post', json=_json_body(mention=True))
    _call(world, 'post', json=_json_body(follow=True))
    assert PushSubscription.query.count() == 1 and PushSubscription.query.one().alerts['mention'] is False


def test_read_change_and_end_it(world):
    assert _call(world, 'get').status_code == 404
    assert _call(world, 'put', json={'data': {'alerts': {'status': True}}}).status_code == 404
    _call(world, 'post', json=_json_body(mention=True))
    assert _call(world, 'get').get_json()['alerts']['mention'] is True
    changed = _call(world, 'put', json={'data': {'alerts': {'status': True}, 'policy': 'none'}}).get_json()
    assert changed['alerts']['status'] is True and changed['alerts']['mention'] is True and changed['policy'] == 'none'
    assert _call(world, 'put', json={'data': {'policy': 'everyone'}}).status_code == 422
    assert _call(world, 'delete').get_json() == {} and PushSubscription.query.count() == 0


@pytest.mark.parametrize('body', [
    {'subscription': {'endpoint': 'http://push.example/x', 'keys': {'p256dh': UA_PUBLIC, 'auth': AUTH}}},
    {'subscription': {'endpoint': ENDPOINT, 'keys': {'p256dh': 'nonsense', 'auth': AUTH}}},
    {'subscription': {'endpoint': ENDPOINT, 'keys': {'p256dh': UA_PUBLIC, 'auth': 'c2hvcnQ'}}},
    {'subscription': {'endpoint': ENDPOINT}},
    {'subscription': 'x'},
    {**_json_body(), 'data': {'policy': 'everyone'}},
    ['not', 'an', 'object'],
], ids=['http', 'bad-key', 'short-auth', 'no-keys', 'no-subscription', 'bad-policy', 'not-an-object'])
def test_a_subscription_that_cannot_be_sent_to_is_refused(world, body):
    assert _call(world, 'post', json=body).status_code == 422
    assert PushSubscription.query.count() == 0


def test_a_subscription_needs_the_push_scope_and_a_person(world):
    reader, _refresh = tokens.issue_token(world['app'], world['me'], 'read')
    assert _call(world, 'post', token=reader, json=_json_body()).status_code == 403
    app_token, _none = tokens.issue_token(world['app'], None, 'push', with_refresh=False)
    assert _call(world, 'post', token=app_token, json=_json_body()).status_code == 422


def test_ending_the_token_ends_its_subscription(world):
    _call(world, 'post', json=_json_body(mention=True))
    db.session.delete(tokens.token_for(world['token']))
    db.session.commit()
    assert PushSubscription.query.count() == 0


def test_the_vapid_key_is_in_v2_instance_on_clearnet(world):
    configuration = world['client'].get('/api/v2/instance').get_json()['configuration']
    assert configuration['vapid'] == {'public_key': webpush.server_key()}


def test_on_an_onion_there_is_no_push(world, monkeypatch):
    """Clearnet only: a notification sent from an onion would leave through a third party's service."""
    _call(world, 'post', json=_json_body(mention=True))   # made on clearnet
    monkeypatch.setattr(push_module, 'current_storefront', lambda: SimpleNamespace(is_onion=True))
    for method, body in (('post', _json_body()), ('put', {'data': {'policy': 'none'}})):
        response = _call(world, method, json=body)
        assert response.status_code == 422 and 'not available' in response.get_json()['error']
    assert PushSubscription.query.one().policy == 'followed'


def test_the_vapid_key_is_not_published_on_an_onion(world):
    assert instance_module._vapid(SimpleNamespace(is_onion=True)) == {}
