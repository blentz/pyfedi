"""Admin -> Payments -> Codes (paid membership plan phase 2, G6): the default gift, gift codes, members' referral codes."""
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Offer, PromoCode, PromoCodeUse, Site
from app.payments.gifts import DEFAULT_GIFT_DAYS_SETTING, DEFAULT_GIFT_OFFER_SETTING
from app.referral import GENERATORS_SETTING, generate_code
from app.utils import get_setting, set_setting
from tests.factories import grant_permission, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    knight = Offer(provider_kind='btcpay', kind='subscription', title='Furcas', offer_group='tiers',
                   amount_minor=666, currency='usd', interval='month')
    gift = Offer(provider_kind='btcpay', kind='donation', title='Tip', offer_group='tips', amount_minor=300, currency='usd')
    db.session.add_all([knight, gift])
    db.session.commit()
    return SimpleNamespace(app=app, client=app.test_client(), baseline=api_baseline, knight=knight, tip=gift)


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


@pytest.fixture
def admin(env):
    user = somebody(env, 'treasurer', 'manage monetization')
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


class TestThePage:
    def test_it_is_monetizations(self, env):
        login(env.client, somebody(env, 'nobody'))
        assert env.client.get('/admin/payments/codes').status_code in (302, 403)

    def test_the_payments_page_leads_to_it(self, env, admin):
        assert b'/admin/payments/codes' in env.client.get('/admin/payments').data

    def test_it_lists_every_code_with_its_uses(self, env, admin):
        code = PromoCode(code='gift-month', kind='gift', offer_id=env.knight.id, days=30, max_uses=5)
        db.session.add(code)
        db.session.commit()
        db.session.add(PromoCodeUse(promo_code_id=code.id, user_id=admin.id))
        db.session.commit()
        page = BeautifulSoup(env.client.get('/admin/payments/codes').get_data(as_text=True), 'html.parser')
        row = page.find('tr', id=f'code-{code.id}')
        assert 'gift-month' in row.get_text() and '1 / 5' in row.get_text() and 'Furcas' in row.get_text()

    def test_members_referral_codes_are_listed_with_whose(self, env, admin):
        set_setting(GENERATORS_SETTING, 'all')
        member = somebody(env, 'referrer')
        generate_code(member)
        db.session.commit()
        assert 'referrer' in env.client.get('/admin/payments/codes').get_data(as_text=True)


class TestTheDefaultGift:
    def test_it_is_saved(self, env, admin):
        assert post(env, '/admin/payments/codes/default', default_offer_id=env.knight.id, default_days=30).status_code == 302
        assert (get_setting(DEFAULT_GIFT_OFFER_SETTING), get_setting(DEFAULT_GIFT_DAYS_SETTING)) == (env.knight.id, 30)

    def test_none_clears_it(self, env, admin):
        set_setting(DEFAULT_GIFT_OFFER_SETTING, env.knight.id)
        post(env, '/admin/payments/codes/default', default_offer_id=0, default_days=30)
        assert not get_setting(DEFAULT_GIFT_OFFER_SETTING)

    def test_a_one_off_is_no_gift(self, env, admin):
        post(env, '/admin/payments/codes/default', default_offer_id=env.tip.id, default_days=30)
        assert not get_setting(DEFAULT_GIFT_OFFER_SETTING)


class TestMakingACode:
    def test_one_is_generated_when_none_is_typed(self, env, admin):
        assert post(env, '/admin/payments/codes/add', offer_id=env.knight.id, days=365, code='').status_code == 302
        code = PromoCode.query.one()
        assert (code.kind, code.offer_id, code.days, code.owner_id) == ('gift', env.knight.id, 365, None)
        assert len(code.code) >= 10 and code.code == code.code.lower()

    def test_a_typed_one_is_kept_lower_case(self, env, admin):
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, days=30, code='  Winter-Gift-2026 ',
             max_uses=50, expires_at='2027-01-31')
        code = PromoCode.query.one()
        assert (code.code, code.max_uses, code.expires_at.date().isoformat()) == ('winter-gift-2026', 50, '2027-01-31')

    def test_a_short_one_is_refused_as_guessable(self, env, admin):
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, days=30, code='free')
        assert PromoCode.query.count() == 0

    def test_a_code_already_taken_is_refused(self, env, admin):
        db.session.add(PromoCode(code='winter-gift-2026', kind='gift', offer_id=env.knight.id, days=30))
        db.session.commit()
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, days=30, code='WINTER-GIFT-2026')
        assert PromoCode.query.count() == 1

    @pytest.mark.parametrize('offer, days', [('none', 30), ('tier', 0)], ids=['no_tier', 'no_days'])
    def test_it_needs_a_tier_and_days(self, env, admin, offer, days):
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id if offer == 'tier' else 0, days=days, code='')
        assert PromoCode.query.count() == 0


class TestChangingACode:
    @pytest.fixture
    def code(self, env):
        row = PromoCode(code='gift-month', kind='gift', offer_id=env.knight.id, days=30)
        db.session.add(row)
        db.session.commit()
        return row

    def test_its_gift_and_limits_change(self, env, admin, code):
        post(env, f'/admin/payments/codes/{code.id}', offer_id=env.knight.id, days=90, max_uses=3, active='y')
        row = db.session.get(PromoCode, code.id)
        assert (row.days, row.max_uses, row.active) == (90, 3, True)

    def test_it_can_be_turned_off(self, env, admin, code):
        post(env, f'/admin/payments/codes/{code.id}', offer_id=env.knight.id, days=30)
        assert db.session.get(PromoCode, code.id).active is False

    def test_a_members_referral_code_gets_its_own_gift(self, env, admin):
        set_setting(GENERATORS_SETTING, 'all')
        member = somebody(env, 'referrer')
        generate_code(member)
        db.session.commit()
        row = PromoCode.query.filter_by(owner_id=member.id).one()
        post(env, f'/admin/payments/codes/{row.id}', offer_id=env.knight.id, days=365, active='y')
        assert db.session.get(PromoCode, row.id).days == 365

    def test_an_unknown_code_is_404(self, env, admin):
        assert post(env, '/admin/payments/codes/99999', offer_id=env.knight.id, days=30).status_code == 404
