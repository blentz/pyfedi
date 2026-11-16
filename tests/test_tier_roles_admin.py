"""The pages for roles carried by subscription tiers (plan docs/superpowers/plans/2026-11-16-tier-roles.md D5, D7)."""
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import db
from app.complimentary import complimentary_offer
from app.models import MemberSubscription, Offer, PromoCode, Role, RoleGrant, RolePermission, Site, utcnow
from app.payments.gifts import REFERRAL_BENEFIT_SETTING
from app.roles import set_offer_roles
from app.tier_roles import offer_roles, sync_tier_roles
from app.utils import get_setting
from tests.factories import admin_role, grant_permission, make_admin, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    knight = Offer(provider_kind='btcpay', kind='subscription', title='Furcas', offer_group='tiers', amount_minor=666,
                   currency='usd', interval='month')
    tip = Offer(provider_kind='btcpay', kind='donation', title='Tip', offer_group='tips', amount_minor=300, currency='usd')
    supporter = Role(name='Supporter', listed_publicly=True)
    helper = Role(name='Helper')
    helper.permissions.append(RolePermission(permission='administer all users', all_domains=True))
    db.session.add_all([knight, tip, supporter, helper])
    db.session.commit()
    admin_role()
    return SimpleNamespace(app=app, client=app.test_client(), baseline=api_baseline, knight=knight, tip=tip,
                           supporter=supporter, helper=helper)


def somebody(env, name, *permissions):
    user = make_user(env.baseline.instance_local, name, local=True)
    for permission in permissions:
        grant_permission(user, permission)
    return user


def login(env, user):
    with env.client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def post(env, path, **data):
    with env.app.test_request_context():
        g.pop('csrf_token', None)
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction() as session:
        session['csrf_token'] = raw
    return env.client.post(path, data={'csrf_token': token, **data})


def page(env, path):
    response = env.client.get(path)
    assert response.status_code == 200
    return BeautifulSoup(response.get_data(as_text=True), 'html.parser')


@pytest.fixture
def admin(env):
    user = make_admin(somebody(env, 'boss'))
    login(env, user)
    return user


@pytest.fixture
def treasurer(env):
    user = somebody(env, 'treasurer', 'manage monetization')
    login(env, user)
    return user


def roles_path(offer):
    return f'/admin/payments/offers/{offer.id}/roles'


class TestThePage:
    def test_it_is_monetizations(self, env):
        login(env, somebody(env, 'nobody'))
        assert env.client.get(roles_path(env.knight)).status_code in (302, 403)

    def test_the_payments_page_leads_to_it_for_subscriptions_alone(self, env, treasurer):
        body = env.client.get('/admin/payments').get_data(as_text=True)
        assert roles_path(env.knight) in body and roles_path(env.tip) not in body

    def test_and_for_complimentary_membership(self, env, treasurer):
        offer = complimentary_offer()
        db.session.commit()
        assert roles_path(offer) in env.client.get('/admin/payments').get_data(as_text=True)

    def test_a_one_off_has_none(self, env, treasurer):
        assert env.client.get(roles_path(env.tip)).status_code == 404

    def test_it_lists_every_role_but_admin_with_its_permissions(self, env, admin):
        listed = page(env, roles_path(env.knight))
        names = {box['value'] for box in listed.find_all('input', attrs={'name': 'role'})}
        assert names == {str(env.supporter.id), str(env.helper.id)}
        assert 'administer all users' in listed.get_text()

    def test_it_warns_what_a_role_with_permissions_does(self, env, admin):
        assert 'domain administrators can no longer act on them' in page(env, roles_path(env.knight)).get_text()


class TestSaving:
    def test_the_roles_chosen_are_carried_and_given_now(self, env, admin):
        member = somebody(env, 'member')
        db.session.add(MemberSubscription(user_id=member.id, offer_id=env.knight.id, provider_kind='btcpay',
                                          provider_reference='inv-1', status='active', created_at=utcnow()))
        db.session.commit()
        post(env, roles_path(env.knight), role=[env.supporter.id, env.helper.id])
        assert offer_roles(env.knight) == [env.helper, env.supporter]
        assert RoleGrant.query.filter_by(user_id=member.id, via_tier=True).count() == 2

    def test_none_chosen_takes_them_away(self, env, admin):
        set_offer_roles(admin, env.knight, [env.supporter])
        post(env, roles_path(env.knight))
        assert offer_roles(env.knight) == []

    def test_admin_is_refused(self, env, admin):
        post(env, roles_path(env.knight), role=[admin_role().id])
        assert offer_roles(env.knight) == []

    def test_an_unknown_role_is_ignored(self, env, admin):
        post(env, roles_path(env.knight), role=['99999', 'x', str(env.supporter.id)])
        assert offer_roles(env.knight) == [env.supporter]


class TestATreasurer:
    """T5: a non-admin changes only roles whose every permission they hold."""

    def test_sees_a_role_they_cannot_change_as_fixed(self, env, treasurer):
        box = page(env, roles_path(env.knight)).find('input', attrs={'name': 'role', 'value': str(env.helper.id)})
        assert box.has_attr('disabled')

    def test_may_attach_a_badge(self, env, treasurer):
        post(env, roles_path(env.knight), role=[env.supporter.id])
        assert offer_roles(env.knight) == [env.supporter]

    def test_cannot_attach_a_power_they_lack(self, env, treasurer):
        response = post(env, roles_path(env.knight), role=[env.helper.id])
        assert offer_roles(env.knight) == []
        assert response.status_code == 302

    def test_saving_leaves_a_role_they_cannot_change(self, env, treasurer):
        set_offer_roles(make_admin(somebody(env, 'boss')), env.knight, [env.helper])
        post(env, roles_path(env.knight), role=[env.supporter.id])   # the fixed box sends nothing
        assert offer_roles(env.knight) == [env.helper, env.supporter]


class TestTheGrantsPanel:
    def test_a_grant_through_a_tier_says_so_and_cannot_be_changed_there(self, env, admin):
        set_offer_roles(admin, env.knight, [env.supporter])
        member = somebody(env, 'member')
        db.session.add(MemberSubscription(user_id=member.id, offer_id=env.knight.id, provider_kind='btcpay',
                                          provider_reference='inv-1', status='active', created_at=utcnow()))
        db.session.commit()
        sync_tier_roles(member)
        db.session.commit()
        grant = RoleGrant.query.filter_by(user_id=member.id).one()
        row = page(env, f'/admin/user/{member.id}/edit').find(id='grants')
        assert 'Through a subscription' in row.get_text()
        assert row.find('form', attrs={'action': f'/admin/grants/{grant.id}/revoke'}) is None

    def test_revoking_one_anyway_is_refused(self, env, admin):
        set_offer_roles(admin, env.knight, [env.supporter])
        member = somebody(env, 'member')
        db.session.add(MemberSubscription(user_id=member.id, offer_id=env.knight.id, provider_kind='btcpay',
                                          provider_reference='inv-1', status='active', created_at=utcnow()))
        db.session.commit()
        sync_tier_roles(member)
        db.session.commit()
        grant = RoleGrant.query.filter_by(user_id=member.id).one()
        post(env, f'/admin/grants/{grant.id}/revoke')
        assert db.session.get(RoleGrant, grant.id) is not None


class TestATierGivingAPowerIsAdmins:
    """T6 (owner 2026-11-16): a tier carrying a role the manager couldn't attach is Admin's alone, wherever it could be
    handed out or repriced."""

    @pytest.fixture
    def locked(self, env):
        set_offer_roles(make_admin(somebody(env, 'boss')), env.knight, [env.helper])
        return env.knight

    def test_its_offer_cannot_be_edited(self, env, treasurer, locked):
        assert env.client.get(f'/admin/payments/offers/{locked.id}/edit').status_code == 302
        post(env, f'/admin/payments/offers/{locked.id}/edit', title='Cheap', amount='0.01')
        assert db.session.get(Offer, locked.id).title == 'Furcas'

    def test_nor_deleted(self, env, treasurer, locked):
        post(env, f'/admin/payments/offers/{locked.id}/delete')
        assert db.session.get(Offer, locked.id) is not None

    def test_another_offer_still_can(self, env, treasurer, locked):
        assert env.client.get(f'/admin/payments/offers/{env.tip.id}/edit').status_code == 200

    def test_no_gift_code_for_it(self, env, treasurer, locked):
        post(env, '/admin/payments/codes/add', offer_id=locked.id, days=365, code='')
        assert PromoCode.query.count() == 0

    @pytest.mark.parametrize('offer', ['tier', 'any'])
    def test_no_discount_code_reaching_it(self, env, treasurer, locked, offer):
        post(env, '/admin/payments/codes/add', kind='percent', percent=99, code='',
             offer_id=locked.id if offer == 'tier' else 0)
        assert PromoCode.query.count() == 0

    def test_a_discount_for_another_tier_is_fine(self, env, treasurer, locked):
        other = Offer(provider_kind='btcpay', kind='subscription', title='Patron', offer_group='patrons',
                      amount_minor=100, currency='usd', interval='month')
        db.session.add(other)
        db.session.commit()
        post(env, '/admin/payments/codes/add', kind='percent', percent=10, code='', offer_id=other.id)
        assert PromoCode.query.count() == 1

    def test_an_existing_code_cannot_be_turned_to_it(self, env, treasurer, locked):
        code = PromoCode(code='gift-for-tips', kind='gift', offer_id=env.tip.id, days=30)
        db.session.add(code)
        db.session.commit()
        post(env, f'/admin/payments/codes/{code.id}', offer_id=locked.id, days=365, active='y')
        assert db.session.get(PromoCode, code.id).offer_id == env.tip.id

    def test_nor_one_for_it_changed(self, env, treasurer, locked):
        code = PromoCode(code='gift-for-knights', kind='gift', offer_id=locked.id, days=30, active=False)
        db.session.add(code)
        db.session.commit()
        post(env, f'/admin/payments/codes/{code.id}', offer_id=locked.id, days=30, active='y')
        assert db.session.get(PromoCode, code.id).active is False

    def test_it_is_not_made_the_referral_default(self, env, treasurer, locked):
        post(env, '/admin/payments/codes/default', benefit=f'gift:{locked.id}', days=365)
        assert not get_setting(REFERRAL_BENEFIT_SETTING)

    def test_nor_is_the_automatic_default_which_may_choose_it(self, env, treasurer, locked):
        post(env, '/admin/payments/codes/default', benefit='auto')
        assert not get_setting(REFERRAL_BENEFIT_SETTING)

    def test_complimentary_carrying_a_power_is_not_given(self, env, treasurer):
        boss = make_admin(somebody(env, 'boss'))
        offer = complimentary_offer()
        db.session.commit()
        set_offer_roles(boss, offer, [env.helper])
        member = somebody(env, 'member')
        post(env, f'/admin/payments/complimentary/{member.id}', grant='y')
        assert MemberSubscription.query.filter_by(user_id=member.id).count() == 0

    def test_an_admin_may_do_all_of_it(self, env, admin, locked):
        post(env, '/admin/payments/codes/add', offer_id=locked.id, days=365, code='')
        assert PromoCode.query.count() == 1


def test_a_referral_code_cannot_be_turned_to_a_tier_giving_a_power(env, treasurer):
    from_admin = make_admin(somebody(env, 'boss'))
    set_offer_roles(from_admin, env.knight, [env.helper])
    member = somebody(env, 'referrer')
    row = PromoCode(code='referral-row-1', kind='gift', owner_id=member.id, benefit='gift', offer_id=env.tip.id,
                    days=30)
    db.session.add(row)
    db.session.commit()
    post(env, f'/admin/payments/codes/{row.id}', benefit=f'gift:{env.knight.id}', days=365, active='y')
    assert db.session.get(PromoCode, row.id).offer_id != env.knight.id
    assert 'Only an admin can do this for Furcas' in env.client.get('/admin/payments/codes').get_data(as_text=True)


def test_the_roles_page_says_how_many_codes_it_turned_off(env, admin):
    treasurer = somebody(env, 'treasurer', 'manage monetization')
    db.session.add(PromoCode(code='treasurers-gift', kind='gift', offer_id=env.knight.id, days=30,
                             created_by=treasurer.id))
    db.session.commit()
    post(env, roles_path(env.knight), role=[env.helper.id])
    body = env.client.get('/admin/payments').get_data(as_text=True)
    assert '1 code(s) for it were turned off' in body
