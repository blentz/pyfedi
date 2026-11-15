"""Discount codes (spec 2026-11-15-paid-membership-design.md PM6, Q2, Q3; plan phase 3, D1-D8)."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Offer, Payment, PromoCode, PromoCodeUse, Site, utcnow
from app.payments.core import apply_event
from app.payments.discounts import discounted, find_discount_code, reserve
from app.payments.events import Paid
from tests.factories import make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    db.session.get(Site, 1).private_instance = False
    knight = Offer(provider_kind='btcpay', kind='subscription', title='Furcas', offer_group='tiers',
                   amount_minor=666, currency='usd', interval='month')
    duke = Offer(provider_kind='btcpay', kind='subscription', title='Astaroth', offer_group='tiers',
                 amount_minor=1666, currency='usd', interval='month')
    paypal = Offer(provider_kind='paypal', kind='subscription', title='Furcas (PayPal)', offer_group='tiers',
                   provider_price_id='P-1', amount_minor=666, currency='usd', interval='month')
    db.session.add_all([knight, duke, paypal])
    member = make_user(api_baseline.instance_local, 'member', local=True)
    db.session.commit()
    return SimpleNamespace(app=app, knight=knight, duke=duke, paypal=paypal, member=member, baseline=api_baseline)


@pytest.fixture(autouse=True)
def cancelled():
    with patch('app.payments.core.cancel_at_provider'):
        yield


def a_code(code='spring-twenty', kind='percent', **values):
    values = {'percent': 20} if kind == 'percent' and not values else values
    row = PromoCode(code=code, kind=kind, **values)
    db.session.add(row)
    db.session.commit()
    return row


class TestThePrice:
    def test_percent_off(self, env):
        assert discounted(a_code(percent=20), env.knight) == 532   # rounded down: the member's way

    def test_an_amount_off(self, env):
        assert discounted(a_code('five-off', kind='amount', amount_minor=166), env.knight) == 500

    def test_a_code_for_one_offer_is_for_that_offer_alone(self, env):
        code = a_code(percent=20, offer_id=env.duke.id)
        assert discounted(code, env.knight) is None and discounted(code, env.duke) == 1332

    def test_paypal_takes_no_codes(self, env):
        assert discounted(a_code(percent=20), env.paypal) is None

    @pytest.mark.parametrize('amount', [666, 700], ids=['all_of_it', 'more'])
    def test_free_is_for_gifts_not_discounts(self, env, amount):
        assert discounted(a_code('too-much', kind='amount', amount_minor=amount), env.knight) is None

    def test_a_gift_is_no_discount(self, env):
        assert discounted(a_code('a-gift', kind='gift', offer_id=env.knight.id, days=30), env.knight) is None


class TestFinding:
    def test_without_case_or_spaces(self, env):
        code = a_code(percent=20)
        assert find_discount_code(' Spring-TWENTY ') == code

    def test_a_gift_code_is_not_a_discount(self, env):
        a_code('a-gift', kind='gift', offer_id=env.knight.id, days=30)
        assert find_discount_code('a-gift') is None

    @pytest.mark.parametrize('values', [{'active': False}, {'expires_at': 'past'}], ids=['inactive', 'expired'])
    def test_one_that_cannot_be_used(self, env, values):
        if values.get('expires_at') == 'past':
            values = {'expires_at': utcnow() - timedelta(minutes=1)}
        a_code(percent=20, **values)
        assert find_discount_code('spring-twenty') is None

    def test_a_members_referral_code_is_never_a_discount(self, env):
        a_code('member-code', percent=20, owner_id=env.member.id)
        assert find_discount_code('member-code') is None


class TestReserving:
    def test_it_holds_the_price_expected(self, env):
        code = a_code(percent=20)
        use = reserve(env.member, code, env.knight)
        assert (use.promo_code_id, use.user_id, use.offer_id, use.expected_minor, use.payment_id) == (
            code.id, env.member.id, env.knight.id, 532, None)

    def test_reserving_again_before_paying_is_the_same_hold(self, env):
        code = a_code(percent=20)
        first = reserve(env.member, code, env.knight)
        again = reserve(env.member, code, env.duke)
        assert again.id == first.id and (again.offer_id, again.expected_minor) == (env.duke.id, 1332)
        assert PromoCodeUse.query.count() == 1

    def test_a_code_paid_with_once_is_not_reserved_again(self, env):
        code = a_code(percent=20)
        use = reserve(env.member, code, env.knight)
        db.session.add(Payment(user_id=env.member.id, provider_kind='btcpay', provider_reference='inv2',
                               amount_minor=532, currency='usd', status='paid'))
        db.session.flush()
        use.payment_id = Payment.query.filter_by(provider_reference='inv2').one().id
        db.session.commit()
        assert reserve(env.member, code, env.knight) is None

    def test_held_uses_count_towards_max_uses(self, env):
        code = a_code(percent=20, max_uses=1)
        reserve(env.member, code, env.knight)
        other = make_user(env.baseline.instance_local, 'other', local=True)
        assert reserve(other, code, env.knight) is None and find_discount_code('spring-twenty') is None

    def test_a_hold_left_a_day_frees_its_use(self, env):
        code = a_code(percent=20, max_uses=1)
        use = reserve(env.member, code, env.knight)
        use.used_at = utcnow() - timedelta(days=1, minutes=1)
        db.session.commit()
        other = make_user(env.baseline.instance_local, 'other', local=True)
        assert reserve(other, code, env.knight) is not None

    def test_one_that_does_not_apply_is_not_reserved(self, env):
        assert reserve(env.member, a_code(percent=20), env.paypal) is None


class TestTheLedger:
    """A payment at the discounted price is no mismatch when it was reserved (D5)."""

    def pay(self, env, amount, reference='inv_1', offer=None):
        apply_event('btcpay', f'evt_{reference}', [Paid(reference, amount, 'usd', env.member.id, (offer or env.knight).id)])
        return Payment.query.filter_by(provider_reference=reference).one()

    def test_the_reserved_price_is_paid(self, env):
        use = reserve(env.member, a_code(percent=20), env.knight)
        payment = self.pay(env, 532)
        assert payment.status == 'paid' and db.session.get(PromoCodeUse, use.id).payment_id == payment.id

    def test_without_a_reservation_it_is_a_mismatch(self, env):
        assert self.pay(env, 532).status == 'mismatch'

    def test_another_amount_is_a_mismatch(self, env):
        reserve(env.member, a_code(percent=20), env.knight)
        assert self.pay(env, 400).status == 'mismatch'

    def test_a_reservation_for_another_offer_does_not_cover_this_one(self, env):
        reserve(env.member, a_code(percent=20), env.duke)
        assert self.pay(env, 532).status == 'mismatch'

    def test_the_full_price_still_matches_and_leaves_the_reservation(self, env):
        use = reserve(env.member, a_code(percent=20), env.knight)
        assert self.pay(env, 666).status == 'paid' and db.session.get(PromoCodeUse, use.id).payment_id is None
