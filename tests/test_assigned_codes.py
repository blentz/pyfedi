"""Codes an admin made for a member to hand out (owner 2026-11-16), as the member's referral page lists them: each with
what it gives, its link and QR code, and its uses. Not referrals: an ordinary gift or discount code."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Offer, PromoCode, PromoCodeUse, Site, SiteDomain, utcnow
from app.referral import GENERATORS_SETTING, REFERRAL_MODE, generate_code
from app.storefront import clear_domain_cache
from app.utils import set_setting
from tests.factories import make_user

PAGE = '/user/settings/referrals'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.registration_mode = 'Open'
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    knight = Offer(provider_kind='stripe', kind='subscription', title='Furcas', offer_group='tiers',
                   provider_price_id='price_k', amount_minor=666, currency='usd', interval='month', position=1)
    db.session.add(knight)
    db.session.flush()
    home = SiteDomain(domain=current_app.config['SERVER_NAME'], scheme='https', federates=True, is_default=True,
                      registration='paid', registration_offer_id=knight.id, lapse_grace_days=3,
                      paid_since=utcnow() - timedelta(days=10))
    db.session.add(home)
    db.session.commit()
    clear_domain_cache()
    yield SimpleNamespace(app=app, client=app.test_client(), site=site, home=home, knight=knight, baseline=api_baseline,
                          base=f"https://{current_app.config['SERVER_NAME']}")
    clear_domain_cache()


def member(env, name='holder'):
    user = make_user(env.baseline.instance_local, name, local=True)
    user.verified = True
    db.session.commit()
    return user


def login(env, user):
    with env.client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def code_for(env, user, text='hand-out-one', **values):
    row = PromoCode(code=text, assigned_to=user.id if user else None,
                    **({'kind': 'gift', 'offer_id': env.knight.id, 'periods': 1} | values))
    db.session.add(row)
    db.session.commit()
    return row


def page(env):
    response = env.client.get(PAGE, base_url=env.base)
    assert response.status_code == 200
    return BeautifulSoup(response.get_data(as_text=True), 'html.parser')


def block(env, row):
    return page(env).find(id=f'assigned-{row.id}')


class TestWhoSeesThePage:
    def test_a_member_holding_a_code_sees_it_without_referral_codes_on(self, env):
        holder = member(env)
        row = code_for(env, holder)
        login(env, holder)
        assert 'hand-out-one' in block(env, row).get_text()

    def test_the_menu_leads_there(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'WTF_CSRF_ENABLED', True)   # the settings form renders its token
        holder = member(env)
        code_for(env, holder)
        login(env, holder)
        assert PAGE in env.client.get('/user/settings', base_url=env.base).get_data(as_text=True)

    def test_a_member_with_neither_gets_no_page(self, env):
        login(env, member(env))
        assert env.client.get(PAGE, base_url=env.base).status_code == 404

    def test_somebody_elses_codes_are_not_listed(self, env):
        holder, other = member(env), member(env, 'other')
        code_for(env, holder)
        code_for(env, other, text='not-yours-at-all')
        login(env, holder)
        assert 'not-yours-at-all' not in str(page(env))

    def test_a_code_for_nobody_is_listed_for_nobody(self, env):
        holder = member(env)
        code_for(env, holder)
        code_for(env, None, text='nobodys-code-here')
        login(env, holder)
        assert 'nobodys-code-here' not in str(page(env))


class TestWhatEachSays:
    def test_a_gift_says_its_tier_and_length(self, env):
        holder = member(env)
        row = code_for(env, holder)
        login(env, holder)
        assert '1 month of Furcas, free' in block(env, row).get_text()

    def test_a_discount_says_what_it_takes_off(self, env):
        holder = member(env)
        row = code_for(env, holder, kind='percent', percent=20, offer_id=None, periods=None)
        login(env, holder)
        assert '20% off their first payment for any plan' in block(env, row).get_text()

    def test_every_one_is_listed(self, env):
        holder = member(env)
        first = code_for(env, holder)
        second = code_for(env, holder, text='hand-out-two', kind='amount', amount_minor=200, offer_id=env.knight.id,
                          periods=None)
        login(env, holder)
        listed = page(env)
        assert listed.find(id=f'assigned-{first.id}') and listed.find(id=f'assigned-{second.id}')
        assert '$2.00 off their first payment for Furcas' in listed.find(id=f'assigned-{second.id}').get_text()

    def test_its_uses_out_of_its_limit(self, env):
        holder, used = member(env), member(env, 'used')
        row = code_for(env, holder, max_uses=5)
        db.session.add(PromoCodeUse(promo_code_id=row.id, user_id=used.id))
        db.session.commit()
        login(env, holder)
        assert 'Used: 1 / 5' in block(env, row).get_text()

    @pytest.mark.parametrize('values', [{'active': False}, {'expires_at': utcnow() - timedelta(days=1)}],
                             ids=['off', 'expired'])
    def test_one_that_cannot_be_used_says_so(self, env, values):
        holder = member(env)
        row = code_for(env, holder, **values)
        login(env, holder)
        assert 'This code cannot be used at the moment.' in block(env, row).get_text()


class TestItsLink:
    def test_it_registers_with_the_code_on_the_default_domain(self, env):
        holder = member(env)
        row = code_for(env, holder)
        login(env, holder)
        link = block(env, row).find('input', id=f'assigned-link-{row.id}')['value']
        assert link == f'{env.base}/auth/register?code=hand-out-one'

    def test_it_has_a_qr_code_and_a_png(self, env):
        holder = member(env)
        row = code_for(env, holder)
        login(env, holder)
        assert block(env, row).find('svg') is not None
        response = env.client.get(f'{PAGE}/codes/{row.id}/qr.png', base_url=env.base)
        assert response.status_code == 200 and response.headers['Content-Type'] == 'image/png'

    def test_nobody_else_gets_its_png(self, env):
        holder, other = member(env), member(env, 'other')
        row = code_for(env, holder)
        code_for(env, other, text='other-code-one')
        login(env, other)
        assert env.client.get(f'{PAGE}/codes/{row.id}/qr.png', base_url=env.base).status_code == 404

    def test_where_joining_is_free_there_is_no_link_to_join_with(self, env):
        env.home.registration = 'anyone'
        db.session.commit()
        clear_domain_cache()
        holder = member(env)
        row = code_for(env, holder)
        login(env, holder)
        shown = block(env, row)
        assert shown.find('svg') is None and shown.find('input', id=f'assigned-link-{row.id}') is None
        assert 'Settings' in shown.get_text()   # where a member enters it instead
        assert env.client.get(f'{PAGE}/codes/{row.id}/qr.png', base_url=env.base).status_code == 404


class TestBesideTheirOwnReferralCode:
    @pytest.fixture
    def referrer(self, env):
        env.site.registration_mode = REFERRAL_MODE
        env.site.application_question = 'Your referral code'
        db.session.commit()
        set_setting(GENERATORS_SETTING, 'all')
        user = member(env, 'referrer')
        generate_code(user)
        db.session.commit()
        login(env, user)
        return user

    def test_both_are_shown(self, env, referrer):
        row = code_for(env, referrer)
        listed = page(env)
        assert referrer.referral_code in str(listed) and listed.find(id=f'assigned-{row.id}') is not None

    def test_regenerating_keeps_the_codes_an_admin_made(self, env, referrer):
        row = code_for(env, referrer)
        with env.app.test_request_context():
            g.pop('csrf_token', None)
            token = generate_csrf()
            raw = flask_session['csrf_token']
        with env.client.session_transaction() as session:
            session['csrf_token'] = raw
        with patch.dict(current_app.config, {'WTF_CSRF_ENABLED': True}):
            env.client.post(f'{PAGE}/generate', data={'csrf_token': token}, base_url=env.base)
        kept = db.session.get(PromoCode, row.id)
        assert (kept.code, kept.assigned_to, kept.active) == ('hand-out-one', referrer.id, True)
