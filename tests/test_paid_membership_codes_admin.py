"""Admin -> Payments -> Codes (paid membership plan phase 2, G6): the default gift, gift codes, members' referral codes."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Offer, PromoCode, PromoCodeUse, Site
from app.payments.gifts import REFERRAL_BENEFIT_SETTING
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
        g.pop('csrf_token', None)   # flask_wtf keeps the token on g, which outlives a request made before this one
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
        code = PromoCode(code='gift-month', kind='gift', offer_id=env.knight.id, periods=1, max_uses=5)
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


class TestTheDefaultBenefit:
    """Saving it is tested in tests/test_referral_benefits.py (plan 2026-11-16 R3); a one-off is no tier to give."""

    def test_a_one_off_is_no_gift(self, env, admin):
        post(env, '/admin/payments/codes/default', benefit=f'gift:{env.tip.id}')
        assert not get_setting(REFERRAL_BENEFIT_SETTING)


class TestMakingACode:
    def test_one_is_generated_when_none_is_typed(self, env, admin):
        assert post(env, '/admin/payments/codes/add', offer_id=env.knight.id, periods=12, code='').status_code == 302
        code = PromoCode.query.one()
        assert (code.kind, code.offer_id, code.periods, code.owner_id) == ('gift', env.knight.id, 12, None)
        assert len(code.code) >= 10 and code.code == code.code.lower()

    def test_a_typed_one_is_kept_lower_case(self, env, admin):
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, periods=1, code='  Winter-Gift-2026 ',
             max_uses=50, expires_at='2027-01-31')
        code = PromoCode.query.one()
        assert (code.code, code.max_uses, code.expires_at.date().isoformat()) == ('winter-gift-2026', 50, '2027-01-31')

    def test_a_short_one_is_refused_as_guessable(self, env, admin):
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, periods=1, code='free')
        assert PromoCode.query.count() == 0

    def test_a_code_already_taken_is_refused(self, env, admin):
        db.session.add(PromoCode(code='winter-gift-2026', kind='gift', offer_id=env.knight.id, periods=1))
        db.session.commit()
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, periods=1, code='WINTER-GIFT-2026')
        assert PromoCode.query.count() == 1

    @pytest.mark.parametrize('offer, periods', [('none', 1), ('tier', 0)], ids=['no_tier', 'no_units'])
    def test_it_needs_a_tier_and_a_number_of_units(self, env, admin, offer, periods):
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id if offer == 'tier' else 0, periods=periods, code='')
        assert PromoCode.query.count() == 0


class TestChangingACode:
    @pytest.fixture
    def code(self, env):
        row = PromoCode(code='gift-month', kind='gift', offer_id=env.knight.id, periods=1)
        db.session.add(row)
        db.session.commit()
        return row

    def test_its_gift_and_limits_change(self, env, admin, code):
        post(env, f'/admin/payments/codes/{code.id}', offer_id=env.knight.id, periods=3, max_uses=3, active='y')
        row = db.session.get(PromoCode, code.id)
        assert (row.periods, row.max_uses, row.active) == (3, 3, True)

    def test_it_can_be_turned_off(self, env, admin, code):
        post(env, f'/admin/payments/codes/{code.id}', offer_id=env.knight.id, periods=1)
        assert db.session.get(PromoCode, code.id).active is False

    def test_a_members_referral_code_gets_its_own_gift(self, env, admin):
        set_setting(GENERATORS_SETTING, 'all')
        member = somebody(env, 'referrer')
        generate_code(member)
        db.session.commit()
        row = PromoCode.query.filter_by(owner_id=member.id).one()
        # Its choices are what the owner's domain has on offer: a tier whose provider is usable (plan 2026-11-16 R2)
        with patch('app.payments.adapters.btcpay_adapter.BtcpayAdapter.available', return_value=True):
            post(env, f'/admin/payments/codes/{row.id}', benefit=f'gift:{env.knight.id}', periods=12, active='y')
        assert db.session.get(PromoCode, row.id).periods == 12

    def test_an_unknown_code_is_404(self, env, admin):
        assert post(env, '/admin/payments/codes/99999', offer_id=env.knight.id, periods=1).status_code == 404


class TestTheEditPage:
    def test_it_shows_the_code_with_its_last_day(self, env, admin):
        code = PromoCode(code='gift-month', kind='gift', offer_id=env.knight.id, periods=1,
                         expires_at=datetime(2027, 1, 31, 23, 59, 59))
        db.session.add(code)
        db.session.commit()
        page = env.client.get(f'/admin/payments/codes/{code.id}').get_data(as_text=True)
        assert 'gift-month' in page and 'value="2027-01-31"' in page

    def test_it_names_whose_referral_code_it_is(self, env, admin):
        set_setting(GENERATORS_SETTING, 'all')
        member = somebody(env, 'referrer')
        generate_code(member)
        db.session.commit()
        row = PromoCode.query.filter_by(owner_id=member.id).one()
        assert "referrer's referral code" in env.client.get(f'/admin/payments/codes/{row.id}').get_data(as_text=True)


class TestDiscountCodes:
    """Plan phase 3 D7: percent or an amount off a first payment, for one offer or any."""

    def test_percent_off_any_offer(self, env, admin):
        post(env, '/admin/payments/codes/add', kind='percent', percent=20, offer_id=0, code='spring-twenty')
        code = PromoCode.query.one()
        assert (code.kind, code.percent, code.offer_id, code.periods) == ('percent', 20, None, None)

    def test_an_amount_off_one_offer(self, env, admin):
        set_setting('payment_currency', 'usd')
        post(env, '/admin/payments/codes/add', kind='amount', amount='1.66', offer_id=env.knight.id, code='one-sixty-six')
        code = PromoCode.query.one()
        assert (code.kind, code.amount_minor, code.offer_id) == ('amount', 166, env.knight.id)

    @pytest.mark.parametrize('values', [{'kind': 'percent', 'percent': 0}, {'kind': 'percent', 'percent': 100},
                                        {'kind': 'percent'}, {'kind': 'amount'}, {'kind': 'amount', 'amount': '0'},
                                        {'kind': 'gift', 'offer_id': 0, 'periods': 1}],
                             ids=['zero', 'all', 'no_percent', 'no_amount', 'zero_amount', 'gift_without_tier'])
    def test_what_it_needs(self, env, admin, values):
        post(env, '/admin/payments/codes/add', **{'offer_id': 0, 'code': 'some-long-code', **values})
        assert PromoCode.query.count() == 0

    def test_the_list_says_what_it_takes_off(self, env, admin):
        db.session.add(PromoCode(code='spring-twenty', kind='percent', percent=20))
        db.session.commit()
        assert '20% off a first payment' in env.client.get('/admin/payments/codes').get_data(as_text=True)

    def test_editing_one_keeps_its_kind(self, env, admin):
        code = PromoCode(code='spring-twenty', kind='percent', percent=20)
        db.session.add(code)
        db.session.commit()
        post(env, f'/admin/payments/codes/{code.id}', kind='gift', percent=30, offer_id=0, active='y')
        row = db.session.get(PromoCode, code.id)
        assert (row.kind, row.percent) == ('percent', 30)


    def test_an_amount_codes_edit_page_shows_its_amount(self, env, admin):
        set_setting('payment_currency', 'usd')
        code = PromoCode(code='one-sixty-six', kind='amount', amount_minor=166)
        db.session.add(code)
        db.session.commit()
        assert 'value="1.66"' in env.client.get(f'/admin/payments/codes/{code.id}').get_data(as_text=True)


class TestACodeForAMember:
    """A gift or discount code made for one member to hand out (owner 2026-11-16): not a referral, so no referral row
    and none of the member's caps; it is listed on their referral page (see tests/test_assigned_codes.py)."""

    def test_making_one_names_the_member(self, env, admin):
        member = somebody(env, 'Handout')
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, periods=1, code='', user_name=' handout ')
        code = PromoCode.query.one()
        assert (code.assigned_to, code.owner_id) == (member.id, None)

    def test_a_discount_can_be_one(self, env, admin):
        member = somebody(env, 'handout')
        post(env, '/admin/payments/codes/add', kind='percent', percent=20, offer_id=0, code='', user_name='handout')
        assert PromoCode.query.one().assigned_to == member.id

    @pytest.mark.parametrize('name', ['nobody-here', 'faraway'], ids=['unknown', 'remote'])
    def test_only_a_local_account_can_be_named(self, env, admin, name):
        make_user(env.baseline.instance_remote, 'faraway', local=False)
        body = post(env, '/admin/payments/codes/add', offer_id=env.knight.id, periods=1, code='',
                    user_name=name).get_data(as_text=True)
        assert PromoCode.query.count() == 0 and 'No local account is called that.' in body

    def test_a_deleted_account_cannot_be_named(self, env, admin):
        member = somebody(env, 'gone')
        member.deleted = True
        db.session.commit()
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, periods=1, code='', user_name='gone')
        assert PromoCode.query.count() == 0

    def test_left_empty_it_is_nobodys(self, env, admin):
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, periods=1, code='', user_name='')
        assert PromoCode.query.one().assigned_to is None

    def test_the_list_says_whose(self, env, admin):
        member = somebody(env, 'handout')
        db.session.add(PromoCode(code='for-handout-1', kind='gift', offer_id=env.knight.id, periods=1,
                                 assigned_to=member.id))
        db.session.commit()
        page = BeautifulSoup(env.client.get('/admin/payments/codes').get_data(as_text=True), 'html.parser')
        row = page.find('tr', id=f'code-{PromoCode.query.one().id}')
        assert 'Gift code for handout' in row.get_text()

    def test_editing_shows_and_changes_the_member(self, env, admin):
        first, second = somebody(env, 'first'), somebody(env, 'second')
        code = PromoCode(code='for-first-1', kind='gift', offer_id=env.knight.id, periods=1, assigned_to=first.id)
        db.session.add(code)
        db.session.commit()
        page = BeautifulSoup(env.client.get(f'/admin/payments/codes/{code.id}').get_data(as_text=True), 'html.parser')
        assert page.find('input', attrs={'name': 'user_name'})['value'] == 'first'
        post(env, f'/admin/payments/codes/{code.id}', offer_id=env.knight.id, periods=1, active='y', user_name='second')
        assert db.session.get(PromoCode, code.id).assigned_to == second.id
        post(env, f'/admin/payments/codes/{code.id}', offer_id=env.knight.id, periods=1, active='y', user_name='')
        assert db.session.get(PromoCode, code.id).assigned_to is None

    def test_editing_to_an_unknown_name_changes_nothing(self, env, admin):
        first = somebody(env, 'first')
        code = PromoCode(code='for-first-1', kind='gift', offer_id=env.knight.id, periods=1, assigned_to=first.id)
        db.session.add(code)
        db.session.commit()
        post(env, f'/admin/payments/codes/{code.id}', offer_id=env.knight.id, periods=3, active='y', user_name='nope')
        row = db.session.get(PromoCode, code.id)
        assert (row.assigned_to, row.periods) == (first.id, 1)

    def test_a_name_with_a_hosted_domain_finds_the_member_there(self, env, admin):
        member = somebody(env, 'handout')
        server = env.app.config['SERVER_NAME']
        post(env, '/admin/payments/codes/add', offer_id=env.knight.id, periods=1, code='', user_name=f'@handout@{server}')
        assert PromoCode.query.one().assigned_to == member.id


class TestAGiftCountsInUnits:
    """Owner 2026-11-17: each gift of a free period is a number of the tier's billing periods, not days."""

    def test_the_forms_ask_for_a_number_of_units(self, env, admin):
        body = env.client.get('/admin/payments/codes').get_data(as_text=True)
        assert 'Number of units of a gift' in body and 'Days of a gift' not in body
        assert 'Each unit is one billing period of the tier' in body

    def test_the_list_says_a_gift_codes_length(self, env, admin):
        db.session.add(PromoCode(code='three-months', kind='gift', offer_id=env.knight.id, periods=3))
        db.session.commit()
        page = BeautifulSoup(env.client.get('/admin/payments/codes').get_data(as_text=True), 'html.parser')
        assert 'Furcas for 3 months' in page.find('tr', id=f'code-{PromoCode.query.one().id}').get_text()
