"""Deleting an account stops charging it (plan phase 1, Task 10). Until now a deleted account went on paying."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.admin.util import unsubscribe_from_everything_then_delete
from app.constants import DONOR_WHILE_SUBSCRIBED
from app.models import MemberSubscription, Offer, Payment, Site
from app.payments.core import end_subscriptions_of
from app.user.utils import purge_user_then_delete_task
from tests.factories import grant_permission, make_user
from tests.test_user_moderation import resolvable


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    member = make_user(api_baseline.instance_local, 'member', local=True)
    member.verified = True
    member.donor_until = DONOR_WHILE_SUBSCRIBED
    offer = Offer(provider_kind='stripe', kind='subscription', title='Plan', offer_group='plans')
    db.session.add(offer)
    db.session.flush()
    db.session.add_all([MemberSubscription(user_id=member.id, offer_id=offer.id, provider_kind='stripe',
                                           provider_reference='sub_1', status='active'),
                        MemberSubscription(user_id=member.id, offer_id=offer.id, provider_kind='stripe',
                                           provider_reference='sub_old', status='ended'),
                        Payment(user_id=member.id, offer_id=offer.id, provider_kind='stripe', provider_reference='in_1',
                                amount_minor=500, currency='usd', status='paid')])
    db.session.commit()
    return SimpleNamespace(app=app, client=app.test_client(), member=member, baseline=api_baseline)


@pytest.fixture
def cancelled():
    with patch('app.payments.core.cancel_at_provider') as cancel:
        yield cancel


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(env):
    with env.app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def assert_ended(env, cancelled):
    db.session.expire_all()
    assert MemberSubscription.query.filter_by(status='active').count() == 0
    cancelled.assert_called_once_with('stripe', 'sub_1')
    payment = Payment.query.one()
    assert payment.user_id is None and payment.amount_minor == 500


def test_the_helper_ends_cancels_and_clears(env, cancelled):
    end_subscriptions_of(env.member)
    assert_ended(env, cancelled)
    assert env.member.donor_until is None


def test_a_provider_being_down_does_not_hold_up_the_deletion(env):
    with patch('app.payments.tasks.cancel_at_provider_task.delay') as delay:
        end_subscriptions_of(env.member)
    delay.assert_called_once_with('stripe', 'sub_1')


def test_deleting_your_own_account(env, cancelled):
    login(env.client, env.member)
    with patch('app.user.routes.send_deletion_requests'):
        env.client.post('/delete_account', data={'csrf_token': csrf(env), 'submit': 'y'})
    assert_ended(env, cancelled)


def test_an_admin_deleting_the_profile(env, cancelled):
    admin = make_user(env.baseline.instance_local, 'deleter', local=True)
    admin.verified = True
    db.session.commit()
    grant_permission(admin, 'manage users')
    grant_permission(admin, 'administer all users')
    login(env.client, admin)
    resolvable(env.member)
    env.client.post(f'/u/{env.member.user_name}/delete', data={'csrf_token': csrf(env)})
    assert_ended(env, cancelled)


def test_a_purge(env, cancelled):
    with patch('app.user.utils.task_selector'):
        purge_user_then_delete_task.run(env.member.id, False)
    assert_ended(env, cancelled)


def test_the_admin_lists_delete(env, cancelled):
    unsubscribe_from_everything_then_delete(env.member.id)
    assert_ended(env, cancelled)
