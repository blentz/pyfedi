"""Complimentary membership (plan 2026-11-15-complimentary-membership.md, K1-K8): a hidden tier, given by admins and to
every local account that holds no subscription, that never ends and is never paid for."""
import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf
from sqlalchemy import text

from app import db
from app.complimentary import KIND, complimentary_of, complimentary_offer, end_complimentary, grant_complimentary, \
    grant_to_new_account
from app.models import CmsPage, MemberSubscription, Offer, PromoCode, Site, SiteDomain, utcnow
from app.payments.adapters import adapter_for
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.core import end_expired_prepaid, end_subscriptions_of, refresh_access, remind_expiring_prepaid
from app.payments.offers import offers_changed, offers_here
from app.payments.status import subscription_statuses
from app.storefront import clear_domain_cache
from app.utils import finalize_user_setup, markdown_to_html
from app.models import Role
from app.roles import grant_role as role_grant
from tests.factories import admin_role, grant_permission, make_admin, make_user, pay_link


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    tier = Offer(provider_kind='stripe', kind='subscription', title='Furcas', offer_group='tiers',
                 provider_price_id='price_furcas', amount_minor=666, currency='usd', interval='month')
    tip = Offer(provider_kind='stripe', kind='donation', title='Tip', offer_group='tips',
                provider_price_id='price_tip', amount_minor=500, currency='usd')
    db.session.add_all([tier, tip])
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    yield SimpleNamespace(app=app, client=app.test_client(), tier=tier, tip=tip, member=member, baseline=api_baseline)
    offers_changed()
    clear_domain_cache()


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def post(env, path, **data):
    with env.app.test_request_context():
        g.pop('csrf_token', None)   # the fixture's app context outlives each request: a token cached there has no session
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with env.client.session_transaction() as session:
        session['csrf_token'] = raw
    return env.client.post(path, data={'csrf_token': token, **data})


def given(user):
    row = grant_complimentary(user)
    db.session.commit()
    return row


def paid_row(user, offer, status='active', reference='sub_1'):
    row = MemberSubscription(user_id=user.id, offer_id=offer.id, provider_kind='stripe', provider_reference=reference,
                             status=status, ended_at=utcnow() if status == 'ended' else None)
    db.session.add(row)
    db.session.commit()
    return row


def paid_domain(env):
    home = SiteDomain(domain=current_app.config['SERVER_NAME'], scheme='https', federates=True, is_default=True,
                      paid_membership=True, registration_offer_id=env.tier.id, lapse_grace_days=3,
                      paid_since=utcnow() - timedelta(days=10))
    db.session.add(home)
    db.session.commit()
    clear_domain_cache()
    return home


class TestTheTier:
    def test_it_is_hidden_free_and_no_donor_label(self, env):
        offer = complimentary_offer()
        assert (offer.provider_kind, offer.kind, offer.offer_group, offer.amount_minor, offer.active, offer.grants_donor) \
            == (KIND, 'subscription', KIND, 0, False, False)

    def test_there_is_one(self, env):
        assert complimentary_offer().id == complimentary_offer().id

    def test_it_is_never_offered(self, env):
        complimentary_offer().active = True   # even switched on by hand: nobody can buy it
        db.session.commit()
        offers_changed()
        with env.app.test_request_context():
            assert all(offer.provider_kind != KIND for offer in offers_here())

    def test_its_adapter_offers_nothing_and_cancels_nothing(self, env):
        adapter = adapter_for(KIND)
        assert not adapter.available() and not adapter.on_onion() and not adapter.prepaid
        assert adapter.cancel('anything') is None and adapter.cancel_at_period_end('anything') <= utcnow()

    @pytest.mark.parametrize('call', ['checkout', 'portal', 'price', 'credentials'])
    def test_what_only_a_provider_does_is_refused(self, env, call):
        adapter = adapter_for(KIND)
        attempts = {'checkout': lambda: adapter.checkout(env.tier, env.member, 'ok', 'no'),
                    'portal': lambda: adapter.billing_portal_url('cus', 'back'),
                    'price': lambda: adapter.describe_price('price'),
                    'credentials': lambda: adapter.check_credentials({})}
        with pytest.raises(ProviderError):
            attempts[call]()

    def test_its_adapter_reads_no_webhook(self, env):
        with pytest.raises(InvalidWebhook):
            adapter_for(KIND).parse_webhook(b'{}', {})


class TestGranting:
    def test_a_grant_never_ends(self, env):
        row = given(env.member)
        assert (row.provider_kind, row.offer_id, row.status) == (KIND, complimentary_offer().id, 'active')
        assert row.current_period_end is None and row.ends_at is None and row.renews_at is None
        assert complimentary_of(env.member) == row

    def test_granting_twice_keeps_one(self, env):
        assert given(env.member) == given(env.member)
        assert MemberSubscription.query.filter_by(user_id=env.member.id).count() == 1

    def test_it_clears_a_lapse(self, env):
        env.member.access_lapsed_at = utcnow() - timedelta(days=30)
        given(env.member)
        assert env.member.access_lapsed_at is None

    def test_ending_it_and_granting_again_makes_a_new_one(self, env):
        first = given(env.member)
        assert end_complimentary(env.member)
        db.session.commit()
        assert first.status == 'ended' and first.ended_at is not None and complimentary_of(env.member) is None
        assert given(env.member).id != first.id

    def test_ending_what_is_not_held_does_nothing(self, env):
        assert not end_complimentary(env.member)


class TestNewAccounts:
    def test_a_new_local_account_with_nothing_gets_it(self, env):
        account = make_user(env.baseline.instance_local, 'newcomer', local=True)
        finalize_user_setup(account)
        assert complimentary_of(account) is not None

    def test_one_that_paid_to_join_gets_none(self, env):
        account = make_user(env.baseline.instance_local, 'payer', local=True)
        paid_row(account, env.tier)
        finalize_user_setup(account)
        assert complimentary_of(account) is None

    def test_one_whose_subscription_already_ended_gets_none(self, env):
        account = make_user(env.baseline.instance_local, 'lapser', local=True)
        paid_row(account, env.tier, status='ended')
        grant_to_new_account(account)
        assert complimentary_of(account) is None

    def test_a_remote_account_gets_none(self, env):
        remote = make_user(env.baseline.instance_remote, 'faraway')
        grant_to_new_account(remote)
        assert complimentary_of(remote) is None


class TestItNeverLapses:
    def test_a_complimentary_member_is_never_lapsed(self, env):
        paid_domain(env)
        paid_row(env.member, env.tier, status='ended')
        given(env.member)
        refresh_access(env.member)
        assert env.member.access_lapsed_at is None

    def test_ending_it_leaves_the_membership_rules(self, env):
        paid_domain(env)
        paid_row(env.member, env.tier, status='ended')
        given(env.member)
        end_complimentary(env.member)
        refresh_access(env.member)
        assert env.member.access_lapsed_at is not None

    def test_the_daily_sweep_neither_ends_it_nor_warns(self, env, monkeypatch):
        row = given(env.member)
        mailed = []
        monkeypatch.setattr('app.payments.core.send_prepaid_reminder', lambda *args: mailed.append(args))
        assert remind_expiring_prepaid() == 0 and end_expired_prepaid() == 0
        assert row.status == 'active' and mailed == []

    def test_it_has_no_webhook(self, env):
        assert env.client.post('/payments/webhook/complimentary', data=b'{}').status_code == 404

    def test_deleting_the_account_ends_it(self, env):
        row = given(env.member)
        end_subscriptions_of(env.member)
        assert row.status == 'ended'


class TestNoPayment:
    def test_the_page_says_it_is_complimentary_and_offers_nothing(self, env):
        given(env.member)
        login(env.client, env.member)
        body = env.client.get('/donations').get_data(as_text=True)
        assert 'Your membership is complimentary' in body
        assert '/donations/checkout/' not in body and '/donations/redeem' not in body
        assert '/cancel' not in body

    def test_a_checkout_is_refused(self, env, monkeypatch):
        given(env.member)
        login(env.client, env.member)
        called = []
        monkeypatch.setattr('app.payments.adapters.stripe_adapter.StripeAdapter.checkout',
                            lambda *args, **kwargs: called.append(args) or 'https://checkout.example/s')
        for offer in (env.tier, env.tip):
            answer = post(env, f'/donations/checkout/{offer.id}')
            assert answer.status_code == 302 and answer.headers['Location'].endswith('/donations')
        assert called == []

    def test_a_code_is_refused(self, env):
        given(env.member)
        login(env.client, env.member)
        db.session.add(PromoCode(code='gift-month', kind='gift', offer_id=env.tier.id, periods=1))
        db.session.commit()
        post(env, '/donations/redeem', code='GIFT-MONTH')
        assert MemberSubscription.query.filter_by(user_id=env.member.id, provider_kind='gift').count() == 0

    def test_it_cannot_be_cancelled(self, env):
        row = given(env.member)
        login(env.client, env.member)
        assert post(env, f'/donations/subscriptions/{row.id}/cancel').status_code == 404
        assert row.status == 'active'

    def test_a_cms_button_says_so(self, env):
        given(env.member)
        login(env.client, env.member)
        body_md = f'[checkout {env.tier.id}] [checkout {env.tip.id}]'
        db.session.add(CmsPage(url='/subscriptions', title='Subscriptions', body=body_md, body_html=markdown_to_html(body_md)))
        db.session.commit()
        body = env.client.get('/subscriptions').get_data(as_text=True)
        assert '/donations/checkout/' not in body and body.count('Complimentary') >= 2

    def test_after_it_ends_they_can_pay(self, env, monkeypatch):
        given(env.member)
        end_complimentary(env.member)
        db.session.commit()
        login(env.client, env.member)
        monkeypatch.setattr('app.payments.adapters.stripe_adapter.StripeAdapter.checkout',
                            lambda *args, **kwargs: 'https://checkout.example/s')
        assert pay_link(post(env, f'/donations/checkout/{env.tier.id}')) == 'https://checkout.example/s'   # its summary (X4)


class TestAdmin:
    @pytest.fixture
    def treasurer(self, env):
        user = make_user(env.baseline.instance_local, 'treasurer', local=True)
        db.session.commit()
        grant_permission(user, 'manage monetization')
        grant_permission(user, 'administer all users')
        login(env.client, user)
        return user

    def test_the_edit_page_offers_a_grant(self, env, treasurer):
        body = env.client.get(f'/admin/user/{env.member.id}/edit').get_data(as_text=True)
        assert f'/admin/payments/complimentary/{env.member.id}' in body and 'Give complimentary membership' in body

    def test_the_edit_page_offers_to_end_one_held(self, env, treasurer):
        given(env.member)
        body = env.client.get(f'/admin/user/{env.member.id}/edit').get_data(as_text=True)
        assert 'End complimentary membership' in body

    def test_granting(self, env, treasurer):
        answer = post(env, f'/admin/payments/complimentary/{env.member.id}', grant='1')
        assert answer.status_code == 302 and complimentary_of(env.member) is not None

    def test_ending(self, env, treasurer):
        given(env.member)
        post(env, f'/admin/payments/complimentary/{env.member.id}', end='1')
        assert complimentary_of(env.member) is None

    def test_one_still_charged_by_a_provider_is_refused(self, env, treasurer):
        """Their card would go on being charged, which complimentary membership declines (K5): cancel it first."""
        paid_row(env.member, env.tier)
        post(env, f'/admin/payments/complimentary/{env.member.id}', grant='1')
        assert complimentary_of(env.member) is None

    def test_one_cancelled_to_run_out_may_have_it(self, env, treasurer):
        paid_row(env.member, env.tier).ends_at = utcnow() + timedelta(days=5)
        db.session.commit()
        post(env, f'/admin/payments/complimentary/{env.member.id}', grant='1')
        assert complimentary_of(env.member) is not None

    def test_one_with_a_prepaid_period_may_have_it(self, env, treasurer):
        db.session.add(MemberSubscription(user_id=env.member.id, offer_id=env.tier.id, provider_kind='btcpay',
                                          provider_reference='inv_1', status='active',
                                          current_period_end=utcnow() + timedelta(days=5)))
        db.session.commit()
        post(env, f'/admin/payments/complimentary/{env.member.id}', grant='1')
        assert complimentary_of(env.member) is not None

    def test_an_account_still_joining_is_refused(self, env, treasurer):
        env.member.registration_pending_since = utcnow()
        db.session.commit()
        post(env, f'/admin/payments/complimentary/{env.member.id}', grant='1')
        assert complimentary_of(env.member) is None

    def test_ending_what_is_not_held_changes_nothing(self, env, treasurer):
        answer = post(env, f'/admin/payments/complimentary/{env.member.id}', end='1')
        assert answer.status_code == 302 and complimentary_of(env.member) is None

    def test_a_post_pressing_neither_button_changes_nothing(self, env, treasurer):
        assert post(env, f'/admin/payments/complimentary/{env.member.id}').status_code == 302
        assert complimentary_of(env.member) is None

    def test_a_remote_account_is_refused(self, env, treasurer):
        remote = make_user(env.baseline.instance_remote, 'faraway')
        db.session.commit()
        assert post(env, f'/admin/payments/complimentary/{remote.id}', grant='1').status_code == 404
        assert complimentary_of(remote) is None

    def test_an_unknown_account_is_refused(self, env, treasurer):
        assert post(env, '/admin/payments/complimentary/999999', grant='1').status_code == 404

    def test_it_needs_manage_monetization(self, env):
        user = make_user(env.baseline.instance_local, 'moderator', local=True)
        db.session.commit()
        grant_permission(user, 'administer all users')
        login(env.client, user)
        post(env, f'/admin/payments/complimentary/{env.member.id}', grant='1')
        assert complimentary_of(env.member) is None
        assert 'Give complimentary membership' not in env.client.get(f'/admin/user/{env.member.id}/edit').get_data(as_text=True)

    def test_the_offer_is_not_listed_or_editable(self, env, treasurer):
        offer = complimentary_offer()
        db.session.commit()
        assert f'[checkout {offer.id}]' not in env.client.get('/admin/payments').get_data(as_text=True)
        assert env.client.get(f'/admin/payments/offers/{offer.id}/edit').status_code == 404

    def test_the_users_list_shows_it(self, env, treasurer):
        given(env.member)
        body = env.client.get('/admin/users?local_remote=local').get_data(as_text=True)
        assert 'Complimentary · no end date' in body


class TestAdminsHoldIt:
    """Every admin holds complimentary membership, kept so (plan 2026-11-16 X3)."""

    def test_being_made_an_admin_grants_it(self, env):
        actor = make_admin(make_user(env.baseline.instance_local, 'boss', local=True))
        role_grant(actor, env.member, admin_role())
        assert complimentary_of(env.member) is not None

    def test_another_role_does_not(self, env):
        actor = make_admin(make_user(env.baseline.instance_local, 'boss', local=True))
        role = Role(name='Helpers')
        db.session.add(role)
        db.session.commit()
        role_grant(actor, env.member, role)
        assert complimentary_of(env.member) is None

    def test_an_admins_cannot_be_ended(self, env):
        treasurer = make_user(env.baseline.instance_local, 'treasurer', local=True)
        db.session.commit()
        grant_permission(treasurer, 'manage monetization')
        make_admin(env.member)
        given(env.member)
        login(env.client, treasurer)
        post(env, f'/admin/payments/complimentary/{env.member.id}', end='1')
        assert complimentary_of(env.member) is not None


class TestStatus:
    def test_it_is_complimentary_with_no_date(self, env):
        given(env.member)
        status = subscription_statuses([env.member.id])[env.member.id]
        assert (status.title, status.state, status.date) == ('Complimentary', 'complimentary', None)


def load(name='complimentary_membership'):
    path = next(Path(__file__).parent.parent.glob(f'migrations/versions/*_{name}.py'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


class TestTheBackfill:
    @pytest.fixture
    def accounts(self, env):
        local = env.baseline.instance_local
        made = SimpleNamespace(
            plain=env.member,
            paying=make_user(local, 'paying', local=True),
            lapsed=make_user(local, 'lapsed', local=True),
            deleted=make_user(local, 'gone', local=True),
            pending=make_user(local, 'pending', local=True),
            unverified=make_user(local, 'unverified', local=True),
            remote=make_user(env.baseline.instance_remote, 'faraway'))
        made.deleted.deleted = True
        made.pending.registration_pending_since = utcnow()
        made.unverified.verified = False
        db.session.commit()
        paid_row(made.paying, env.tier)
        paid_row(made.lapsed, env.tier, status='ended', reference='sub_2')
        connection = db.session.connection()
        with Operations.context(MigrationContext.configure(connection)):
            load().downgrade()
            load().upgrade()
        return made

    def held(self):
        return set(db.session.execute(text("SELECT user_id FROM member_subscription WHERE provider_kind = 'complimentary' "
                                           "AND status = 'active' AND current_period_end IS NULL")).scalars())

    def test_every_set_up_local_account_without_one_gets_it(self, env, accounts):
        held = self.held()
        assert {accounts.plain.id, accounts.lapsed.id} <= held
        assert not {accounts.paying.id, accounts.deleted.id, accounts.pending.id, accounts.unverified.id,
                    accounts.remote.id} & held

    def test_its_offer_is_hidden(self, env, accounts):
        rows = db.session.execute(text("SELECT active, amount_minor, grants_donor FROM offer "
                                       "WHERE provider_kind = 'complimentary'")).fetchall()
        assert [tuple(row) for row in rows] == [(False, 0, False)]

    def test_running_it_again_grants_nothing_more(self, env, accounts):
        before = db.session.execute(text("SELECT count(*) FROM member_subscription")).scalar()
        with Operations.context(MigrationContext.configure(db.session.connection())):
            load().upgrade()
        assert db.session.execute(text("SELECT count(*) FROM member_subscription")).scalar() == before

    def test_admins_hold_it_after_the_admins_migration(self, env, accounts):
        """An admin who held a paid subscription missed the first backfill; X3's grants it (plan 2026-11-16)."""
        make_admin(accounts.paying)
        with Operations.context(MigrationContext.configure(db.session.connection())):
            load('admins_complimentary').upgrade()
        assert accounts.paying.id in self.held()
        assert not {accounts.remote.id, accounts.deleted.id} & self.held()

    def test_the_downgrade_takes_it_away(self, env, accounts):
        with Operations.context(MigrationContext.configure(db.session.connection())):
            load().downgrade()
        assert not self.held()
        assert db.session.execute(text("SELECT count(*) FROM offer WHERE provider_kind = 'complimentary'")).scalar() == 0
        assert db.session.execute(text("SELECT count(*) FROM member_subscription")).scalar() == 2
