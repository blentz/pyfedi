"""Refunds and cancellations end a subscription at the end of its current subscription month, or when what was paid
for runs out if that is sooner, for every provider (plan 2026-11-15-btc-daily-price.md, B6, B7)."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import MemberSubscription, Offer, Payment, Site, utcnow
from app.payments.core import apply_event, cancel_subscription, end_expired_prepaid, remind_expiring_prepaid
from app.payments.ending import month_end, scheduled_end
from app.payments.events import Paid, Refunded, SubscriptionEnded
from tests.factories import make_user


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    monthly = Offer(provider_kind='stripe', kind='subscription', title='Furcas', offer_group='tiers',
                    provider_price_id='price_m', amount_minor=666, currency='usd', interval='month', grants_donor=True)
    yearly = Offer(provider_kind='stripe', kind='subscription', title='Furcas yearly', offer_group='tiers',
                   provider_price_id='price_y', amount_minor=7500, currency='usd', interval='year')
    prepaid = Offer(provider_kind='btcpay', kind='subscription', title='Furcas in bitcoin', offer_group='tiers',
                    amount_minor=7500, currency='usd', interval='year')
    gift = Offer(provider_kind='stripe', kind='donation', title='Tip', offer_group='tips', provider_price_id='price_t',
                 amount_minor=500, currency='usd', grants_donor=True)
    db.session.add_all([monthly, yearly, prepaid, gift])
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    return SimpleNamespace(app=app, client=app.test_client(), monthly=monthly, yearly=yearly, prepaid=prepaid, gift=gift,
                           member=member)


@pytest.fixture(autouse=True)
def told():
    with patch('app.payments.core.cancel_at_provider') as cancel:
        yield cancel


def held(env, offer, started_days_ago, reference='sub_1', **values):
    kind = offer.provider_kind
    row = MemberSubscription(user_id=env.member.id, offer_id=offer.id, provider_kind=kind, provider_reference=reference,
                             status='active', created_at=utcnow() - timedelta(days=started_days_ago), **values)
    db.session.add(row)
    db.session.commit()
    return row


def post(env, path):
    with env.app.test_request_context():
        g.pop('csrf_token', None)
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction() as session:
        session['csrf_token'] = raw
        session['_user_id'], session['_fresh'] = str(env.member.id), True
    return env.client.post(path, data={'csrf_token': token})


class TestTheMonth:
    @pytest.mark.parametrize('start, moment, end', [
        (datetime(2026, 1, 17, 9), datetime(2026, 1, 20), datetime(2026, 2, 17, 9)),
        (datetime(2026, 1, 17, 9), datetime(2026, 5, 1), datetime(2026, 5, 17, 9)),
        (datetime(2026, 1, 17, 9), datetime(2026, 5, 17, 9), datetime(2026, 6, 17, 9)),   # a month's last instant has gone
        (datetime(2026, 1, 31), datetime(2026, 2, 10), datetime(2026, 2, 28)),           # the day clamped to February
        (datetime(2026, 1, 31), datetime(2026, 3, 1), datetime(2026, 3, 31)),            # and back to the 31st after
        (datetime(2027, 12, 20), datetime(2028, 1, 25), datetime(2028, 2, 20)),          # over a year's end
    ], ids=['first', 'fifth', 'on_the_boundary', 'clamped', 'unclamped', 'new_year'])
    def test_months_count_from_the_start(self, start, moment, end):
        assert month_end(start, moment) == end

    def test_paid_for_running_out_sooner_ends_it_then(self, env):
        row = held(env, env.prepaid, 2, reference='inv_1', current_period_end=utcnow() + timedelta(days=3))
        assert scheduled_end(row, utcnow()) == row.current_period_end

    def test_a_renewal_date_sooner_ends_it_then(self, env):
        row = held(env, env.monthly, 2, renews_at=utcnow() + timedelta(days=5))
        assert scheduled_end(row, utcnow()) == row.renews_at

    def test_otherwise_the_month_does(self, env):
        row = held(env, env.yearly, 100, renews_at=utcnow() + timedelta(days=265))
        now = utcnow()
        assert scheduled_end(row, now) == month_end(row.created_at, now) < row.renews_at


class TestCancelling:
    def test_a_yearly_plan_ends_at_the_months_end_and_stops_being_charged_now(self, env):
        row = held(env, env.yearly, 100, renews_at=utcnow() + timedelta(days=265))
        with patch('app.payments.adapters.stripe_adapter.StripeAdapter.cancel') as cancel:
            cancel_subscription(row)
        cancel.assert_called_once_with('sub_1')
        assert row.status == 'active' and row.ends_at == month_end(row.created_at, row.ends_at - timedelta(days=1))
        assert utcnow() < row.ends_at < row.renews_at

    def test_a_regular_donation_also_runs_to_the_months_end(self, env):
        row = held(env, env.monthly, 3)
        with patch('app.payments.adapters.stripe_adapter.StripeAdapter.cancel'):
            cancel_subscription(row)
        assert row.status == 'active' and row.ends_at is not None

    def test_the_providers_ending_event_waits_for_it(self, env):
        row = held(env, env.monthly, 3)
        with patch('app.payments.adapters.stripe_adapter.StripeAdapter.cancel'):
            cancel_subscription(row)
        apply_event('stripe', 'evt_deleted', [SubscriptionEnded(reference='sub_1')])
        assert row.status == 'active'

    def test_the_daily_sweep_ends_it_after(self, env):
        row = held(env, env.monthly, 40, ends_at=utcnow() - timedelta(minutes=1))
        end_expired_prepaid()
        assert row.status == 'ended'

    def test_a_prepaid_year_can_be_cancelled_and_ends_at_the_months_end(self, env):
        row = held(env, env.prepaid, 100, reference='inv_1', current_period_end=utcnow() + timedelta(days=265))
        answer = post(env, f'/donations/subscriptions/{row.id}/cancel')
        assert answer.status_code == 302
        assert row.status == 'active' and utcnow() < row.ends_at < row.current_period_end

    def test_a_cancelled_period_is_shown_as_such_and_not_offered_again(self, env):
        row = held(env, env.prepaid, 100, reference='inv_1', current_period_end=utcnow() + timedelta(days=265),
                   ends_at=utcnow() + timedelta(days=10))
        with env.client.session_transaction() as session:
            session['_user_id'], session['_fresh'] = str(env.member.id), True
        body = env.client.get('/donations').get_data(as_text=True)
        assert row.ends_at.strftime('%Y-%m-%d') in body and f'/donations/subscriptions/{row.id}/cancel' not in body

    def test_a_cancelled_period_is_not_reminded_to_pay_again(self, env):
        held(env, env.prepaid, 360, reference='inv_1', current_period_end=utcnow() + timedelta(days=5),
             ends_at=utcnow() + timedelta(days=5))
        with patch('app.payments.core.send_prepaid_reminder') as mail:
            assert remind_expiring_prepaid() == 0
        mail.assert_not_called()

    def test_cancelling_twice_changes_nothing(self, env):
        ends = utcnow() + timedelta(days=10)
        row = held(env, env.prepaid, 100, reference='inv_1', current_period_end=utcnow() + timedelta(days=265), ends_at=ends)
        assert post(env, f'/donations/subscriptions/{row.id}/cancel').status_code == 404
        assert row.ends_at == ends

    def test_a_gift_is_not_cancelled_by_its_member(self, env):
        row = held(env, env.prepaid, 1, reference='gift:1', current_period_end=utcnow() + timedelta(days=29))
        row.provider_kind = 'gift'
        db.session.commit()
        assert post(env, f'/donations/subscriptions/{row.id}/cancel').status_code == 404


class TestRefunds:
    def paid(self, env, offer, reference, kind='stripe'):
        apply_event(kind, f'evt_{reference}', [Paid(reference=reference, amount_minor=offer.amount_minor, currency='usd',
                                                     user_id=env.member.id, offer_id=offer.id)])

    def test_a_refunded_subscription_payment_ends_it_at_the_months_end(self, env, told):
        row = held(env, env.yearly, 100, renews_at=utcnow() + timedelta(days=265))
        self.paid(env, env.yearly, 'in_1')
        apply_event('stripe', 'evt_refund', [Refunded(reference='in_1')])
        assert Payment.query.filter_by(provider_reference='in_1').one().status == 'refunded'
        assert row.status == 'active' and utcnow() < row.ends_at < row.renews_at
        told.assert_called_once_with('stripe', 'sub_1')

    def test_a_refunded_prepaid_period_runs_to_the_months_end_not_now(self, env, told):
        row = held(env, env.prepaid, 100, reference='inv_1', current_period_end=utcnow() + timedelta(days=265))
        self.paid(env, env.prepaid, 'inv_1', kind='btcpay')
        apply_event('btcpay', 'evt_invalid', [Refunded(reference='inv_1'), SubscriptionEnded(reference='inv_1')])
        assert row.status == 'active' and utcnow() < row.ends_at < row.current_period_end

    def test_a_refunded_one_off_ends_no_subscription(self, env, told):
        row = held(env, env.monthly, 3)
        self.paid(env, env.gift, 'pi_1')
        apply_event('stripe', 'evt_refund', [Refunded(reference='pi_1')])
        assert row.ends_at is None and not told.called

    def test_a_subscription_already_ending_sooner_keeps_its_end(self, env, told):
        ends = utcnow() + timedelta(days=2)
        row = held(env, env.yearly, 100, renews_at=utcnow() + timedelta(days=265), ends_at=ends)
        self.paid(env, env.yearly, 'in_1')
        apply_event('stripe', 'evt_refund', [Refunded(reference='in_1')])
        assert row.ends_at == ends
