"""Gift codes (spec 2026-11-15-paid-membership-design.md PM3, PM6, PM8; plan phase 2, G1-G7)."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import MemberSubscription, Offer, PromoCode, PromoCodeUse, Site, utcnow
from app.payments.adapters import adapter_for
from app.payments.core import end_expired_prepaid
from app.payments.gifts import find_gift_code, start_gift
from tests.factories import make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    db.session.get(Site, 1).private_instance = False
    knight = Offer(provider_kind='btcpay', kind='subscription', title='Furcas', offer_group='tiers',
                   amount_minor=666, currency='usd', interval='month')
    other = Offer(provider_kind='btcpay', kind='subscription', title='Patron', offer_group='patrons',
                  amount_minor=900, currency='usd', interval='month')
    db.session.add_all([knight, other])
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    return SimpleNamespace(app=app, client=app.test_client(), knight=knight, other=other, member=member,
                           baseline=api_baseline)


def a_code(env, code='GIFT-MONTH', days=30, **values):
    row = PromoCode(code=code.lower(), kind='gift', offer_id=env.knight.id, days=days, **values)
    db.session.add(row)
    db.session.commit()
    return row


def held(env, user, ends_in_days, offer=None, reference='bt_1'):
    row = MemberSubscription(user_id=user.id, offer_id=(offer or env.knight).id, provider_kind='btcpay',
                             provider_reference=reference, status='active',
                             current_period_end=utcnow() + timedelta(days=ends_in_days))
    db.session.add(row)
    db.session.commit()
    return row


def gifts_of(user):
    return MemberSubscription.query.filter_by(user_id=user.id, provider_kind='gift').order_by(MemberSubscription.id).all()


class TestFindingACode:
    def test_it_is_read_without_case_or_spaces(self, env):
        code = a_code(env)
        assert find_gift_code('  gift-Month ') == code

    @pytest.mark.parametrize('values', [{'active': False}, {'expires_at': 'past'}, {'max_uses': 1}],
                             ids=['inactive', 'expired', 'used_up'])
    def test_one_that_cannot_be_used_is_not_found(self, env, values):
        if values.get('expires_at') == 'past':
            values = {'expires_at': utcnow() - timedelta(minutes=1)}
        code = a_code(env, **values)
        if 'max_uses' in values:
            db.session.add(PromoCodeUse(promo_code_id=code.id, user_id=make_user(env.baseline.instance_local, 'other',
                                                                                 local=True).id))
            db.session.commit()
        assert find_gift_code('GIFT-MONTH') is None

    def test_an_unknown_or_empty_code_is_not_found(self, env):
        a_code(env)
        assert find_gift_code('nope') is None and find_gift_code('') is None and find_gift_code(None) is None

    def test_a_discount_code_is_no_gift(self, env):
        db.session.add(PromoCode(code='tenoff', kind='percent', percent=10))
        db.session.commit()
        assert find_gift_code('TENOFF') is None


class TestAGift:
    def test_it_is_a_free_period_of_its_tier(self, env):
        before = utcnow()
        subscription = start_gift(env.member, a_code(env, days=30))
        assert (subscription.provider_kind, subscription.offer_id, subscription.status) == ('gift', env.knight.id, 'active')
        assert before + timedelta(days=30) <= subscription.current_period_end <= utcnow() + timedelta(days=30)

    def test_its_use_is_recorded(self, env):
        code = a_code(env)
        subscription = start_gift(env.member, code)
        use = PromoCodeUse.query.one()
        assert (use.promo_code_id, use.user_id, use.subscription_id) == (code.id, env.member.id, subscription.id)

    def test_it_starts_when_what_is_held_ends(self, env):
        """PM8: we never short anybody."""
        paid = held(env, env.member, ends_in_days=20)
        subscription = start_gift(env.member, a_code(env, days=30))
        assert subscription.current_period_end == paid.current_period_end + timedelta(days=30)

    def test_a_gift_on_a_gift_stacks(self, env):
        first = start_gift(env.member, a_code(env, 'FIRST', days=30))
        second = start_gift(env.member, a_code(env, 'SECOND', days=365))
        assert second.current_period_end == first.current_period_end + timedelta(days=365)

    def test_time_held_in_another_group_is_not_counted(self, env):
        held(env, env.member, ends_in_days=200, offer=env.other)
        subscription = start_gift(env.member, a_code(env, days=30))
        assert subscription.current_period_end <= utcnow() + timedelta(days=30)

    def test_one_account_uses_a_code_once(self, env):
        code = a_code(env, max_uses=None)
        assert start_gift(env.member, code) is not None
        assert start_gift(env.member, code) is None
        assert len(gifts_of(env.member)) == 1

    def test_it_ends_when_its_time_is_up(self, env):
        subscription = start_gift(env.member, a_code(env))
        subscription.current_period_end = utcnow() - timedelta(seconds=1)
        db.session.commit()
        end_expired_prepaid()
        assert db.session.get(MemberSubscription, subscription.id).status == 'ended'


class TestTheGiftProvider:
    def test_it_is_prepaid_and_never_offered(self, env):
        gift = adapter_for('gift')
        assert gift.prepaid and not gift.available() and not gift.on_onion()

    def test_cancelling_a_gift_asks_nobody(self, env):
        assert adapter_for('gift').cancel('gift:anything') is None

    def test_it_has_no_webhook(self, env):
        assert env.client.post('/payments/webhook/gift', data=b'{}').status_code == 404


class TestUseIsCheckedAtTheMomentOfUse:
    def test_a_code_used_up_since_it_was_found_gives_nothing(self, env):
        code = a_code(env, max_uses=1)
        other = make_user(env.baseline.instance_local, 'quicker', local=True)
        start_gift(other, code)
        assert start_gift(env.member, code) is None and gifts_of(env.member) == []

    def test_a_code_deactivated_since_gives_nothing(self, env):
        code = a_code(env)
        code.active = False
        db.session.commit()
        assert start_gift(env.member, code) is None
