"""Donations: choosing a plan, the Stripe webhook, and cancelling.

Sub-project 88 -- `app/user/subscription.py`. These five routes are the only
ones on the instance that move money, and one of them, `/stripe_webhook`, is
an unauthenticated POST endpoint open to the whole internet. It is protected
by a signature check, which the tests below exercise for real rather than by
mocking it out: every good payload here is signed with the configured secret,
so the verification path runs on every one of them.

Five defects, all measured before anything was asserted:

* `/plan_unsubscribe/<account_id>/<subscription>` had no `else` arm, so a
  subscription id that was not the caller's fell off the end of the view and
  Flask raised "did not return a valid response" -- a 500 where a refusal was
  the answer (D1261);
* `/stripe_redirect/billing` read `stripe_session['url']` out of whatever
  Stripe returned. A signed-in account that had never donated has no Stripe
  customer, so Stripe answered 400 with an error body and the page was
  `KeyError: 'url'` (D1262);
* the webhook handed `client_reference_id` straight to `db.session.get(User,
  ...)`. Anything non-numeric was a `DataError`, which aborted the
  transaction and returned 500 -- and Stripe retries a failing webhook for
  days (D1263);
* the webhook assigned `stripe_session['customer']` unconditionally, so a
  session carrying no customer was `KeyError: 'customer'`, and one carrying a
  null customer erased the customer id already stored -- which then made
  D1262 reachable for an account that HAD donated (D1264);
* `event['type']`, `event['data']` and `subscription['id']` were all indexed
  without a membership test, each one a 500 and another endless Stripe retry
  (D1265).
"""
import hashlib
import hmac
import json
import time
from unittest.mock import patch

import httpx
import pytest
from flask import current_app, g

from app import db
from app.models import Site, User

SECRET = 'whsec_test_secret'


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), site=g.site,
                           donor=api_baseline.user2, baseline=api_baseline)


@pytest.fixture
def stripe_configured(monkeypatch):
    """What the instance looks like once its operator has set Stripe up."""
    for key, value in (('STRIPE_SECRET_KEY', 'sk_test'),
                       ('STRIPE_PUBLISHABLE_KEY', 'pk_test'),
                       ('STRIPE_MONTHLY_SMALL', 'price_small'),
                       ('STRIPE_MONTHLY_BIG', 'price_big'),
                       ('STRIPE_MONTHLY_SMALL_TEXT', 'A small amount'),
                       ('STRIPE_MONTHLY_BIG_TEXT', 'A larger amount'),
                       ('WEBHOOK_SIGNING_SECRET', SECRET)):
        monkeypatch.setitem(current_app.config, key, value)


def sign_in(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def signed_post(client, payload, secret=SECRET, timestamp=None):
    """POST to the webhook the way Stripe does, signature and all."""
    if not isinstance(payload, (bytes, str)):
        payload = json.dumps(payload)
    if isinstance(payload, str):
        payload = payload.encode()
    timestamp = timestamp or int(time.time())
    signature = hmac.new(secret.encode(),
                         b'%d.%s' % (timestamp, payload),
                         hashlib.sha256).hexdigest()
    return client.post('/stripe_webhook', data=payload,
                       headers={'Stripe-Signature':
                                f't={timestamp},v1={signature}'})


def checkout_completed(**session):
    return {'id': 'evt_1', 'type': 'checkout.session.completed',
            'data': {'object': session}}


class TestChoosingAPlan:
    """`/donate` is the one page here anonymous visitors may see."""

    def test_a_stranger_is_shown_the_donation_page(self, env):
        response = env.client.get('/donate')
        assert response.status_code == 200

    def test_a_stranger_is_invited_to_log_in_when_stripe_is_set_up(
            self, env, stripe_configured):
        response = env.client.get('/donate')
        assert response.status_code == 200
        assert b'Log in to donate' in response.data

    def test_a_member_is_offered_the_plans(self, env, stripe_configured):
        sign_in(env.client, env.donor)
        response = env.client.get('/donate')
        assert response.status_code == 200
        assert b'A small amount' in response.data
        assert b'A larger amount' in response.data

    def test_a_member_is_shown_the_plain_page_when_stripe_is_not_set_up(
            self, env):
        sign_in(env.client, env.donor)
        response = env.client.get('/donate')
        assert response.status_code == 200
        assert b'A small amount' not in response.data

    def test_somebody_already_donating_is_told_a_choice_replaces_it(
            self, env, stripe_configured):
        env.donor.stripe_subscription_id = 'sub_existing'
        db.session.commit()
        sign_in(env.client, env.donor)
        response = env.client.get('/donate')
        assert b'replace your current donation plan' in response.data


class TestStartingACheckout:
    def test_a_stranger_is_sent_to_log_in(self, env, stripe_configured):
        response = env.client.get('/stripe_redirect/monthly_small')
        assert response.status_code == 302
        assert 'login' in response.headers['Location']

    def test_the_small_plan_is_priced_from_the_configuration(
            self, env, stripe_configured):
        sign_in(env.client, env.donor)
        with patch('stripe.checkout.Session.create',
                   return_value={'id': 'cs_1'}) as create:
            assert env.client.get(
                '/stripe_redirect/monthly_small').status_code == 200
        assert create.call_args.kwargs['line_items'][0]['price'] == 'price_small'
        assert create.call_args.kwargs['mode'] == 'subscription'

    def test_the_big_plan_is_priced_from_the_configuration(
            self, env, stripe_configured):
        sign_in(env.client, env.donor)
        with patch('stripe.checkout.Session.create',
                   return_value={'id': 'cs_1'}) as create:
            assert env.client.get(
                '/stripe_redirect/monthly_big').status_code == 200
        assert create.call_args.kwargs['line_items'][0]['price'] == 'price_big'

    def test_the_session_names_the_account_paying(self, env,
                                                  stripe_configured):
        sign_in(env.client, env.donor)
        with patch('stripe.checkout.Session.create',
                   return_value={'id': 'cs_1'}) as create:
            env.client.get('/stripe_redirect/monthly_small')
        assert create.call_args.kwargs['client_reference_id'] == env.donor.id

    def test_a_newcomer_is_identified_by_email(self, env, stripe_configured):
        """Stripe refuses both at once, so a first-time donor sends only the
        address."""
        env.donor.stripe_customer_id = None
        db.session.commit()
        sign_in(env.client, env.donor)
        with patch('stripe.checkout.Session.create',
                   return_value={'id': 'cs_1'}) as create:
            env.client.get('/stripe_redirect/monthly_small')
        assert create.call_args.kwargs['customer'] is None
        assert create.call_args.kwargs['customer_email'] == env.donor.email

    def test_an_empty_customer_id_counts_as_none(self, env,
                                                 stripe_configured):
        env.donor.stripe_customer_id = ''
        db.session.commit()
        sign_in(env.client, env.donor)
        with patch('stripe.checkout.Session.create',
                   return_value={'id': 'cs_1'}) as create:
            env.client.get('/stripe_redirect/monthly_small')
        assert create.call_args.kwargs['customer'] is None
        assert create.call_args.kwargs['customer_email'] == env.donor.email

    def test_a_returning_donor_is_identified_by_customer(self, env,
                                                        stripe_configured):
        env.donor.stripe_customer_id = 'cus_known'
        db.session.commit()
        sign_in(env.client, env.donor)
        with patch('stripe.checkout.Session.create',
                   return_value={'id': 'cs_1'}) as create:
            env.client.get('/stripe_redirect/monthly_small')
        assert create.call_args.kwargs['customer'] == 'cus_known'
        assert create.call_args.kwargs['customer_email'] is None

    def test_a_plan_nobody_offers_buys_nothing(self, env, stripe_configured):
        sign_in(env.client, env.donor)
        with patch('stripe.checkout.Session.create') as create:
            response = env.client.get('/stripe_redirect/monthly_enormous')
        assert response.status_code == 200
        assert create.call_count == 0


class TestTheBillingPortal:
    """`/stripe_redirect/billing` asks Stripe for a one-off portal link."""

    PORTAL = 'https://api.stripe.com/v1/billing_portal/sessions'

    def test_a_stranger_is_sent_to_log_in(self, env, stripe_configured):
        response = env.client.get('/stripe_redirect/billing')
        assert response.status_code == 302
        assert 'login' in response.headers['Location']

    def test_somebody_who_has_never_donated_is_not_taken_to_stripe(
            self, env, stripe_configured, http_mock):
        """D1262. No customer means nothing for the portal to show, and
        asking anyway earned a 400 whose body has no `url` in it."""
        env.donor.stripe_customer_id = None
        db.session.commit()
        sign_in(env.client, env.donor)
        response = env.client.get('/stripe_redirect/billing')
        assert response.status_code == 302
        assert '/donate' in response.headers['Location']
        assert len(http_mock.calls) == 0   # no route is registered: a request
                                           # to Stripe here would fail unmocked

    def test_a_donor_is_taken_to_the_portal(self, env, stripe_configured,
                                            http_mock):
        env.donor.stripe_customer_id = 'cus_known'
        db.session.commit()
        sign_in(env.client, env.donor)
        http_mock.post(self.PORTAL).mock(return_value=httpx.Response(
            200, json={'url': 'https://billing.stripe.test/session/xyz'}))
        response = env.client.get('/stripe_redirect/billing')
        assert response.status_code == 302
        assert response.headers['Location'] == \
            'https://billing.stripe.test/session/xyz'

    def test_stripe_refusing_is_not_a_crash(self, env, stripe_configured,
                                            http_mock):
        """D1262. The error body has `error`, not `url`."""
        env.donor.stripe_customer_id = 'cus_stale'
        db.session.commit()
        sign_in(env.client, env.donor)
        http_mock.post(self.PORTAL).mock(return_value=httpx.Response(
            400, json={'error': {'message': 'No such customer'}}))
        response = env.client.get('/stripe_redirect/billing')
        assert response.status_code == 302
        assert '/donate' in response.headers['Location']

    def test_an_answer_that_is_not_json_is_not_a_crash(
            self, env, stripe_configured, http_mock):
        """A proxy between here and Stripe can answer with an HTML error
        page, which `response.json()` will not parse."""
        env.donor.stripe_customer_id = 'cus_known'
        db.session.commit()
        sign_in(env.client, env.donor)
        http_mock.post(self.PORTAL).mock(return_value=httpx.Response(
            502, text='<html>Bad Gateway</html>'))
        response = env.client.get('/stripe_redirect/billing')
        assert response.status_code == 302
        assert '/donate' in response.headers['Location']

    def test_an_answer_that_is_json_but_not_an_object(
            self, env, stripe_configured, http_mock):
        env.donor.stripe_customer_id = 'cus_known'
        db.session.commit()
        sign_in(env.client, env.donor)
        http_mock.post(self.PORTAL).mock(
            return_value=httpx.Response(200, json=['not', 'an', 'object']))
        response = env.client.get('/stripe_redirect/billing')
        assert response.status_code == 302
        assert '/donate' in response.headers['Location']


class TestWhoMaySpeakToTheWebhook:
    """The one route here that anyone on the internet may POST to."""

    def test_a_request_with_no_signature_is_refused(self, env,
                                                    stripe_configured):
        response = env.client.post('/stripe_webhook',
                                   data=json.dumps(checkout_completed()))
        assert response.status_code == 400
        assert b'could not verify signature' in response.data

    def test_a_signature_from_the_wrong_secret_is_refused(
            self, env, stripe_configured):
        response = signed_post(env.client, checkout_completed(),
                               secret='whsec_somebody_elses')
        assert response.status_code == 400
        assert b'could not verify signature' in response.data

    def test_a_signature_that_is_not_one_is_refused(self, env,
                                                    stripe_configured):
        response = env.client.post(
            '/stripe_webhook', data=json.dumps(checkout_completed()),
            headers={'Stripe-Signature': 'nonsense'})
        assert response.status_code == 400

    def test_a_replayed_signature_is_refused(self, env, stripe_configured):
        """Stripe's default tolerance is five minutes; this one is a day
        old."""
        response = signed_post(env.client, checkout_completed(),
                               timestamp=int(time.time()) - 86400)
        assert response.status_code == 400

    def test_a_signed_payload_that_is_not_json_is_refused(
            self, env, stripe_configured):
        response = signed_post(env.client, b'not json at all')
        assert response.status_code == 400
        assert b'invalid payload' in response.data

    def test_nothing_is_stored_for_a_request_that_is_refused(
            self, env, stripe_configured):
        """The signature check is the only thing standing between a stranger
        and a free donor badge."""
        env.donor.stripe_subscription_id = None
        db.session.commit()
        signed_post(env.client,
                    checkout_completed(client_reference_id=env.donor.id,
                                       customer='cus_forged',
                                       subscription='sub_forged'),
                    secret='whsec_somebody_elses')
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id is None
        assert env.donor.stripe_customer_id != 'cus_forged'


class TestACheckoutThatCompleted:
    def test_the_customer_and_the_subscription_are_stored(
            self, env, stripe_configured):
        response = signed_post(env.client, checkout_completed(
            client_reference_id=env.donor.id, customer='cus_1',
            subscription='sub_1'))
        assert response.status_code == 200
        db.session.refresh(env.donor)
        assert env.donor.stripe_customer_id == 'cus_1'
        assert env.donor.stripe_subscription_id == 'sub_1'

    def test_a_new_plan_cancels_the_old_one_at_stripe(self, env,
                                                      stripe_configured):
        env.donor.stripe_subscription_id = 'sub_old'
        db.session.commit()
        with patch('stripe.Subscription.delete') as delete:
            signed_post(env.client, checkout_completed(
                client_reference_id=env.donor.id, customer='cus_1',
                subscription='sub_new'))
        delete.assert_called_once_with('sub_old')
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id == 'sub_new'

    def test_the_same_plan_again_cancels_nothing(self, env,
                                                 stripe_configured):
        """Stripe delivers the same event more than once by design."""
        env.donor.stripe_subscription_id = 'sub_1'
        db.session.commit()
        with patch('stripe.Subscription.delete') as delete:
            signed_post(env.client, checkout_completed(
                client_reference_id=env.donor.id, customer='cus_1',
                subscription='sub_1'))
        assert delete.call_count == 0
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id == 'sub_1'

    def test_stripe_refusing_the_cancellation_does_not_lose_the_new_plan(
            self, env, stripe_configured):
        env.donor.stripe_subscription_id = 'sub_old'
        db.session.commit()
        with patch('stripe.Subscription.delete',
                   side_effect=Exception('already cancelled')):
            response = signed_post(env.client, checkout_completed(
                client_reference_id=env.donor.id, customer='cus_1',
                subscription='sub_new'))
        assert response.status_code == 200
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id == 'sub_new'

    def test_a_one_off_payment_stores_no_subscription(self, env,
                                                      stripe_configured):
        """A checkout in `payment` mode carries a null subscription."""
        env.donor.stripe_subscription_id = None
        db.session.commit()
        response = signed_post(env.client, checkout_completed(
            client_reference_id=env.donor.id, customer='cus_1',
            subscription=None))
        assert response.status_code == 200
        db.session.refresh(env.donor)
        assert env.donor.stripe_customer_id == 'cus_1'
        assert env.donor.stripe_subscription_id is None

    def test_a_session_with_no_subscription_key_at_all(self, env,
                                                       stripe_configured):
        response = signed_post(env.client, checkout_completed(
            client_reference_id=env.donor.id, customer='cus_1'))
        assert response.status_code == 200
        db.session.refresh(env.donor)
        assert env.donor.stripe_customer_id == 'cus_1'


class TestWhoTheCheckoutSaysItIsFor:
    """`client_reference_id` is attached to the session at checkout, so it is
    whatever was attached -- not necessarily a user id, or a number."""

    def test_a_session_naming_nobody(self, env, stripe_configured):
        """D1263. `db.session.get(User, None)` warned and matched nothing."""
        response = signed_post(env.client, checkout_completed(
            client_reference_id=None, customer='cus_1', subscription='sub_1'))
        assert response.status_code == 200

    def test_a_session_with_no_reference_at_all(self, env,
                                                stripe_configured):
        response = signed_post(env.client, {
            'type': 'checkout.session.completed',
            'data': {'object': {'customer': 'cus_1'}}})
        assert response.status_code == 200

    def test_a_reference_that_is_not_a_number(self, env, stripe_configured):
        """D1263. This was `DataError: invalid input syntax for type
        integer`, which aborted the transaction and answered 500. Stripe
        retries a 500 for days."""
        response = signed_post(env.client, checkout_completed(
            client_reference_id='not-a-number', customer='cus_1',
            subscription='sub_1'))
        assert response.status_code == 200

    def test_a_reference_that_is_a_number_in_a_string(self, env,
                                                      stripe_configured):
        """Stripe stores it as a string and gives it back as one."""
        response = signed_post(env.client, checkout_completed(
            client_reference_id=str(env.donor.id), customer='cus_str',
            subscription='sub_str'))
        assert response.status_code == 200
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id == 'sub_str'

    def test_a_reference_to_an_account_that_is_gone(self, env,
                                                    stripe_configured):
        response = signed_post(env.client, checkout_completed(
            client_reference_id=999999, customer='cus_1',
            subscription='sub_1'))
        assert response.status_code == 200

    def test_an_account_that_is_gone_stores_nothing_anywhere(
            self, env, stripe_configured):
        before = User.query.filter_by(stripe_customer_id='cus_1').count()
        signed_post(env.client, checkout_completed(
            client_reference_id=999999, customer='cus_1',
            subscription='sub_1'))
        assert User.query.filter_by(stripe_customer_id='cus_1').count() == before


class TestTheCustomerOnACompletedCheckout:
    def test_a_session_carrying_no_customer_key(self, env,
                                                stripe_configured):
        """D1264. This was `KeyError: 'customer'`."""
        response = signed_post(env.client, checkout_completed(
            client_reference_id=env.donor.id, subscription='sub_1'))
        assert response.status_code == 200
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id == 'sub_1'

    def test_a_null_customer_does_not_erase_the_one_on_file(
            self, env, stripe_configured):
        """D1264. Erasing it would have left the donor unable to open the
        billing portal, which is how D1262 was reachable for somebody who had
        in fact donated."""
        env.donor.stripe_customer_id = 'cus_known'
        db.session.commit()
        signed_post(env.client, checkout_completed(
            client_reference_id=env.donor.id, customer=None,
            subscription='sub_1'))
        db.session.refresh(env.donor)
        assert env.donor.stripe_customer_id == 'cus_known'

    def test_a_new_customer_replaces_the_one_on_file(self, env,
                                                     stripe_configured):
        env.donor.stripe_customer_id = 'cus_old'
        db.session.commit()
        signed_post(env.client, checkout_completed(
            client_reference_id=env.donor.id, customer='cus_new',
            subscription='sub_1'))
        db.session.refresh(env.donor)
        assert env.donor.stripe_customer_id == 'cus_new'


class TestAnEventShapedUnexpectedly:
    """D1265. Each of these was a 500, and a 500 is what Stripe retries."""

    def test_an_event_with_no_data(self, env, stripe_configured):
        assert signed_post(env.client, {
            'type': 'checkout.session.completed'}).status_code == 200

    def test_an_event_whose_data_holds_no_object(self, env,
                                                 stripe_configured):
        assert signed_post(env.client, {
            'type': 'checkout.session.completed',
            'data': {}}).status_code == 200

    def test_an_event_whose_object_is_not_one(self, env, stripe_configured):
        assert signed_post(env.client, {
            'type': 'checkout.session.completed',
            'data': {'object': 'a string'}}).status_code == 200

    def test_an_event_with_no_type(self, env, stripe_configured):
        assert signed_post(env.client, {
            'data': {'object': {}}}).status_code == 200

    def test_a_kind_of_event_this_instance_ignores(self, env,
                                                   stripe_configured):
        assert signed_post(env.client, {
            'type': 'invoice.paid',
            'data': {'object': {'id': 'in_1'}}}).status_code == 200


class TestASubscriptionThatEnded:
    def test_the_donor_stops_being_one(self, env, stripe_configured):
        env.donor.stripe_subscription_id = 'sub_ending'
        db.session.commit()
        response = signed_post(env.client, {
            'type': 'customer.subscription.deleted',
            'data': {'object': {'id': 'sub_ending'}}})
        assert response.status_code == 200
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id is None

    def test_a_subscription_nobody_here_holds(self, env, stripe_configured):
        env.donor.stripe_subscription_id = 'sub_mine'
        db.session.commit()
        response = signed_post(env.client, {
            'type': 'customer.subscription.deleted',
            'data': {'object': {'id': 'sub_somebody_elses'}}})
        assert response.status_code == 200
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id == 'sub_mine'

    def test_an_event_naming_no_subscription_cancels_nobody(
            self, env, stripe_configured):
        """A null id matches every account that is not donating, and writing
        null over null changes nothing -- so the guard in the handler is
        belt-and-braces and no mutant of it is observable. What IS observable
        is the absent key: that was `KeyError: 'id'` (D1265)."""
        env.donor.stripe_subscription_id = 'sub_mine'
        db.session.commit()
        response = signed_post(env.client, {
            'type': 'customer.subscription.deleted',
            'data': {'object': {'id': None}}})
        assert response.status_code == 200
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id == 'sub_mine'

    def test_an_event_with_no_id_key(self, env, stripe_configured):
        response = signed_post(env.client, {
            'type': 'customer.subscription.deleted', 'data': {'object': {}}})
        assert response.status_code == 200


class TestComingBackFromStripe:
    def test_a_stranger_is_sent_to_log_in(self, env):
        response = env.client.get('/stripe_result/success')
        assert response.status_code == 302
        assert 'login' in response.headers['Location']

    @pytest.mark.parametrize('result', ['success', 'failure'])
    def test_the_two_outcomes_are_shown(self, env, result):
        sign_in(env.client, env.donor)
        response = env.client.get(f'/stripe_result/{result}')
        assert response.status_code == 200
        assert response.data

    def test_anything_else_says_nothing(self, env):
        sign_in(env.client, env.donor)
        response = env.client.get('/stripe_result/whatever')
        assert response.status_code == 200
        assert response.data == b''


class TestCancellingADonation:
    def test_a_stranger_is_sent_to_log_in(self, env):
        response = env.client.get('/plan_unsubscribe/acct_1/sub_1')
        assert response.status_code == 302
        assert 'login' in response.headers['Location']

    def test_somebody_who_is_not_donating_is_told_so(self, env,
                                                    stripe_configured):
        env.donor.stripe_subscription_id = None
        db.session.commit()
        sign_in(env.client, env.donor)
        with patch('stripe.Subscription.delete') as delete:
            response = env.client.get('/plan_unsubscribe/acct_1/sub_1')
        assert response.status_code == 200
        assert b'no regular donations' in response.data
        assert delete.call_count == 0

    def test_your_own_donation_is_cancelled(self, env, stripe_configured):
        env.donor.stripe_subscription_id = 'sub_mine'
        db.session.commit()
        sign_in(env.client, env.donor)
        with patch('stripe.Subscription.delete') as delete:
            response = env.client.get('/plan_unsubscribe/acct_1/sub_mine')
        assert response.status_code == 200
        assert b'has been cancelled' in response.data
        delete.assert_called_once_with('sub_mine')
        db.session.refresh(env.donor)
        assert env.donor.stripe_subscription_id is None

    def test_a_donation_that_is_not_yours_is_refused(self, env,
                                                    stripe_configured):
        """D1261. With no `else` arm the view returned None and Flask raised
        "did not return a valid response", so a guessed subscription id was a
        500 rather than a refusal."""
        env.donor.stripe_subscription_id = 'sub_mine'
        db.session.commit()
        sign_in(env.client, env.donor)
        with patch('stripe.Subscription.delete') as delete:
            response = env.client.get('/plan_unsubscribe/acct_1/sub_theirs')
        assert response.status_code == 200
        assert b'not one of your regular donations' in response.data
        assert delete.call_count == 0

    def test_nobody_else_s_donation_is_touched(self, env, stripe_configured):
        """The account id in the URL is not what authorises this; the
        subscription id matching the caller's own is."""
        other = env.baseline.user1
        other.stripe_subscription_id = 'sub_theirs'
        env.donor.stripe_subscription_id = 'sub_mine'
        db.session.commit()
        sign_in(env.client, env.donor)
        with patch('stripe.Subscription.delete') as delete:
            env.client.get(f'/plan_unsubscribe/{other.id}/sub_theirs')
        assert delete.call_count == 0
        db.session.refresh(other)
        assert other.stripe_subscription_id == 'sub_theirs'
