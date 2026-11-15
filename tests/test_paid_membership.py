"""Paid membership on the default domain (spec 2026-11-15-paid-membership-design.md, plan phase 1, M1-M7)."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import MemberSubscription, Offer, Site, SiteDomain, User, utcnow
from app.storefront import clear_domain_cache, default_domain, paid_registration
from tests.factories import grant_permission, make_user

CHECKOUT = 'https://checkout.example/session'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.registration_mode = 'Open'
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    knight = Offer(provider_kind='stripe', kind='subscription', title='Furcas', offer_group='tiers',
                   provider_price_id='price_k', amount_minor=666, currency='usd', interval='month', position=1)
    duke = Offer(provider_kind='stripe', kind='subscription', title='Astaroth', offer_group='tiers',
                 provider_price_id='price_d', amount_minor=1666, currency='usd', interval='month', position=2)
    gift = Offer(provider_kind='stripe', kind='donation', title='Gift', offer_group='gifts', provider_price_id='price_g',
                 amount_minor=500, currency='usd')
    db.session.add_all([knight, duke, gift])
    db.session.flush()
    home = SiteDomain(domain=current_app.config['SERVER_NAME'], scheme='https', federates=True, is_default=True,
                      registration='paid', registration_offer_id=knight.id, lapse_grace_days=3,
                      paid_since=utcnow() - timedelta(days=10))
    db.session.add(home)
    db.session.commit()
    clear_domain_cache()
    yield SimpleNamespace(app=app, client=app.test_client(), site=site, home=home, knight=knight, duke=duke, gift=gift,
                          baseline=api_baseline, base=f"https://{current_app.config['SERVER_NAME']}")
    clear_domain_cache()


def member(env, name='member', created_days_ago=1):
    user = make_user(env.baseline.instance_local, name, local=True)
    user.created = utcnow() - timedelta(days=created_days_ago)
    db.session.commit()
    return user


def lapsed_since(user, days):
    user.access_lapsed_at = utcnow() - timedelta(days=days)
    db.session.commit()
    return user


class TestTheDefaultDomainCanCharge:
    def test_paid_on_its_own_row_makes_it_paid(self, env):
        assert paid_registration(default_domain())

    def test_anyone_on_its_own_row_keeps_it_free(self, env):
        env.home.registration = 'anyone'
        db.session.commit()
        clear_domain_cache()
        assert not paid_registration(default_domain())

    def test_the_date_it_began_charging_is_known(self, env):
        assert default_domain().paid_since == env.home.paid_since


class TestLapsing:
    def test_a_member_who_joined_since_is_locked_after_the_grace(self, env):
        assert lapsed_since(member(env), days=4).access_has_lapsed()

    def test_not_within_the_grace(self, env):
        assert not lapsed_since(member(env), days=2).access_has_lapsed()

    def test_one_who_joined_before_it_charged_never_is(self, env):
        """PM5: grandfathered for good, even after holding a plan and letting it go."""
        assert not lapsed_since(member(env, created_days_ago=30), days=40).access_has_lapsed()

    def test_anyone_holding_a_role_never_is(self, env):
        user = lapsed_since(member(env), days=40)
        grant_permission(user, 'moderate all communities')
        assert not user.access_has_lapsed()

    def test_a_domain_with_no_start_date_keeps_the_old_rule(self, env):
        """Every sister domain made paid before this: no account is grandfathered by date."""
        env.home.paid_since = None
        db.session.commit()
        clear_domain_cache()
        assert lapsed_since(member(env, created_days_ago=30), days=40).access_has_lapsed()

    def test_a_pending_account_remembers_its_tier(self, env):
        user = member(env)
        user.pending_offer_id = env.duke.id
        db.session.commit()
        assert db.session.get(User, user.id).pending_offer_id == env.duke.id

    def test_a_removed_offer_forgets_it(self, env):
        user = member(env)
        user.pending_offer_id = env.gift.id
        db.session.commit()
        db.session.delete(env.gift)
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(User, user.id).pending_offer_id is None
