"""Web Push delivery (owner ruling 2026-11-09; app/push_delivery.py): a new notification is pushed to its person's
subscriptions that want it, from a clearnet token alone; a push service's 404 or 410 forgets the subscription."""
import json
from unittest.mock import patch

import pytest

import app.push_delivery as delivery
from app import db
from app.constants import NOTIF_BAN, NOTIF_FOLLOW, NOTIF_REPLY
from app.models import Notification, PushSubscription, SiteDomain, utcnow
from app.oauth_server import tokens
from app.storefront import clear_domain_cache, domain_for_host
from tests.factories import make_follow, make_user
from tests.test_webpush import AUTH, UA_PUBLIC
from tests.two_domains import request_on


@pytest.fixture(autouse=True)
def no_task_inside_the_commit(monkeypatch):
    """Celery runs eagerly in tests, which would deliver inside the commit that made a notification: the tests call
    `deliver` themselves."""
    monkeypatch.setattr(delivery.push_task, 'delay', lambda ids: None)


@pytest.fixture
def world(app, api_baseline):
    registered, _secret = tokens.register_app('Tusky', ['https://app.example/cb'], 'read push', None)
    me, other = make_user(None, 'pushed', local=True), make_user(None, 'pushauthor', local=True)
    access, _refresh = tokens.issue_token(registered, me, 'read push')
    token = tokens.token_for(access)
    subscription = PushSubscription(token_id=token.id, user_id=me.id, endpoint='https://push.example/s',
                                    p256dh=UA_PUBLIC, auth=AUTH, alerts={'mention': True, 'follow': False},
                                    policy='all')
    db.session.add(subscription)
    db.session.commit()
    return {'me': me, 'other': other, 'app': registered, 'token': token, 'subscription': subscription, 'flask': app}


def _notification(world, kind=NOTIF_REPLY, user=None, author=None):
    row = Notification(title='Someone replied to you', url='/x', user_id=(user or world['me']).id,
                       author_id=(author or world['other']).id, notif_type=kind, targets={})
    db.session.add(row)
    db.session.commit()
    return row


def _sent(world, *rows, status=201):
    with patch.object(delivery.webpush, 'send', return_value=status) as send:
        delivery.deliver([row.id for row in rows])
    return send


def test_a_new_notification_of_a_subscribed_person_is_queued_after_the_commit(world):
    with patch.object(delivery, 'queue_push') as queued:
        mine = _notification(world)
        _notification(world, user=world['other'], author=world['me'])   # nobody subscribed
        _notification(world, kind=NOTIF_BAN)                              # no Mastodon form
    assert [call.args[0] for call in queued.call_args_list] == [[mine.id]]


def test_a_rolled_back_notification_is_not_queued(world):
    with patch.object(delivery, 'queue_push') as queued:
        db.session.add(Notification(title='t', url='/x', user_id=world['me'].id, author_id=world['other'].id,
                                    notif_type=NOTIF_REPLY, targets={}))
        db.session.flush()
        db.session.rollback()
        db.session.commit()
    queued.assert_not_called()


def test_a_wanted_notification_is_sent_as_mastodons_apps_read_it(world):
    row = _notification(world)
    send = _sent(world, row)
    endpoint, p256dh, auth, payload, subject = send.call_args.args
    assert (endpoint, p256dh, auth) == ('https://push.example/s', UA_PUBLIC, AUTH)
    body = json.loads(payload)
    assert body['notification_id'] == str(row.id) and body['notification_type'] == 'mention'
    assert body['title'] == world['other'].display_name() and body['body'] == 'Someone replied to you'
    assert body['access_token'] == '' and subject.startswith('http')
    assert body['icon'] == '' or body['icon'].startswith('http')   # an author with no avatar shows none


def test_an_alert_that_is_off_or_a_kind_without_a_form_is_not_sent(world):
    assert not _sent(world, _notification(world, kind=NOTIF_FOLLOW)).called
    assert not _sent(world, _notification(world, kind=NOTIF_BAN)).called


@pytest.mark.parametrize('policy, follows, sent', [
    ('none', None, False), ('followed', None, False), ('followed', 'me-follows-them', True),
    ('follower', None, False), ('follower', 'they-follow-me', True), ('all', None, True)])
def test_the_policy_decides_from_whom(world, policy, follows, sent):
    world['subscription'].policy = policy
    db.session.commit()
    if follows == 'me-follows-them':
        make_follow(world['me'], world['other'])
    elif follows == 'they-follow-me':
        make_follow(world['other'], world['me'])
    assert _sent(world, _notification(world)).called is sent


def test_a_revoked_token_pushes_nothing(world):
    world['token'].revoked_at = utcnow()
    db.session.commit()
    assert not _sent(world, _notification(world)).called


def test_a_token_bound_to_an_onion_pushes_nothing(world):
    """Clearnet only: from an onion, a notification would leave through a third party's service."""
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False, is_onion=True))
    db.session.commit()
    clear_domain_cache()
    with request_on(world['flask'], domain_for_host('abc.onion')):
        access, _refresh = tokens.issue_token(world['app'], world['me'], 'read push')
        onion_token = tokens.token_for(access)
    world['subscription'].token_id = onion_token.id
    db.session.commit()
    assert not _sent(world, _notification(world)).called


def test_a_notification_hidden_here_is_not_sent(world):
    with patch.object(delivery, 'hidden_notifications', side_effect=lambda rows: [True for _ in rows]):
        assert not _sent(world, _notification(world)).called


@pytest.mark.parametrize('status', [404, 410])
def test_a_subscription_the_push_service_forgot_is_forgotten(world, status):
    _sent(world, _notification(world), status=status)
    db.session.expire_all()
    assert PushSubscription.query.count() == 0


def test_one_failing_push_service_does_not_stop_the_rest(world):
    second = PushSubscription(token_id=tokens.token_for(tokens.issue_token(world['app'], world['me'], 'push')[0]).id,
                              user_id=world['me'].id, endpoint='https://push2.example/s', p256dh=UA_PUBLIC, auth=AUTH,
                              alerts={'mention': True}, policy='all')
    db.session.add(second)
    db.session.commit()
    with patch.object(delivery.webpush, 'send', side_effect=[ConnectionError('down'), 201]) as send:
        delivery.deliver([_notification(world).id])
    assert send.call_count == 2


def test_nothing_or_nobody_is_nothing(world):
    assert not _sent(world).called
    with patch.object(delivery.webpush, 'send') as send:
        delivery.deliver([2 ** 31 - 1])
    send.assert_not_called()


def test_a_fault_reading_the_notifications_is_raised_for_the_task(world):
    with patch.object(delivery, 'hidden_notifications', side_effect=RuntimeError('db')), \
            pytest.raises(RuntimeError):
        delivery.deliver([_notification(world).id])


def test_queue_push_is_always_a_task_whose_body_delivers(world):
    with patch.object(delivery, 'deliver') as delivered, patch.object(delivery.push_task, 'delay') as later:
        delivery.queue_push([2])
        delivery.push_task([3])   # the task's body, run in place
    later.assert_called_once_with([2])
    delivered.assert_called_once_with([3])


def test_a_notification_with_no_author_has_an_empty_title(world):
    row = _notification(world)
    row.author_id = None
    db.session.commit()
    body = json.loads(_sent(world, row).call_args.args[3])
    assert body['title'] == '' and body['icon'] == ''


def test_an_authors_avatar_is_an_absolute_icon(world):
    with patch.object(delivery.User, 'avatar_image', return_value='/static/media/users/a.png'):
        body = json.loads(_sent(world, _notification(world)).call_args.args[3])
    assert body['icon'].startswith('http') and body['icon'].endswith('/static/media/users/a.png')
