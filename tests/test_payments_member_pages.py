"""/donations: paying and stopping paying (plan phase 1, Task 7)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import DONOR_WHILE_SUBSCRIBED
from app.models import MemberSubscription, Offer, OfferDomain, Payment, Site, SiteDomain
from app.payments.offers import money
from app.storefront import clear_domain_cache
from tests.factories import make_user


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    offer = Offer(provider_kind='stripe', kind='subscription', title='A small amount', offer_group='tiers',
                  provider_price_id='price_small', amount_minor=500, currency='usd', interval='month', grants_donor=True)
    db.session.add(offer)
    member = make_user(api_baseline.instance_local, 'member', local=True)
    other = make_user(api_baseline.instance_local, 'other', local=True)
    db.session.commit()
    return SimpleNamespace(app=app, client=app.test_client(), member=member, other=other, offer=offer)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def post(env, path, **data):
    with env.app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction() as session:
        session['csrf_token'] = raw
    return env.client.post(path, data={'csrf_token': token, **data})


def subscribe(member, reference='sub_1', offer=None):
    row = MemberSubscription(user_id=member.id, offer_id=offer.id if offer else None, provider_kind='stripe',
                             provider_reference=reference, status='active')
    db.session.add(row)
    member.donor_until = DONOR_WHILE_SUBSCRIBED
    db.session.commit()
    return row


class TestThePage:
    def test_a_member_is_offered_the_plans_with_the_amount(self, env):
        login(env.client, env.member)
        response = env.client.get('/donations')
        assert response.status_code == 200
        assert b'A small amount' in response.data and b'$5.00' in response.data and b'month' in response.data

    def test_every_offer_is_listed_in_its_group(self, env):
        db.session.add(Offer(provider_kind='stripe', kind='subscription', title='A larger amount', offer_group='tiers',
                             provider_price_id='price_big', amount_minor=1000, currency='usd', interval='month'))
        db.session.commit()
        login(env.client, env.member)
        body = env.client.get('/donations').data
        assert b'A small amount' in body and b'A larger amount' in body

    def test_a_stranger_is_sent_to_log_in(self, env):
        response = env.client.get('/donations')
        assert response.status_code == 302 and '/auth/login' in response.headers['Location']

    def test_an_inactive_offer_is_not_offered(self, env):
        env.offer.active = False
        db.session.commit()
        login(env.client, env.member)
        assert env.client.get('/donations').status_code == 404

    def test_no_stripe_means_no_offers(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', '')
        login(env.client, env.member)
        assert env.client.get('/donations').status_code == 404

    def test_a_legacy_offer_shows_no_amount(self, env):
        env.offer.amount_minor = None
        db.session.commit()
        login(env.client, env.member)
        assert b'$' not in env.client.get('/donations').data.split(b'A small amount')[1].split(b'</form>')[0]

    def test_a_subscriber_is_shown_how_to_stop_with_nothing_on_offer(self, env):
        env.offer.active = False
        subscribe(env.member, offer=env.offer)
        login(env.client, env.member)
        response = env.client.get('/donations')
        assert response.status_code == 200
        assert b'Cancel regular donation' in response.data and b'A small amount' in response.data

    def test_a_migrated_subscription_has_a_plain_name(self, env):
        subscribe(env.member)
        login(env.client, env.member)
        assert b'Regular donation' in env.client.get('/donations').data

    def test_only_somebody_with_a_customer_can_update_a_card(self, env):
        subscribe(env.member)
        login(env.client, env.member)
        assert b'Update credit card' not in env.client.get('/donations').data
        env.member.stripe_customer_id = 'cus_1'
        db.session.commit()
        assert b'Update credit card' in env.client.get('/donations').data

    def test_a_member_sees_only_their_own_payments(self, env):
        for user, amount, reference in ((env.member, 500, 'mine'), (env.other, 900, 'theirs')):
            db.session.add(Payment(user_id=user.id, offer_id=env.offer.id, provider_kind='stripe',
                                   provider_reference=reference, amount_minor=amount, currency='usd', status='paid'))
        db.session.commit()
        login(env.client, env.member)
        history = env.client.get('/donations').data.split(b'Your payments')[1]
        assert b'$5.00' in history and b'$9.00' not in history


class TestDomains:
    def test_an_offer_on_another_domain_is_not_offered_here(self, env, app):
        other = SiteDomain(domain='sister.example', scheme='https', registration='anyone')
        db.session.add(other)
        db.session.commit()
        db.session.add(OfferDomain(offer_id=env.offer.id, site_domain_id=other.id))
        db.session.commit()
        login(env.client, env.member)
        with patch('stripe.checkout.Session.create') as create:
            assert post(env, f'/donations/checkout/{env.offer.id}').status_code == 404
        assert not create.called
        assert env.client.get('/donations').status_code == 404


    def test_a_sister_domain_is_where_the_provider_sends_the_member_back(self, env):
        db.session.add(SiteDomain(domain='sister.example', scheme='https', registration='anyone'))
        db.session.commit()
        clear_domain_cache()
        with env.client.session_transaction(base_url='https://sister.example') as session:
            session['_user_id'] = str(env.member.id)
            session['_fresh'] = True
            with env.app.test_request_context():
                token = generate_csrf()
                session['csrf_token'] = flask_session['csrf_token']
        with patch('stripe.checkout.Session.create', return_value=SimpleNamespace(url='https://s')) as create:
            env.client.post(f'/donations/checkout/{env.offer.id}', base_url='https://sister.example',
                            data={'csrf_token': token})
        clear_domain_cache()
        assert create.call_args.kwargs['success_url'].startswith('https://sister.example/')


class TestCheckout:
    def test_it_goes_to_the_provider(self, env):
        login(env.client, env.member)
        with patch('stripe.checkout.Session.create', return_value=SimpleNamespace(url='https://stripe.test/c')) as create:
            response = post(env, f'/donations/checkout/{env.offer.id}')
        assert response.status_code == 302 and response.headers['Location'] == 'https://stripe.test/c'
        assert create.call_args.kwargs['success_url'].endswith('/donations/return/success')
        assert create.call_args.kwargs['cancel_url'].endswith('/donations/return/failure')

    def test_a_refusal_is_explained(self, env):
        login(env.client, env.member)
        with patch('stripe.checkout.Session.create', side_effect=stripe.APIConnectionError('down')):
            response = post(env, f'/donations/checkout/{env.offer.id}')
        assert response.status_code == 302 and response.headers['Location'].endswith('/donations')

    def test_an_unknown_offer_is_not_found(self, env):
        login(env.client, env.member)
        assert post(env, '/donations/checkout/987654').status_code == 404

    def test_it_needs_csrf(self, env):
        login(env.client, env.member)
        with patch('stripe.checkout.Session.create') as create:
            assert env.client.post(f'/donations/checkout/{env.offer.id}').status_code == 400
        assert not create.called

    def test_a_get_is_refused(self, env):
        login(env.client, env.member)
        assert env.client.get(f'/donations/checkout/{env.offer.id}').status_code == 405

    def test_a_stranger_is_sent_to_log_in(self, env):
        response = post(env, f'/donations/checkout/{env.offer.id}')
        assert response.status_code == 302 and '/auth/login' in response.headers['Location']

    def test_coming_back_grants_nothing(self, env):
        login(env.client, env.member)
        response = env.client.get('/donations/return/success')
        assert response.status_code == 200 and b'as soon as the payment is confirmed' in response.data
        assert env.member.donor_until is None and MemberSubscription.query.count() == 0

    def test_a_failed_return_says_so(self, env):
        login(env.client, env.member)
        assert b'Something went wrong' in env.client.get('/donations/return/failure').data

    def test_a_checkout_begun_before_this_release_comes_back(self, env):
        login(env.client, env.member)
        response = env.client.get('/stripe_result/success')
        assert response.status_code == 302 and response.headers['Location'].endswith('/donations/return/success')

    def test_any_other_return_is_not_found(self, env):
        login(env.client, env.member)
        assert env.client.get('/donations/return/other').status_code == 404


class TestCancelling:
    def test_your_own_is_cancelled_at_the_provider_then_here(self, env):
        row = subscribe(env.member, offer=env.offer)
        login(env.client, env.member)
        with patch('stripe.Subscription.cancel') as cancel:
            response = post(env, f'/donations/subscriptions/{row.id}/cancel')
        assert response.status_code == 302
        cancel.assert_called_once_with('sub_1', api_key='sk_test')
        assert row.status == 'ended' and not env.member.is_donor()

    def test_a_provider_refusal_changes_nothing(self, env):
        row = subscribe(env.member, offer=env.offer)
        login(env.client, env.member)
        with patch('stripe.Subscription.cancel', side_effect=stripe.APIConnectionError('down')):
            response = post(env, f'/donations/subscriptions/{row.id}/cancel')
        assert response.status_code == 302
        assert row.status == 'active' and env.member.is_donor()

    def test_somebody_elses_is_not_found(self, env):
        row = subscribe(env.other)
        login(env.client, env.member)
        with patch('stripe.Subscription.cancel') as cancel:
            assert post(env, f'/donations/subscriptions/{row.id}/cancel').status_code == 404
        assert not cancel.called and row.status == 'active'

    def test_one_already_ended_is_not_found(self, env):
        row = subscribe(env.member)
        row.status = 'ended'
        db.session.commit()
        login(env.client, env.member)
        assert post(env, f'/donations/subscriptions/{row.id}/cancel').status_code == 404

    def test_a_get_cancels_nothing(self, env):
        row = subscribe(env.member)
        login(env.client, env.member)
        assert env.client.get(f'/donations/subscriptions/{row.id}/cancel').status_code == 405
        assert row.status == 'active'

    def test_the_old_get_address_is_gone(self, env):
        row = subscribe(env.member)
        login(env.client, env.member)
        assert env.client.get(f'/plan_unsubscribe/{env.member.id}/sub_1').status_code == 404
        assert row.status == 'active'


class TestTheBillingPortal:
    def test_a_donor_is_taken_there(self, env):
        env.member.stripe_customer_id = 'cus_1'
        db.session.commit()
        login(env.client, env.member)
        with patch('stripe.billing_portal.Session.create', return_value=SimpleNamespace(url='https://portal')):
            response = post(env, '/donations/billing')
        assert response.headers['Location'] == 'https://portal'

    def test_somebody_who_has_never_donated_is_not(self, env):
        login(env.client, env.member)
        with patch('stripe.billing_portal.Session.create') as create:
            response = post(env, '/donations/billing')
        assert response.headers['Location'].endswith('/donations') and not create.called

    def test_a_refusal_is_not_a_crash(self, env):
        env.member.stripe_customer_id = 'cus_1'
        db.session.commit()
        login(env.client, env.member)
        with patch('stripe.billing_portal.Session.create', side_effect=stripe.APIConnectionError('down')):
            response = post(env, '/donations/billing')
        assert response.headers['Location'].endswith('/donations')


class TestMoney:
    @pytest.mark.parametrize('amount, currency, shown', [(500, 'usd', '$5.00'), (500, 'jpy', '¥500')], ids=['usd', 'jpy'])
    def test_a_currency_keeps_its_own_decimals(self, app, amount, currency, shown):
        with app.test_request_context():
            assert money(amount, currency) == shown
