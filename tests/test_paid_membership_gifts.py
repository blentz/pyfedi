"""Gift codes (spec 2026-11-15-paid-membership-design.md PM3, PM6, PM8; plan phase 2, G1-G7)."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import MemberSubscription, Offer, PromoCode, PromoCodeUse, Site, utcnow
from app.payments.adapters import adapter_for
from app.payments.adapters.base import InvalidWebhook, ProviderError
from app.payments.offers import period_text
from app.payments.gifts import referral_gift_code
from app.payments.holding import first_charge_after
from app.payments.core import end_expired_prepaid, remind_expiring_prepaid
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


def a_code(env, code='GIFT-MONTH', periods=1, **values):
    row = PromoCode(code=code.lower(), kind='gift', offer_id=env.knight.id, periods=periods, **values)
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

    @pytest.mark.parametrize('values', [{'offer_id': None}, {'periods': None}, {'periods': 0}], ids=['no_tier', 'no_units', 'zero_units'])
    def test_a_gift_code_with_no_tier_or_length_is_not_found(self, env, values):
        row = a_code(env)
        for name, value in values.items():
            setattr(row, name, value)
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
        subscription = start_gift(env.member, a_code(env, periods=1))
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
        subscription = start_gift(env.member, a_code(env, periods=1))
        assert subscription.current_period_end == paid.current_period_end + timedelta(days=30)

    def test_a_gift_on_a_gift_stacks(self, env):
        first = start_gift(env.member, a_code(env, 'FIRST', periods=1))
        second = start_gift(env.member, a_code(env, 'SECOND', periods=12))
        assert second.current_period_end == first.current_period_end + timedelta(days=360)

    def test_time_held_in_another_group_is_not_counted(self, env):
        held(env, env.member, ends_in_days=200, offer=env.other)
        subscription = start_gift(env.member, a_code(env, periods=1))
        assert subscription.current_period_end <= utcnow() + timedelta(days=30)

    def test_one_account_uses_a_code_once(self, env):
        code = a_code(env, max_uses=None)
        assert start_gift(env.member, code) is not None
        assert start_gift(env.member, code) is None
        assert len(gifts_of(env.member)) == 1

    def test_its_end_is_mentioned_once_a_day_beforehand(self, env):
        """A gift is prepaid-like (G3): the member is told before it runs out, as for a paid period."""
        subscription = start_gift(env.member, a_code(env))
        subscription.current_period_end = utcnow() + timedelta(days=3)   # three days of its month left
        db.session.commit()
        with patch('app.payments.core.send_prepaid_reminder') as mail:
            assert remind_expiring_prepaid() == 1 and remind_expiring_prepaid() == 0
        mail.assert_called_once_with(env.member, 'Furcas', subscription.current_period_end,
                                     (subscription.current_period_end.date() - utcnow().date()).days)

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


class TestNothingElseIsAskedOfAGift:
    @pytest.mark.parametrize('call', [
        lambda gift, env: gift.checkout(env.knight, env.member, 'ok', 'no'),
        lambda gift, env: gift.billing_portal_url('cus', 'back'),
        lambda gift, env: gift.describe_price('price'),
        lambda gift, env: gift.check_credentials({}),
    ], ids=['checkout', 'portal', 'price', 'credentials'])
    def test_what_only_a_provider_does_is_refused(self, env, call):
        with pytest.raises(ProviderError):
            call(adapter_for('gift'), env)

    def test_it_has_no_webhook_to_read(self, env):
        with pytest.raises(InvalidWebhook):
            adapter_for('gift').parse_webhook(b'{}', {})

    def test_cancelling_one_ends_it_now(self, env):
        assert adapter_for('gift').cancel_at_period_end('gift:x') <= utcnow()


class TestEdges:
    def test_a_member_with_no_referral_code_has_no_row(self, env):
        assert referral_gift_code(env.member) is None

    def test_a_guest_or_a_one_off_is_charged_now(self, env):
        gift = Offer(provider_kind='btcpay', kind='donation', title='Tip', offer_group='tips', amount_minor=300,
                     currency='usd')
        db.session.add(gift)
        db.session.commit()
        assert first_charge_after(None, env.knight) is None and first_charge_after(env.member, gift) is None


class TestAGiftIsWholePeriods:
    """Owner 2026-11-17: a gift is a number of its tier's billing periods, each what one payment buys."""

    def test_months_are_thirty_days_each(self, env):
        before = utcnow()
        subscription = start_gift(env.member, a_code(env, periods=3))
        assert before + timedelta(days=90) <= subscription.current_period_end <= utcnow() + timedelta(days=90)

    def test_years_are_a_year_each(self, env):
        env.knight.interval = 'year'
        db.session.commit()
        before = utcnow()
        subscription = start_gift(env.member, a_code(env, periods=2))
        assert before + timedelta(days=730) <= subscription.current_period_end <= utcnow() + timedelta(days=730)


@pytest.mark.parametrize('count, interval, text', [(1, 'month', '1 month'), (3, 'month', '3 months'),
                                                   (1, 'year', '1 year'), (2, 'year', '2 years'), (2, None, '2 months')],
                         ids=['a-month', 'months', 'a-year', 'years', 'no-interval'])
def test_a_gifts_length_in_words(app, count, interval, text):
    with app.test_request_context():
        assert period_text(count, interval) == text
