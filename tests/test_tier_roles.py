"""Roles carried by subscription tiers (plan docs/superpowers/plans/2026-11-16-tier-roles.md, T1-T5, D1-D7)."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.complimentary import end_complimentary, grant_complimentary, grant_to_new_account
from app.models import MemberSubscription, ModLog, Offer, PromoCode, Role, RoleGrant, RolePermission, Site, SiteDomain, \
    User, utcnow
from app.payments.core import apply_event, cancel_subscription, end_expired_prepaid
from app.payments.events import SubscriptionEnded, SubscriptionStarted
from app.payments.gifts import start_gift
from app.permissions import user_access
from app.roles import RoleError, change_expiry, delete_role, grant_role, may_attach, remove_expired_grants, revoke_grant, \
    set_offer_roles
from app.storefront import clear_domain_cache
from app.tier_roles import offer_roles, sync_tier_roles
from tests.factories import admin_role, grant_permission, limit_role, make_admin, make_user


@pytest.fixture(autouse=True)
def provider_calls():
    with patch('app.payments.core.cancel_at_provider'), \
            patch('app.payments.adapters.stripe_adapter.StripeAdapter.cancel'):
        yield


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    knight = Offer(provider_kind='stripe', kind='subscription', title='Furcas', offer_group='tiers',
                   provider_price_id='price_k', amount_minor=666, currency='usd', interval='month')
    duke = Offer(provider_kind='stripe', kind='subscription', title='Astaroth', offer_group='tiers',
                 provider_price_id='price_d', amount_minor=1666, currency='usd', interval='month')
    tip = Offer(provider_kind='stripe', kind='donation', title='Tip', offer_group='tips', amount_minor=300, currency='usd')
    db.session.add_all([knight, duke, tip])
    db.session.flush()
    home = SiteDomain(domain=current_app.config['SERVER_NAME'], scheme='https', federates=True, is_default=True,
                      registration='anyone', registration_offer_id=knight.id, lapse_grace_days=3)
    supporter = Role(name='Supporter', listed_publicly=True)   # a badge: no permissions
    helper = Role(name='Helper')
    helper.permissions.append(RolePermission(permission='administer all users', all_domains=True))
    db.session.add_all([home, supporter, helper])
    db.session.commit()
    clear_domain_cache()
    yield SimpleNamespace(site=site, home=home, knight=knight, duke=duke, tip=tip, supporter=supporter, helper=helper,
                          baseline=api_baseline, admin=make_admin(make_user(api_baseline.instance_local, 'boss',
                                                                            local=True)))
    clear_domain_cache()


def member(env, name='member'):
    return make_user(env.baseline.instance_local, name, local=True)


def start(user, offer, reference='sub_1'):
    apply_event('stripe', f'start-{reference}', [SubscriptionStarted(reference, user.id, offer.id)])


def end(reference='sub_1'):
    apply_event('stripe', f'end-{reference}', [SubscriptionEnded(reference)])


def grant_of(user, role):
    return RoleGrant.query.filter_by(user_id=user.id, role_id=role.id).first()


def holds(user, role):
    grant = grant_of(user, role)
    return grant is not None and (grant.expires_at is None or grant.expires_at > utcnow())


def charge_for_joining(env):
    env.home.registration = 'paid'
    env.home.paid_since = utcnow() - timedelta(days=30)
    db.session.commit()
    clear_domain_cache()


class TestHoldingTheTiersRoles:
    def test_subscribing_grants_them(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter, env.helper])
        user = member(env)
        start(user, env.knight)
        grant = grant_of(user, env.supporter)
        assert grant.via_tier and grant.expires_at is None and grant.granted_by is None
        assert user_access('administer all users', user.id)

    def test_another_tier_grants_nothing(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.duke)
        assert grant_of(user, env.supporter) is None

    def test_the_grant_is_in_the_modlog_naming_no_admin(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.knight)
        row = ModLog.query.filter_by(action='grant_role', target_user_id=user.id).one()
        assert row.user_id is None and 'Supporter' in row.reason and 'Furcas' in row.reason

    def test_a_cancelled_but_paid_up_subscription_keeps_them(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.knight)
        cancel_subscription(MemberSubscription.query.one())
        assert holds(user, env.supporter) and grant_of(user, env.supporter).expires_at is None

    def test_ending_where_nothing_lapses_ends_them_at_once(self, env):
        set_offer_roles(env.admin, env.knight, [env.helper])
        user = member(env)
        start(user, env.knight)
        end()
        assert not holds(user, env.helper) and not user_access('administer all users', user.id)
        assert remove_expired_grants() == 1 and grant_of(user, env.helper) is None

    def test_where_joining_is_paid_they_last_the_grace_days(self, env):
        charge_for_joining(env)
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.knight)
        end()
        user = db.session.get(User, user.id)
        assert grant_of(user, env.supporter).expires_at == user.access_lapsed_at + timedelta(days=3)
        assert holds(user, env.supporter)

    def test_a_role_with_permissions_ends_as_the_lock_begins(self, env):
        """PM5: a role holder never locks. The grant's end is the lock's start, so neither outlives the other."""
        charge_for_joining(env)
        set_offer_roles(env.admin, env.knight, [env.helper])
        user = member(env)
        start(user, env.knight)
        end()
        user = db.session.get(User, user.id)
        assert not user.access_has_lapsed()
        user.access_lapsed_at -= timedelta(days=4)
        grant_of(user, env.helper).expires_at -= timedelta(days=4)
        db.session.commit()
        assert user.access_has_lapsed()

    def test_subscribing_again_keeps_them(self, env):
        charge_for_joining(env)
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.knight)
        end()
        start(user, env.knight, reference='sub_2')
        assert grant_of(user, env.supporter).expires_at is None

    def test_an_end_already_set_is_not_pushed_back(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.knight)
        end()
        first = grant_of(user, env.supporter).expires_at
        sync_tier_roles(user)
        db.session.commit()
        assert grant_of(user, env.supporter).expires_at == first

    def test_one_tier_ending_leaves_a_role_another_still_gives(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        other = Offer(provider_kind='stripe', kind='subscription', title='Patron', offer_group='patrons',
                      provider_price_id='price_p', amount_minor=100, currency='usd', interval='month')
        db.session.add(other)
        db.session.commit()
        set_offer_roles(env.admin, other, [env.supporter])
        user = member(env)
        start(user, env.knight)
        start(user, other, reference='sub_2')
        end()
        assert holds(user, env.supporter)

    def test_the_daily_sweep_ends_them(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.knight)
        MemberSubscription.query.one().ends_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
        end_expired_prepaid()
        assert not holds(user, env.supporter)


class TestARoleAnAdminGave:
    def test_is_never_ended_by_a_tier(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        grant_role(env.admin, user, env.supporter)
        start(user, env.knight)
        end()
        grant = grant_of(user, env.supporter)
        assert not grant.via_tier and grant.expires_at is None

    def test_granting_one_held_through_a_tier_makes_it_the_admins(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.knight)
        grant_role(env.admin, user, env.supporter)
        end()
        grant = grant_of(user, env.supporter)
        assert (grant.via_tier, grant.granted_by, grant.expires_at) == (False, env.admin.id, None)

    def test_when_it_runs_out_the_tier_gives_it_again(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        grant_role(env.admin, user, env.supporter, expires_at=utcnow() + timedelta(days=1))
        start(user, env.knight)
        grant_of(user, env.supporter).expires_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
        remove_expired_grants()
        grant = grant_of(user, env.supporter)
        assert grant.via_tier and grant.expires_at is None

    def test_one_held_through_a_tier_cannot_be_revoked_or_given_an_end(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.knight)
        grant = grant_of(user, env.supporter)
        with pytest.raises(RoleError, match='through a subscription'):
            revoke_grant(env.admin, grant)
        with pytest.raises(RoleError, match='through a subscription'):
            change_expiry(env.admin, grant, utcnow() + timedelta(days=1))
        assert holds(user, env.supporter)


class TestGiftsAndComplimentary:
    def test_a_gift_of_the_tier_gives_them(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        code = PromoCode(code='gift-for-you', kind='gift', offer_id=env.knight.id, periods=1)
        db.session.add(code)
        db.session.commit()
        start_gift(user, code)
        assert holds(user, env.supporter)

    def test_complimentary_membership_gives_its_roles(self, env):
        user = member(env)
        grant_complimentary(user)
        db.session.commit()
        offer = MemberSubscription.query.filter_by(user_id=user.id).one().offer_id
        set_offer_roles(env.admin, db.session.get(Offer, offer), [env.supporter])
        assert holds(user, env.supporter)

    def test_a_new_account_gets_complimentarys_roles(self, env):
        holder = member(env, 'first')
        grant_complimentary(holder)
        db.session.commit()
        set_offer_roles(env.admin, db.session.get(Offer, MemberSubscription.query.one().offer_id), [env.supporter])
        newcomer = member(env, 'newcomer')
        grant_to_new_account(newcomer)
        db.session.commit()
        assert holds(newcomer, env.supporter)

    def test_ending_complimentary_ends_them(self, env):
        user = member(env)
        grant_complimentary(user)
        db.session.commit()
        set_offer_roles(env.admin, db.session.get(Offer, MemberSubscription.query.one().offer_id), [env.supporter])
        end_complimentary(user)
        sync_tier_roles(user)
        db.session.commit()
        assert not holds(user, env.supporter)


class TestSettingATiersRoles:
    def test_adding_one_gives_it_to_the_tiers_members_now(self, env):
        user = member(env)
        start(user, env.knight)
        set_offer_roles(env.admin, env.knight, [env.supporter])
        assert holds(user, env.supporter) and offer_roles(env.knight) == [env.supporter]

    def test_taking_one_away_ends_it_for_them(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        user = member(env)
        start(user, env.knight)
        set_offer_roles(env.admin, env.knight, [])
        assert not holds(user, env.supporter) and offer_roles(env.knight) == []

    def test_admin_is_never_one(self, env):
        with pytest.raises(RoleError, match='Admin'):
            set_offer_roles(env.admin, env.knight, [admin_role()])
        assert offer_roles(env.knight) == []

    def test_a_one_off_carries_none(self, env):
        with pytest.raises(RoleError, match='subscription'):
            set_offer_roles(env.admin, env.tip, [env.supporter])

    def test_deleting_the_role_takes_it_off_the_tier(self, env):
        set_offer_roles(env.admin, env.knight, [env.supporter])
        delete_role(env.admin, env.supporter, revoke=True)
        assert offer_roles(env.knight) == []


class TestWhoMayAttachWhat:
    """T5: a non-admin with manage monetization attaches or removes only roles whose every permission they hold, on at
    least the same domains."""

    @pytest.fixture
    def treasurer(self, env):
        user = member(env, 'treasurer')
        grant_permission(user, 'manage monetization')
        return user

    def test_a_badge_role(self, env, treasurer):
        assert may_attach(treasurer, env.supporter)
        set_offer_roles(treasurer, env.knight, [env.supporter])
        assert offer_roles(env.knight) == [env.supporter]

    def test_not_a_role_with_a_power_they_lack(self, env, treasurer):
        assert not may_attach(treasurer, env.helper)
        with pytest.raises(RoleError, match='Helper'):
            set_offer_roles(treasurer, env.knight, [env.helper])
        assert offer_roles(env.knight) == []

    def test_nor_take_one_away(self, env, treasurer):
        set_offer_roles(env.admin, env.knight, [env.helper])
        with pytest.raises(RoleError, match='Helper'):
            set_offer_roles(treasurer, env.knight, [])
        assert offer_roles(env.knight) == [env.helper]

    def test_a_role_with_a_power_they_hold_everywhere(self, env, treasurer):
        grant_permission(treasurer, 'administer all users')
        assert may_attach(treasurer, env.helper)

    def test_not_one_reaching_further_than_theirs(self, env, treasurer):
        own = grant_permission(treasurer, 'administer all users')
        limit_role(own, env.home)
        assert not may_attach(treasurer, env.helper)   # every domain, against their one

    def test_one_reaching_no_further(self, env, treasurer):
        own = grant_permission(treasurer, 'administer all users')
        limit_role(own, env.home)
        limit_role(env.helper, env.home)
        assert may_attach(treasurer, env.helper)

    def test_an_expired_power_counts_for_nothing(self, env, treasurer):
        grant_permission(treasurer, 'administer all users')
        grant = RoleGrant.query.join(Role).filter(Role.name == 'role-administer all users').one()
        grant.expires_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
        assert not may_attach(treasurer, env.helper)

    def test_an_admin_may_attach_any_but_admin(self, env):
        assert may_attach(env.admin, env.helper) and not may_attach(env.admin, admin_role())


class TestOnlyLastingPowersCount:
    """Security review 2026-11-16: a power held for a while, or through a tier, can't be attached to a tier for good."""

    @pytest.fixture
    def treasurer(self, env):
        user = member(env, 'treasurer')
        grant_permission(user, 'manage monetization')
        return user

    def test_a_power_held_until_a_date(self, env, treasurer):
        grant_permission(treasurer, 'administer all users')
        RoleGrant.query.join(Role).filter(Role.name == 'role-administer all users').one().expires_at = \
            utcnow() + timedelta(days=1)
        db.session.commit()
        assert not may_attach(treasurer, env.helper)

    def test_a_power_held_through_a_tier(self, env, treasurer):
        set_offer_roles(env.admin, env.knight, [env.helper])
        start(treasurer, env.knight)
        assert user_access('administer all users', treasurer.id) and not may_attach(treasurer, env.helper)


class TestCodesMadeBeforeAPowerWasAdded:
    """Security review 2026-11-16: a code a manager made for a tier before an admin gave the tier a power they lack
    would hand that power out. Adding the role turns such codes off; an admin's own stay."""

    @pytest.fixture
    def treasurer(self, env):
        user = member(env, 'treasurer')
        grant_permission(user, 'manage monetization')
        return user

    def code(self, creator, **values):
        row = PromoCode(code=f'code-{len(PromoCode.query.all())}-made', created_by=creator.id,
                        **({'kind': 'gift', 'periods': 1} | values))
        db.session.add(row)
        db.session.commit()
        return row

    def test_a_managers_gift_code_for_it_is_turned_off(self, env, treasurer):
        row = self.code(treasurer, offer_id=env.knight.id)
        set_offer_roles(env.admin, env.knight, [env.helper])
        assert db.session.get(PromoCode, row.id).active is False

    def test_so_is_their_discount_for_any_offer(self, env, treasurer):
        row = self.code(treasurer, kind='percent', percent=50, periods=None)
        set_offer_roles(env.admin, env.knight, [env.helper])
        assert db.session.get(PromoCode, row.id).active is False

    def test_an_admins_code_stays_on(self, env):
        row = self.code(env.admin, offer_id=env.knight.id)
        set_offer_roles(env.admin, env.knight, [env.helper])
        assert db.session.get(PromoCode, row.id).active is True

    def test_a_badge_leaves_a_managers_code_on(self, env, treasurer):
        row = self.code(treasurer, offer_id=env.knight.id)
        set_offer_roles(env.admin, env.knight, [env.supporter])
        assert db.session.get(PromoCode, row.id).active is True

    def test_another_tiers_code_stays_on(self, env, treasurer):
        row = self.code(treasurer, offer_id=env.duke.id)
        set_offer_roles(env.admin, env.knight, [env.helper])
        assert db.session.get(PromoCode, row.id).active is True

    def test_a_code_nobody_made_is_turned_off(self, env):
        row = PromoCode(code='orphan-code-1', kind='gift', offer_id=env.knight.id, periods=1)
        db.session.add(row)
        db.session.commit()
        assert set_offer_roles(env.admin, env.knight, [env.helper]) == 1
        assert db.session.get(PromoCode, row.id).active is False


class TestPowersHeldTwice:
    @pytest.fixture
    def treasurer(self, env):
        user = member(env, 'treasurer')
        grant_permission(user, 'manage monetization')
        return user

    def second_domain(self):
        shop = SiteDomain(domain='shop.tiers.example', scheme='https', federates=False)
        db.session.add(shop)
        db.session.commit()
        clear_domain_cache()
        return shop

    def powered(self, name, *domains):
        role = Role(name=name)
        role.permissions.append(RolePermission(permission='administer all users', all_domains=True))
        db.session.add(role)
        db.session.commit()
        return limit_role(role, *domains) if domains else role

    def test_its_domains_add_up(self, env, treasurer):
        shop = self.second_domain()
        for role in (self.powered('Here', env.home), self.powered('There', shop)):
            grant_role(env.admin, treasurer, role)
        limit_role(env.helper, env.home, shop)
        assert may_attach(treasurer, env.helper)

    @pytest.mark.parametrize('everywhere_first', [True, False], ids=['everywhere-first', 'here-first'])
    def test_every_domain_from_either_is_every_domain(self, env, treasurer, everywhere_first):
        # roles are read in the order they were made
        made = [self.powered('Everywhere'), self.powered('Here', env.home)] if everywhere_first else \
            [self.powered('Here', env.home), self.powered('Everywhere')]
        for role in made:
            grant_role(env.admin, treasurer, role)
        assert may_attach(treasurer, env.helper)



def test_a_lapse_kept_from_when_the_domain_charged_gives_no_grace_once_it_is_free(env):
    """Taking a role off a tier syncs its members without refresh_access, which would have cleared the old lapse."""
    set_offer_roles(env.admin, env.knight, [env.supporter])
    user = member(env)
    start(user, env.knight)
    user.access_lapsed_at = utcnow() - timedelta(hours=1)   # from when the domain charged
    db.session.commit()
    set_offer_roles(env.admin, env.knight, [])
    assert not holds(user, env.supporter)
