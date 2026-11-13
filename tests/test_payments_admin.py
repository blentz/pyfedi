"""The admin's payments area (spec 2026-11-05-monetization-infrastructure-design.md, plan phase 1 Tasks 1 and 9)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe
from cryptography.fernet import Fernet
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import MemberSubscription, Offer, OfferDomain, Payment, PaymentProvider, Site, SiteDomain
from app.payments.credentials import seal, unseal
from app.storefront import clear_domain_cache
from app.utils import get_setting, set_setting
from tests.factories import grant_permission, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, client=app.test_client(), baseline=api_baseline)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def somebody(env, name, *permissions):
    user = make_user(env.baseline.instance_local, name, local=True)
    user.verified = True
    db.session.commit()
    for permission in permissions:
        grant_permission(user, permission)
    return user


class TestTheTab:
    def test_a_holder_sees_it(self, env):
        login(env.client, somebody(env, 'holder', 'manage monetization'))
        assert b'id="payments-tab"' in env.client.get('/admin/payments').data

    def test_somebody_with_another_permission_does_not(self, env):
        login(env.client, somebody(env, 'cms', 'edit cms pages'))
        assert b'id="payments-tab"' not in env.client.get('/admin/pages').data


SECRET = 'sk_test_plaintext_secret'
WEBHOOK = 'whsec_plaintext_secret'


@pytest.fixture
def key(app, monkeypatch):
    monkeypatch.setitem(app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())


@pytest.fixture
def admin(env):
    user = somebody(env, 'holder', 'manage monetization')
    login(env.client, user)
    return user


def csrf(env):
    with env.app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def post(env, path, **data):
    return env.client.post(path, data={'csrf_token': csrf(env), **data})


def store(values=None, enabled=True):
    row = PaymentProvider(kind='stripe', enabled=enabled, credentials=seal(values or {'secret_key': SECRET,
                                                                                      'webhook_secret': WEBHOOK}))
    db.session.add(row)
    db.session.commit()
    return row


def price(amount=500, currency='usd', interval='month'):
    return SimpleNamespace(unit_amount=amount, currency=currency,
                           recurring=SimpleNamespace(interval=interval) if interval else None)


def an_offer(**values):
    offer = Offer(**{'provider_kind': 'stripe', 'kind': 'subscription', 'title': 'Plan', 'offer_group': 'plans',
                     'provider_price_id': 'price_1', 'amount_minor': 500, 'currency': 'usd', **values})
    db.session.add(offer)
    db.session.commit()
    return offer


OFFER = {'title': 'Plan', 'price_id': 'price_1', 'offer_group': 'tiers', 'grants_donor': 'y', 'position': '2', 'active': 'y'}


class TestStatus:
    def test_a_missing_key_is_explained(self, env, admin):
        assert b'flask generate-payment-key' in env.client.get('/admin/payments').data

    def test_a_valid_key_is_not_nagged_about(self, env, admin, key):
        assert b'flask generate-payment-key' not in env.client.get('/admin/payments').data

    def test_the_webhook_address_and_events_are_listed(self, env, admin):
        body = env.client.get('/admin/payments').data
        assert b'/payments/webhook/stripe' in body and b'invoice.paid' in body and b'charge.refunded' in body

    def test_not_connected(self, env, admin):
        assert b'Stripe is not connected' in env.client.get('/admin/payments').data


class TestCredentials:
    def test_they_are_stored_sealed(self, env, admin, key):
        with patch('stripe.Price.list'):
            response = post(env, '/admin/payments/stripe', secret_key=SECRET, webhook_secret=WEBHOOK, enabled='y')
        assert response.status_code == 302
        row = PaymentProvider.query.one()
        assert SECRET not in row.credentials and unseal(row.credentials) == {'secret_key': SECRET, 'webhook_secret': WEBHOOK}
        assert row.enabled

    def test_they_are_never_shown(self, env, admin, key):
        store()
        body = env.client.get('/admin/payments').get_data(as_text=True)
        assert SECRET not in body and WEBHOOK not in body
        assert 'credentials are stored' in body

    def test_they_are_not_shown_after_a_failed_save(self, env, admin, key):
        with patch('stripe.Price.list', side_effect=stripe.AuthenticationError('bad')):
            response = env.client.post('/admin/payments/stripe', follow_redirects=True,
                                       data={'csrf_token': csrf(env), 'secret_key': SECRET, 'webhook_secret': WEBHOOK})
        body = response.get_data(as_text=True)
        assert 'refused' in body and SECRET not in body and WEBHOOK not in body

    def test_an_invalid_form_shows_nothing_back(self, env, admin, key):
        body = post(env, '/admin/payments/stripe', secret_key='x' * 300, webhook_secret=WEBHOOK).get_data(as_text=True)
        assert 'x' * 300 not in body and WEBHOOK not in body

    def test_blank_keeps_what_is_stored_and_asks_stripe_nothing(self, env, admin, key):
        row = store()
        before = row.credentials
        with patch('stripe.Price.list') as listing:
            post(env, '/admin/payments/stripe', enabled='y')
        assert row.credentials == before and not listing.called

    def test_one_field_replaces_one_value(self, env, admin, key):
        row = store()
        with patch('stripe.Price.list'):
            post(env, '/admin/payments/stripe', webhook_secret='whsec_new', enabled='y')
        assert unseal(row.credentials) == {'secret_key': SECRET, 'webhook_secret': 'whsec_new'}

    def test_a_refused_key_keeps_the_old_one(self, env, admin, key):
        row = store()
        with patch('stripe.Price.list', side_effect=stripe.AuthenticationError('bad')):
            post(env, '/admin/payments/stripe', secret_key='sk_test_wrong', enabled='y')
        assert unseal(row.credentials)['secret_key'] == SECRET

    def test_with_no_key_nothing_is_stored(self, env, admin):
        with patch('stripe.Price.list') as listing:
            post(env, '/admin/payments/stripe', secret_key=SECRET, enabled='y')
        assert PaymentProvider.query.count() == 0 and not listing.called

    def test_a_value_the_key_cannot_open_can_be_replaced(self, env, admin, key, app, monkeypatch):
        row = store()
        monkeypatch.setitem(app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())
        with patch('stripe.Price.list'):
            post(env, '/admin/payments/stripe', secret_key='sk_test_new', webhook_secret='whsec_new', enabled='y')
        assert unseal(row.credentials) == {'secret_key': 'sk_test_new', 'webhook_secret': 'whsec_new'}

    def test_it_can_be_disabled(self, env, admin, key):
        row = store()
        post(env, '/admin/payments/stripe')
        assert row.enabled is False

    def test_no_secret_reaches_the_log(self, env, admin, key, caplog):
        with patch('stripe.Price.list', side_effect=stripe.AuthenticationError(f'Invalid: {SECRET}')):
            post(env, '/admin/payments/stripe', secret_key=SECRET, webhook_secret=WEBHOOK)
        assert SECRET not in caplog.text and WEBHOOK not in caplog.text

    def test_a_holder_of_another_permission_cannot_post(self, env, key):
        login(env.client, somebody(env, 'cms', 'edit cms pages'))
        assert post(env, '/admin/payments/stripe', secret_key=SECRET).status_code in (302, 403)
        assert PaymentProvider.query.count() == 0


class TestImport:
    @pytest.fixture
    def environment(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'STRIPE_SECRET_KEY', SECRET)
        monkeypatch.setitem(app.config, 'WEBHOOK_SIGNING_SECRET', WEBHOOK)

    def test_the_page_offers_it(self, env, admin, environment):
        assert b'Import from the environment' in env.client.get('/admin/payments').data

    def test_it_seals_the_environment_and_reads_the_prices(self, env, admin, key, environment):
        offer = an_offer(amount_minor=None, currency=None, offer_group='legacy')
        with patch('stripe.Price.retrieve', return_value=price()):
            post(env, '/admin/payments/stripe/import')
        assert unseal(PaymentProvider.query.one().credentials) == {'secret_key': SECRET, 'webhook_secret': WEBHOOK}
        assert (offer.amount_minor, offer.currency, offer.interval) == (500, 'usd', 'month')
        assert get_setting('payment_currency') == 'usd'

    def test_running_it_again_changes_nothing(self, env, admin, key, environment):
        row = store({'secret_key': 'sk_test_later', 'webhook_secret': 'whsec_later'})
        post(env, '/admin/payments/stripe/import')
        assert unseal(row.credentials)['secret_key'] == 'sk_test_later'

    def test_a_price_it_cannot_read_stays_blank_and_is_reported(self, env, admin, key, environment):
        offer = an_offer(amount_minor=None, currency=None)
        with patch('stripe.Price.retrieve', side_effect=stripe.APIConnectionError('down')):
            response = env.client.post('/admin/payments/stripe/import', follow_redirects=True,
                                       data={'csrf_token': csrf(env)})
        assert b'could not be read' in response.data and offer.amount_minor is None

    def test_a_second_currency_stays_blank(self, env, admin, key, environment):
        set_setting('payment_currency', 'eur')
        offer = an_offer(amount_minor=None, currency=None)
        with patch('stripe.Price.retrieve', return_value=price()):
            post(env, '/admin/payments/stripe/import')
        assert offer.amount_minor is None

    def test_with_no_key_nothing_happens(self, env, admin, environment):
        post(env, '/admin/payments/stripe/import')
        assert PaymentProvider.query.count() == 0

    def test_it_needs_csrf(self, env, admin, key, environment):
        assert env.client.post('/admin/payments/stripe/import').status_code == 400


class TestOffers:
    @pytest.fixture(autouse=True)
    def stripe_up(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'STRIPE_SECRET_KEY', 'sk_test')

    def test_the_amount_and_interval_come_from_stripe(self, env, admin):
        with patch('stripe.Price.retrieve', return_value=price(700)) as retrieve:
            response = post(env, '/admin/payments/offers/add', **OFFER)
        assert response.status_code == 302
        retrieve.assert_called_once_with('price_1', api_key='sk_test')
        offer = Offer.query.one()
        assert (offer.title, offer.kind, offer.amount_minor, offer.currency, offer.interval, offer.offer_group,
                offer.grants_donor, offer.position, offer.active) == (
            'Plan', 'subscription', 700, 'usd', 'month', 'tiers', True, 2, True)
        assert b'$7.00' in env.client.get('/admin/payments').data

    def test_a_one_off_price_makes_a_donation_with_a_length(self, env, admin):
        with patch('stripe.Price.retrieve', return_value=price(interval=None)):
            post(env, '/admin/payments/offers/add', **OFFER, donor_days='30')
        offer = Offer.query.one()
        assert (offer.kind, offer.donor_days) == ('donation', 30)

    def test_a_subscription_has_no_length(self, env, admin):
        with patch('stripe.Price.retrieve', return_value=price()):
            post(env, '/admin/payments/offers/add', **OFFER, donor_days='30')
        assert Offer.query.one().donor_days is None

    def test_a_price_stripe_does_not_know(self, env, admin):
        with patch('stripe.Price.retrieve', side_effect=stripe.InvalidRequestError('no', 'id')):
            response = post(env, '/admin/payments/offers/add', **OFFER)
        assert b'could not find that price' in response.data and Offer.query.count() == 0

    def test_another_currency_is_refused(self, env, admin):
        set_setting('payment_currency', 'eur')
        with patch('stripe.Price.retrieve', return_value=price()):
            response = post(env, '/admin/payments/offers/add', **OFFER)
        assert b'different currency' in response.data and Offer.query.count() == 0

    def test_a_second_price_in_the_sites_currency_is_fine(self, env, admin):
        set_setting('payment_currency', 'usd')
        with patch('stripe.Price.retrieve', return_value=price()):
            post(env, '/admin/payments/offers/add', **OFFER)
        assert Offer.query.count() == 1

    def test_an_edit_changes_what_it_says(self, env, admin):
        offer = an_offer()
        with patch('stripe.Price.retrieve') as retrieve:
            post(env, f'/admin/payments/offers/{offer.id}/edit', **{**OFFER, 'title': 'New'})
        assert offer.title == 'New' and not retrieve.called

    def test_an_edit_of_the_price_reads_it_again(self, env, admin):
        offer = an_offer()
        with patch('stripe.Price.retrieve', return_value=price(900)):
            post(env, f'/admin/payments/offers/{offer.id}/edit', **{**OFFER, 'price_id': 'price_2'})
        assert (offer.provider_price_id, offer.amount_minor) == ('price_2', 900)

    def test_the_edit_page_shows_the_price_and_domains(self, env, admin):
        domain = SiteDomain(domain='sister.example', scheme='https', registration='anyone')
        db.session.add(domain)
        db.session.commit()
        offer = an_offer()
        db.session.add(OfferDomain(offer_id=offer.id, site_domain_id=domain.id))
        db.session.commit()
        clear_domain_cache()
        body = env.client.get(f'/admin/payments/offers/{offer.id}/edit').get_data(as_text=True)
        clear_domain_cache()
        assert 'value="price_1"' in body and 'sister.example' in body

    def test_domains_are_saved_and_replaced(self, env, admin):
        domain = SiteDomain(domain='sister.example', scheme='https', registration='anyone')
        db.session.add(domain)
        db.session.commit()
        clear_domain_cache()
        offer = an_offer()
        token = csrf(env)   # one per test: the shared app context caches it
        env.client.post(f'/admin/payments/offers/{offer.id}/edit',
                        data={'csrf_token': token, **OFFER, 'domains': [str(domain.id)]})
        assert [row.site_domain_id for row in OfferDomain.query.filter_by(offer_id=offer.id)] == [domain.id]
        env.client.post(f'/admin/payments/offers/{offer.id}/edit', data={'csrf_token': token, **OFFER})
        clear_domain_cache()
        assert OfferDomain.query.filter_by(offer_id=offer.id).count() == 0

    def test_it_can_stop_being_offered(self, env, admin):
        offer = an_offer()
        data = {key: value for key, value in OFFER.items() if key != 'active'}
        post(env, f'/admin/payments/offers/{offer.id}/edit', **data)
        assert offer.active is False

    def test_an_unknown_offer_is_not_found(self, env, admin):
        assert env.client.get('/admin/payments/offers/987654/edit').status_code == 404

    def test_deleting_one_nobody_paid_for(self, env, admin):
        offer = an_offer()
        post(env, f'/admin/payments/offers/{offer.id}/delete')
        assert Offer.query.count() == 0

    def test_one_somebody_paid_for_cannot_be_deleted(self, env, admin):
        offer = an_offer()
        db.session.add(Payment(offer_id=offer.id, provider_kind='stripe', provider_reference='in_1', amount_minor=500,
                               currency='usd', status='paid'))
        db.session.commit()
        response = env.client.post(f'/admin/payments/offers/{offer.id}/delete', follow_redirects=True,
                                   data={'csrf_token': csrf(env)})
        assert b'cannot be deleted' in response.data and Offer.query.count() == 1

    def test_one_somebody_is_subscribed_to_cannot_be_deleted(self, env, admin):
        offer = an_offer()
        db.session.add(MemberSubscription(user_id=admin.id, offer_id=offer.id, provider_kind='stripe',
                                          provider_reference='sub_1', status='active'))
        db.session.commit()
        post(env, f'/admin/payments/offers/{offer.id}/delete')
        assert Offer.query.count() == 1

    def test_a_change_clears_the_menus_cached_answer(self, env, admin):
        offer = an_offer()
        with patch('app.admin.payments_routes.offers_changed') as changed:
            post(env, f'/admin/payments/offers/{offer.id}/delete')
        assert changed.called


class TestShortcodes:
    @pytest.fixture(autouse=True)
    def stripe_up(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'STRIPE_SECRET_KEY', 'sk_test')

    def test_every_offer_shows_the_shortcode_that_draws_its_button(self, env, admin):
        offer = an_offer()
        assert f'[checkout {offer.id}]'.encode() in env.client.get('/admin/payments').data


class TestSettings:
    def test_retention_is_saved_and_cleared(self, env, admin):
        token = csrf(env)   # one per test: the shared app context caches it
        env.client.post('/admin/payments/settings', data={'csrf_token': token, 'retention_days': '400'})
        assert get_setting('payment_retention_days') == 400
        env.client.post('/admin/payments/settings', data={'csrf_token': token})
        assert get_setting('payment_retention_days') is None

    def test_a_nonsense_retention_is_refused(self, env, admin):
        response = post(env, '/admin/payments/settings', retention_days='0')
        assert response.status_code == 200 and get_setting('payment_retention_days') is None


class TestTheLedger:
    @pytest.fixture
    def rows(self, env, admin):
        offer = an_offer()
        for reference, status, user_id in (('in_1', 'paid', admin.id), ('in_2', 'refunded', None)):
            db.session.add(Payment(user_id=user_id, offer_id=offer.id if status == 'paid' else None, provider_kind='stripe',
                                   provider_reference=reference, amount_minor=500, currency='usd', status=status))
        db.session.commit()
        return offer

    def test_it_lists_payments_with_their_accounts(self, env, rows, admin):
        body = env.client.get('/admin/payments/ledger').get_data(as_text=True)
        assert 'in_1' in body and 'in_2' in body and '$5.00' in body and f'>{admin.user_name}</a>' in body

    def test_it_filters_by_status(self, env, rows):
        body = env.client.get('/admin/payments/ledger?status=refunded').get_data(as_text=True)
        assert 'in_2' in body and 'in_1' not in body

    def test_it_filters_by_offer(self, env, rows):
        body = env.client.get(f'/admin/payments/ledger?offer_id={rows.id}').get_data(as_text=True)
        assert 'in_1' in body and 'in_2' not in body

    @pytest.mark.parametrize('query', ['status=nonsense', 'offer_id=987654', 'offer_id=99999999999', 'offer_id=x', 'page=99'],
                             ids=['status', 'offer', 'huge_offer', 'text_offer', 'past_the_end'])
    def test_odd_filters_are_not_a_crash(self, env, rows, query):
        assert env.client.get(f'/admin/payments/ledger?{query}').status_code == 200

    def test_it_pages(self, env, admin):
        db.session.add_all(Payment(provider_kind='stripe', provider_reference=f'in_{number}', amount_minor=1, currency='usd',
                                   status='paid') for number in range(51))
        db.session.commit()
        first = env.client.get('/admin/payments/ledger').get_data(as_text=True)
        assert 'page=2' in first
        assert 'page=1' in env.client.get('/admin/payments/ledger?page=2').get_data(as_text=True)

    def test_it_asks_for_a_retention_period_until_there_is_one(self, env, rows):
        assert b'No retention period is set' in env.client.get('/admin/payments/ledger').data
        set_setting('payment_retention_days', 400)
        assert b'No retention period is set' not in env.client.get('/admin/payments/ledger').data


class TestLinks:
    LINK = {'title': 'Our Ko-fi', 'url': 'https://ko-fi.example/us', 'position': '1', 'active': 'y'}

    def test_a_link_needs_no_provider_and_no_key(self, env, admin):
        assert post(env, '/admin/payments/links/add', **self.LINK).status_code == 302
        link = Offer.query.one()
        assert (link.provider_kind, link.kind, link.title, link.url, link.position, link.active) == (
            'link', 'link', 'Our Ko-fi', 'https://ko-fi.example/us', 1, True)
        assert link.amount_minor is None and link.grants_donor is False

    def test_it_is_listed_with_its_address(self, env, admin):
        post(env, '/admin/payments/links/add', **self.LINK)
        body = env.client.get('/admin/payments').get_data(as_text=True)
        assert 'Our Ko-fi' in body and 'https://ko-fi.example/us' in body and '/admin/payments/offers/' in body

    @pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,x', 'ko-fi.example', 'https://', 'ftp://x.example'],
                             ids=['javascript', 'data', 'no_scheme', 'no_host', 'ftp'])
    def test_only_a_web_address_is_accepted(self, env, admin, url):
        response = post(env, '/admin/payments/links/add', **{**self.LINK, 'url': url})
        assert response.status_code == 200 and Offer.query.count() == 0

    def test_editing_a_link_uses_the_link_form_and_asks_stripe_nothing(self, env, admin):
        link = Offer(provider_kind='link', kind='link', title='Old', offer_group='links', url='https://old.example/')
        db.session.add(link)
        db.session.commit()
        token = csrf(env)   # one per test: the shared app context caches it
        body = env.client.get(f'/admin/payments/offers/{link.id}/edit').get_data(as_text=True)
        assert 'value="https://old.example/"' in body and 'price_id' not in body
        with patch('stripe.Price.retrieve') as retrieve:
            env.client.post(f'/admin/payments/offers/{link.id}/edit', data={'csrf_token': token, **self.LINK})
        assert (link.title, link.url) == ('Our Ko-fi', 'https://ko-fi.example/us') and not retrieve.called

    def test_a_link_can_be_limited_to_a_domain(self, env, admin):
        domain = SiteDomain(domain='sister.example', scheme='https', registration='anyone')
        db.session.add(domain)
        db.session.commit()
        clear_domain_cache()
        env.client.post('/admin/payments/links/add', data={'csrf_token': csrf(env), **self.LINK, 'domains': [str(domain.id)]})
        clear_domain_cache()
        assert OfferDomain.query.one().site_domain_id == domain.id

    def test_an_invalid_edit_stays_on_the_form(self, env, admin):
        link = Offer(provider_kind='link', kind='link', title='Old', offer_group='links', url='https://old.example/')
        db.session.add(link)
        db.session.commit()
        response = post(env, f'/admin/payments/offers/{link.id}/edit', **{**self.LINK, 'url': 'nonsense'})
        assert response.status_code == 200 and link.url == 'https://old.example/'

    def test_a_link_can_be_deleted(self, env, admin):
        link = Offer(provider_kind='link', kind='link', title='Old', offer_group='links', url='https://old.example/')
        db.session.add(link)
        db.session.commit()
        post(env, f'/admin/payments/offers/{link.id}/delete')
        assert Offer.query.count() == 0
