"""Mastodon's Web Push subscription (owner ruling 2026-11-09: push notifications, clearnet only; app/webpush.py).

An app with the `push` scope subscribes its token: POST /api/v1/push/subscription with the push service's endpoint
and the keys to encrypt for, and which notifications it wants (`data[alerts][...]`, Mastodon's names) from whom
(`data[policy]`); GET reads it, PUT changes the alerts and policy, DELETE ends it. One subscription per token, a new
one replacing it. Never on an onion storefront: a notification sent there would leave through a third party's
service. The endpoint must be https on a public address and not ours (webpush.acceptable_endpoint)."""
from flask import g, jsonify, request

from app import db, webpush
from app.mastodon import bp
from app.models import PushSubscription
from app.oauth_server.protect import require_oauth
from app.storefront import current_storefront

ALERTS = ('mention', 'status', 'reblog', 'follow', 'follow_request', 'favourite', 'poll', 'update', 'admin.sign_up',
          'admin.report', 'quote', 'quoted_update')
POLICIES = ('all', 'followed', 'follower', 'none')


class _Refused(Exception):
    pass


def _body() -> dict:
    """The request as Mastodon's nested names: JSON as sent, or a form's `subscription[keys][auth]` and the like."""
    if request.is_json:
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}
    nested = {}
    for name, value in request.form.items():
        parts = name.replace(']', '').split('[')
        place = nested
        for part in parts[:-1]:
            if not isinstance(place.get(part), dict):
                place[part] = {}
            place = place[part]
        place[parts[-1]] = value
    return nested


def _truthy(value) -> bool:
    return value is True or str(value).lower() in ('1', 'true')


def _data(body: dict, current: PushSubscription | None) -> tuple[dict, str]:
    data = body.get('data') if isinstance(body.get('data'), dict) else {}
    asked = data.get('alerts') if isinstance(data.get('alerts'), dict) else {}
    alerts = dict(current.alerts) if current is not None else {name: False for name in ALERTS}
    alerts.update({name: _truthy(asked[name]) for name in ALERTS if name in asked})
    policy = data.get('policy', current.policy if current is not None else 'all')
    if policy not in POLICIES:
        raise _Refused(f'Validation failed: {policy!r} is not a policy')
    return alerts, policy


def _keys(subscription: dict) -> tuple[str, str, str]:
    endpoint = subscription.get('endpoint')
    keys = subscription.get('keys') if isinstance(subscription.get('keys'), dict) else {}
    p256dh, auth = keys.get('p256dh'), keys.get('auth')
    if not webpush.acceptable_endpoint(endpoint):
        raise _Refused('Validation failed: the endpoint must be an https address of a push service')
    try:
        webpush.encrypt(b'', p256dh, auth)   # a key that is no P-256 point, or no auth secret, cannot be encrypted for
        if len(webpush.unb64(auth)) != 16:
            raise ValueError('auth')
    except (TypeError, ValueError, AttributeError):
        raise _Refused('Validation failed: the subscription keys are not a P-256 key and a 16-byte secret')
    return endpoint, p256dh, auth


def _entity(subscription: PushSubscription) -> dict:
    return {'id': str(subscription.id), 'endpoint': subscription.endpoint, 'standard': False,
            'alerts': subscription.alerts, 'policy': subscription.policy, 'server_key': webpush.server_key()}


def _mine() -> PushSubscription | None:
    return PushSubscription.query.filter_by(token_id=g.oauth_token.id).first()


def _clearnet_only():
    if current_storefront().is_onion:
        return jsonify({'error': 'Push notifications are not available on this address'}), 422
    return None


@bp.route('/api/v1/push/subscription', methods=['POST'])
@require_oauth('push')
def subscribe():
    if (refusal := _clearnet_only()) is not None:
        return refusal
    body = _body()
    try:
        endpoint, p256dh, auth = _keys(body.get('subscription') if isinstance(body.get('subscription'), dict) else {})
        alerts, policy = _data(body, None)
    except _Refused as refused:
        return jsonify({'error': str(refused)}), 422
    PushSubscription.query.filter_by(token_id=g.oauth_token.id).delete()
    subscription = PushSubscription(token_id=g.oauth_token.id, user_id=g.oauth_user.id, endpoint=endpoint,
                                    p256dh=p256dh, auth=auth, alerts=alerts, policy=policy)
    db.session.add(subscription)
    db.session.commit()
    return jsonify(_entity(subscription))


@bp.route('/api/v1/push/subscription')
@require_oauth('push')
def get_subscription():
    subscription = _mine()
    if subscription is None:
        return jsonify({'error': 'Record not found'}), 404
    return jsonify(_entity(subscription))


@bp.route('/api/v1/push/subscription', methods=['PUT'])
@require_oauth('push')
def update_subscription():
    if (refusal := _clearnet_only()) is not None:
        return refusal
    subscription = _mine()
    if subscription is None:
        return jsonify({'error': 'Record not found'}), 404
    try:
        subscription.alerts, subscription.policy = _data(_body(), subscription)
    except _Refused as refused:
        return jsonify({'error': str(refused)}), 422
    db.session.commit()
    return jsonify(_entity(subscription))


@bp.route('/api/v1/push/subscription', methods=['DELETE'])
@require_oauth('push')
def unsubscribe():
    PushSubscription.query.filter_by(token_id=g.oauth_token.id).delete()
    db.session.commit()
    return jsonify({})
