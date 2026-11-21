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
from app.models import MemberSubscription, Offer, OfferDomain, PromoCode, PromoCodeUse, Site, SiteDomain, utcnow
from app.payments.gifts import delete_gift_code, join_domain, sold_on
from app.referral import GENERATORS_SETTING, REFERRAL_MODE, generate_code
from app.storefront import clear_domain_cache, default_domain
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
                      paid_membership=True, registration_offer_id=knight.id, lapse_grace_days=3,
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


def login(env, user, base=None):
    with env.client.session_transaction(**({'base_url': base} if base else {})) as session:
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
        env.home.paid_membership = False
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


def post(env, path, csrf=True):
    """POST with a real CSRF token, checked (WTF_CSRF_ENABLED); csrf=False sends none."""
    data = {}
    if csrf:
        with env.app.test_request_context():
            g.pop('csrf_token', None)
            data['csrf_token'] = generate_csrf()
            raw = flask_session['csrf_token']
        with env.client.session_transaction() as session:
            session['csrf_token'] = raw
    with patch.dict(current_app.config, {'WTF_CSRF_ENABLED': True}):
        return env.client.post(path, data=data, base_url=env.base)


def delete_path(row):
    return f'{PAGE}/codes/{row.id}/delete'


class TestTheMemberDeletesACode:
    """Owner 2026-11-19: the member a gift code was made for can delete it."""

    def test_their_gift_code_is_deleted(self, env):
        holder = member(env)
        row, other = code_for(env, holder), code_for(env, holder, text='hand-out-two')
        login(env, holder)

        response = post(env, delete_path(row))

        assert response.status_code == 302 and response.headers['Location'].endswith(PAGE)   # one is left to show
        assert db.session.get(PromoCode, row.id) is None and db.session.get(PromoCode, other.id) is not None
        listed = page(env)
        assert listed.find(id=f'assigned-{row.id}') is None
        assert 'hand-out-one' not in [code.get_text() for code in listed.find_all('code')]
        assert 'Code hand-out-one deleted.' in listed.get_text()

    def test_with_none_left_they_go_to_their_settings(self, env):
        holder = member(env)
        row = code_for(env, holder)
        login(env, holder)

        response = post(env, delete_path(row))

        assert response.headers['Location'].endswith('/user/settings')
        assert env.client.get(PAGE, base_url=env.base).status_code == 404   # nothing left: no referral page

    def test_whoever_used_it_keeps_what_it_gave(self, env):
        holder, joiner = member(env), member(env, 'joiner')
        row = code_for(env, holder)
        given = MemberSubscription(user_id=joiner.id, offer_id=env.knight.id, provider_kind='gift',
                                   provider_reference='gift:kept', status='active', created_at=utcnow(),
                                   current_period_end=utcnow() + timedelta(days=30))
        db.session.add(given)
        db.session.flush()
        db.session.add(PromoCodeUse(promo_code_id=row.id, user_id=joiner.id, subscription_id=given.id))
        db.session.commit()
        login(env, holder)

        post(env, delete_path(row))

        assert db.session.get(MemberSubscription, given.id).status == 'active'
        assert PromoCodeUse.query.filter_by(user_id=joiner.id).count() == 0

    def test_the_page_offers_it(self, env):
        holder = member(env)
        row = code_for(env, holder)
        login(env, holder)
        form = block(env, row).find('form', action=delete_path(row))
        assert form is not None and 'confirm_first' in form.find('button')['class']

    def test_a_discount_code_is_not_theirs_to_delete(self, env):
        holder = member(env)
        row = code_for(env, holder, kind='percent', percent=20, offer_id=None, periods=None)
        login(env, holder)
        assert block(env, row).find('form', action=delete_path(row)) is None
        assert post(env, delete_path(row)).status_code == 404
        assert db.session.get(PromoCode, row.id) is not None

    def test_another_members_code_is_404(self, env):
        holder, other = member(env), member(env, 'other')
        row = code_for(env, other)
        code_for(env, holder, text='hand-out-two')
        login(env, holder)
        assert post(env, delete_path(row)).status_code == 404
        assert db.session.get(PromoCode, row.id) is not None

    def test_a_code_made_for_nobody_is_404(self, env):
        holder = member(env)
        row = code_for(env, None)
        login(env, holder)
        assert post(env, delete_path(row)).status_code == 404
        assert db.session.get(PromoCode, row.id) is not None

    def test_without_a_csrf_token_nothing_is_deleted(self, env):
        holder = member(env)
        row = code_for(env, holder)
        login(env, holder)
        assert post(env, delete_path(row), csrf=False).status_code == 400
        assert db.session.get(PromoCode, row.id) is not None

    def test_signed_out_nothing_is_deleted(self, env):
        row = code_for(env, member(env))
        assert post(env, delete_path(row)).status_code == 302   # to the login
        assert db.session.get(PromoCode, row.id) is not None


def test_a_code_gone_before_it_is_deleted_is_no_error(env):
    """Two deletes at once: the second finds no row under its lock and does nothing."""

    row = code_for(env, member(env))
    PromoCode.query.filter_by(id=row.id).delete()
    db.session.flush()
    delete_gift_code(row)
    db.session.commit()
    assert db.session.get(PromoCode, row.id) is None


def test_a_member_without_referral_codes_cannot_generate_one(env):
    """Holding only codes an admin made, the referral page is theirs but their own referral code is not (RC1)."""
    holder = member(env)
    code_for(env, holder)
    login(env, holder)
    assert post(env, f'{PAGE}/generate').status_code == 404
    assert db.session.get(type(holder), holder.id).referral_code is None


class TestItsLinkIsOnTheDomainItWorksOn:
    """Owner 2026-11-20: the link to join with a code names the domain the member is on when the code admits there,
    else one it does."""

    SECOND = 'https://second.example'

    @pytest.fixture
    def second(self, env):
        row = SiteDomain(domain='second.example', scheme='https', federates=True, paid_membership=True,
                         registration_offer_id=env.knight.id)
        db.session.add(row)
        db.session.commit()
        clear_domain_cache()
        return row

    def link_on(self, env, row, base):
        response = env.client.get(PAGE, base_url=base)
        shown = BeautifulSoup(response.get_data(as_text=True), 'html.parser').find(id=f'assigned-{row.id}')
        field = shown.find('input', id=f'assigned-link-{row.id}')
        return field['value'] if field is not None else None

    def test_on_another_domain_the_link_is_that_domains(self, env, second):
        holder = member(env)
        row = code_for(env, holder)
        login(env, holder)
        login(env, holder, self.SECOND)
        assert self.link_on(env, row, self.SECOND) == f'{self.SECOND}/auth/register?code=hand-out-one'
        assert self.link_on(env, row, env.base) == f'{env.base}/auth/register?code=hand-out-one'

    def test_where_its_tier_is_not_sold_the_link_is_a_domain_where_it_is(self, env, second):
        own = Offer(provider_kind='stripe', kind='subscription', title='Second only', offer_group='second',
                    provider_price_id='price_s', amount_minor=100, currency='usd', interval='month')
        db.session.add(own)
        db.session.flush()
        db.session.add(OfferDomain(offer_id=own.id, site_domain_id=second.id))
        second.registration_offer_id = own.id   # the second domain sells its own tier only
        db.session.commit()
        clear_domain_cache()
        holder = member(env)
        row = code_for(env, holder)   # Furcas: the default domain's
        login(env, holder, self.SECOND)
        assert self.link_on(env, row, self.SECOND) == f'{env.base}/auth/register?code=hand-out-one'

    def test_a_tier_sold_nowhere_gets_no_link(self, env):
        holder = member(env)
        retired = Offer(provider_kind='stripe', kind='subscription', title='Retired', offer_group='tiers',
                        provider_price_id='price_r', amount_minor=100, currency='usd', interval='month', active=False)
        db.session.add(retired)
        db.session.commit()
        row = code_for(env, holder, offer_id=retired.id)
        login(env, holder)
        assert self.link_on(env, row, env.base) is None


class TestSoldOn:
    def test_nowhere_joining_is_free(self, env):
        env.home.paid_membership = False
        db.session.commit()
        clear_domain_cache()
        row = code_for(env, None)
        with env.app.test_request_context():
            assert not sold_on(row, default_domain()) and join_domain(row) is None

    def test_a_discount_for_any_offer_wherever_joining_is_paid_for(self, env):
        row = code_for(env, None, kind='percent', percent=10, offer_id=None, periods=None)
        with env.app.test_request_context():
            assert sold_on(row, default_domain())

    def test_a_discount_for_one_tier_where_that_tier_is_sold(self, env):
        other = Offer(provider_kind='stripe', kind='subscription', title='Other', offer_group='elsewhere',
                      provider_price_id='price_o', amount_minor=100, currency='usd', interval='month', active=False)
        db.session.add(other)
        db.session.commit()
        on_knight = code_for(env, None, text='knight-off-ten', kind='percent', percent=10, periods=None)
        on_other = code_for(env, None, text='other-off-ten', kind='percent', percent=10, offer_id=other.id, periods=None)
        with env.app.test_request_context():
            assert sold_on(on_knight, default_domain()) and not sold_on(on_other, default_domain())
