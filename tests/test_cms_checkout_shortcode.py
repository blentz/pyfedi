"""[checkout N] in a CMS page: offer N's own button, drawn when the page is shown, so it carries the visitor's CSRF
token and knows who they are. A link in markdown can't start a checkout: checkout is a POST."""
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from flask import current_app, g

from app import db
from app.models import CmsPage, MemberSubscription, Offer, OfferDomain, PaymentProvider, Site, SiteDomain
from app.payments.credentials import seal
from app.payments.offers import offers_changed
from app.payments.shortcodes import checkout_shortcodes
from app.utils import markdown_to_html
from tests.factories import make_user


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    tier = Offer(provider_kind='stripe', kind='subscription', title='Furcas', offer_group='tiers',
                 provider_price_id='price_furcas', amount_minor=666, currency='usd', interval='month')
    gift = Offer(provider_kind='stripe', kind='donation', title='Gift', offer_group='gifts',
                 provider_price_id='price_gift', amount_minor=500, currency='usd')
    db.session.add_all([tier, gift])
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    yield SimpleNamespace(app=app, client=app.test_client(), tier=tier, gift=gift, member=member)
    offers_changed()


def page(body, url='/subscriptions'):
    db.session.add(CmsPage(url=url, title='Subscriptions', body=body, body_html=markdown_to_html(body)))
    db.session.commit()


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def shown(env, body):
    page(body)
    return env.client.get('/subscriptions').get_data(as_text=True)


class TestTheButton:
    def test_a_member_gets_a_form_that_starts_the_checkout_with_their_token(self, env):
        login(env.client, env.member)
        body = shown(env, f'Pick one: [checkout {env.tier.id}]')
        assert f'action="/donations/checkout/{env.tier.id}"' in body
        assert 'name="csrf_token"' in body
        assert '>Subscribe</button>' in body
        assert '[checkout' not in body

    def test_the_form_really_starts_the_checkout(self, env, monkeypatch):
        login(env.client, env.member)
        body = shown(env, f'[checkout {env.tier.id}]')
        token = body.split('name="csrf_token" value="')[1].split('"')[0]
        monkeypatch.setattr('app.payments.adapters.stripe_adapter.StripeAdapter.checkout',
                            lambda self, offer, user, success, failure: 'https://checkout.example/s')
        answer = env.client.post(f'/donations/checkout/{env.tier.id}', data={'csrf_token': token})
        assert answer.status_code == 302 and answer.headers['Location'] == 'https://checkout.example/s'

    def test_a_label_after_the_number_is_the_buttons_words(self, env):
        login(env.client, env.member)
        body = shown(env, f'[checkout {env.tier.id} Rule as a Knight]')
        assert '>Rule as a Knight</button>' in body

    def test_a_label_is_text_not_markup(self, env):
        login(env.client, env.member)
        body = shown(env, f'[checkout {env.tier.id} a &amp; b]')
        assert '>a &amp; b</button>' in body

    def test_a_one_off_says_so(self, env):
        login(env.client, env.member)
        assert '>Donate once</button>' in shown(env, f'[checkout {env.gift.id}]')

    def test_buttons_work_in_a_table(self, env):
        login(env.client, env.member)
        body = shown(env, f'| Monthly | Yearly |\n|---|---|\n| [checkout {env.tier.id}] | [checkout {env.gift.id}] |\n')
        assert f'action="/donations/checkout/{env.tier.id}"' in body
        assert f'action="/donations/checkout/{env.gift.id}"' in body

    def test_a_guest_is_asked_to_log_in_and_comes_back_here(self, env):
        body = shown(env, f'[checkout {env.tier.id}]')
        assert 'Log in to subscribe' in body
        assert 'next=%2Fsubscriptions' in body or 'next=/subscriptions' in body
        assert 'action="/donations/checkout' not in body

    def test_a_guest_may_give_once_with_no_token(self, env):
        body = shown(env, f'[checkout {env.gift.id}]')
        assert f'action="/donations/checkout/{env.gift.id}"' in body
        assert 'name="csrf_token"' not in body

    def test_a_link_out_is_a_plain_link(self, env):
        link = Offer(provider_kind='link', kind='link', title='Ko-fi', offer_group='links', url='https://ko-fi.example/us')
        db.session.add(link)
        db.session.commit()
        body = shown(env, f'[checkout {link.id}]')
        assert 'href="https://ko-fi.example/us"' in body and 'rel="noopener noreferrer nofollow"' in body

    def test_a_prepaid_plan_says_what_one_payment_buys(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', Fernet.generate_key().decode())
        db.session.add(PaymentProvider(kind='btcpay', credentials=seal({'api_key': 'k', 'webhook_secret': 's'}),
                                       settings={'url': 'https://pay.example', 'store_id': 'store1'}))
        plan = Offer(provider_kind='btcpay', kind='subscription', title='Prepaid', offer_group='tiers',
                     amount_minor=666, currency='usd', interval='month')
        db.session.add(plan)
        db.session.commit()
        offers_changed()
        login(env.client, env.member)
        assert '>Pay for one month</button>' in shown(env, f'[checkout {plan.id}]')


class TestWhatIsNotOffered:
    def test_an_unknown_offer_is_not_available(self, env):
        body = shown(env, '[checkout 999999]')
        assert 'Not available' in body and 'action="/donations' not in body

    def test_an_inactive_offer_is_not_available(self, env):
        env.tier.active = False
        db.session.commit()
        login(env.client, env.member)
        body = shown(env, f'[checkout {env.tier.id}]')
        assert 'Not available' in body and 'action="/donations' not in body

    def test_a_provider_that_is_not_set_up_offers_nothing(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', '')
        login(env.client, env.member)
        assert 'Not available' in shown(env, f'[checkout {env.tier.id}]')

    def test_an_offer_on_another_domain_is_not_available_here(self, env):
        elsewhere = SiteDomain(domain='elsewhere.example')
        db.session.add(elsewhere)
        db.session.flush()
        db.session.add(OfferDomain(offer_id=env.tier.id, site_domain_id=elsewhere.id))
        db.session.commit()
        login(env.client, env.member)
        assert 'Not available' in shown(env, f'[checkout {env.tier.id}]')


class TestAPlanAlreadyHeld:
    def test_it_leads_to_where_it_is_managed_instead_of_a_second_one(self, env):
        db.session.add(MemberSubscription(user_id=env.member.id, offer_id=env.tier.id, provider_kind='stripe',
                                          provider_reference='sub_1', status='active'))
        db.session.commit()
        login(env.client, env.member)
        body = shown(env, f'[checkout {env.tier.id}]')
        assert 'Your current plan' in body and 'href="/donations"' in body
        assert f'action="/donations/checkout/{env.tier.id}"' not in body

    def test_a_cancelled_one_can_be_chosen_again(self, env):
        db.session.add(MemberSubscription(user_id=env.member.id, offer_id=env.tier.id, provider_kind='stripe',
                                          provider_reference='sub_1', status='cancelled'))
        db.session.commit()
        login(env.client, env.member)
        assert f'action="/donations/checkout/{env.tier.id}"' in shown(env, f'[checkout {env.tier.id}]')


class TestTheText:
    def test_code_shows_the_shortcode_itself(self, env):
        body = shown(env, f'Write `[checkout {env.tier.id}]` for a button.\n\n    [checkout {env.tier.id}]\n')
        assert body.count(f'[checkout {env.tier.id}]') == 2
        assert 'action="/donations/checkout' not in body

    def test_a_page_with_no_shortcode_is_unchanged(self, env):
        with env.app.test_request_context('/'):
            assert str(checkout_shortcodes('<p>Hello [world]</p>')) == '<p>Hello [world]</p>'

    def test_a_page_without_shortcodes_asks_nothing_of_the_database(self, env, monkeypatch):
        monkeypatch.setattr('app.payments.shortcodes.offers_here', lambda: pytest.fail('offers looked up'))
        assert 'Just words' in shown(env, 'Just words')

    def test_the_stored_page_keeps_the_shortcode(self, env):
        page(f'[checkout {env.tier.id}]')
        assert f'[checkout {env.tier.id}]' in CmsPage.query.filter_by(url='/subscriptions').one().body_html

    def test_a_page_with_a_sidebar_draws_buttons_too(self, env):
        login(env.client, env.member)
        db.session.add(CmsPage(url='/subscriptions', title='Subscriptions', body='x', show_sidebar=True,
                               body_html=markdown_to_html(f'[checkout {env.tier.id}]')))
        db.session.commit()
        assert f'action="/donations/checkout/{env.tier.id}"' in env.client.get('/subscriptions').get_data(as_text=True)

    def test_the_about_page_draws_buttons_too(self, env):
        login(env.client, env.member)
        page(f'[checkout {env.tier.id}]', url='/about')
        assert f'action="/donations/checkout/{env.tier.id}"' in env.client.get('/about').get_data(as_text=True)


class TestOnlyTextIsRewritten:
    """A shortcode in an attribute is left as written: drawing a button there would close the attribute, and what the
    page's author wrote after it would become markup the sanitizer never saw."""

    def test_in_a_link_address(self, env):
        with env.app.test_request_context('/'):
            html = str(checkout_shortcodes(f'<p><a href="https://x.example/[checkout {env.tier.id}]">x</a></p>'))
        assert html == f'<p><a href="https://x.example/[checkout {env.tier.id}]">x</a></p>'

    def test_in_an_attribute_with_words_after_it(self, env):
        alt = f'[checkout {env.tier.id} x] onerror=alert(1) y'
        with env.app.test_request_context('/'):
            html = str(checkout_shortcodes(f'<p><img alt="{alt}" src="/a.png"/></p>'))
        soup = BeautifulSoup(html, 'html.parser')
        assert [img.attrs for img in soup.find_all('img')] == [{'alt': alt, 'src': '/a.png'}]
        assert soup.find('form') is None

    def test_markup_in_the_text_around_it_stays_text(self, env):
        login(env.client, env.member)
        body = shown(env, f'&lt;script&gt;x&lt;/script&gt; [checkout {env.tier.id}]')
        assert '<script>x' not in body and '&lt;script&gt;x' in body
        assert f'action="/donations/checkout/{env.tier.id}"' in body

    def test_a_label_with_markup_characters_is_text(self, env):
        login(env.client, env.member)
        body = shown(env, f'[checkout {env.tier.id} a &lt;b&gt; &amp; c]')
        form = BeautifulSoup(body, 'html.parser').find('form', action=f'/donations/checkout/{env.tier.id}')
        assert form.button.get_text() == 'a <b> & c' and form.button.find('b') is None

    def test_in_a_comment(self, env):
        with env.app.test_request_context('/'):
            html = str(checkout_shortcodes(f'<p>x</p><!-- [checkout {env.tier.id}] -->'))
        assert html == f'<p>x</p><!-- [checkout {env.tier.id}] -->'
