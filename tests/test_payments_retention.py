"""Past retention, a settled payment keeps its amounts and loses its account (M6, plan P4)."""
from datetime import timedelta

import pytest

from app import db
from app.models import MemberSubscription, Offer, Payment, utcnow
from app.payments.retention import pseudonymise_settled
from app.utils import set_setting
from tests.factories import make_instance, make_user


@pytest.fixture
def member(app, db_session):
    return make_user(make_instance('local.example'), 'member', local=True)


def an_offer(kind='subscription', grants=False, days=None):
    offer = Offer(provider_kind='stripe', kind=kind, title='Plan', offer_group='plans', grants_donor=grants, donor_days=days)
    db.session.add(offer)
    db.session.commit()
    return offer


def a_payment(member, age_days=100, offer=None, status='paid', reference='in_1'):
    when = utcnow() - timedelta(days=age_days)
    payment = Payment(user_id=member.id, offer_id=offer.id if offer else None, provider_kind='stripe',
                      provider_reference=reference, amount_minor=500, currency='usd', status=status, created_at=when,
                      settled_at=when)
    db.session.add(payment)
    db.session.commit()
    return payment


def link_of(payment):
    db.session.expire_all()
    return db.session.get(Payment, payment.id).user_id


def test_no_retention_changes_nothing(member):
    payment = a_payment(member)
    assert pseudonymise_settled() == 0
    assert link_of(payment) == member.id


def test_past_retention_the_link_goes_and_the_amounts_stay(member):
    set_setting('payment_retention_days', 30)
    payment = a_payment(member)
    assert pseudonymise_settled() == 1
    assert link_of(payment) is None
    assert db.session.get(Payment, payment.id).amount_minor == 500


def test_younger_rows_stay(member):
    set_setting('payment_retention_days', 30)
    payment = a_payment(member, age_days=10)
    pseudonymise_settled()
    assert link_of(payment) == member.id


def test_a_row_paying_for_an_active_subscription_stays(member):
    set_setting('payment_retention_days', 30)
    offer = an_offer()
    db.session.add(MemberSubscription(user_id=member.id, offer_id=offer.id, provider_kind='stripe',
                                      provider_reference='sub_1', status='active'))
    db.session.commit()
    payment = a_payment(member, offer=offer)
    pseudonymise_settled()
    assert link_of(payment) == member.id


def test_a_row_for_an_ended_subscription_goes(member):
    set_setting('payment_retention_days', 30)
    offer = an_offer()
    db.session.add(MemberSubscription(user_id=member.id, offer_id=offer.id, provider_kind='stripe',
                                      provider_reference='sub_1', status='ended'))
    db.session.commit()
    payment = a_payment(member, offer=offer)
    pseudonymise_settled()
    assert link_of(payment) is None


@pytest.mark.parametrize('days, age, kept', [(None, 100, True), (365, 100, True), (30, 100, False)],
                         ids=['for_good', 'still_running', 'lapsed'])
def test_a_row_holding_up_a_label_stays_until_the_label_ends(member, days, age, kept):
    set_setting('payment_retention_days', 30)
    payment = a_payment(member, age_days=age, offer=an_offer(kind='donation', grants=True, days=days))
    pseudonymise_settled()
    assert (link_of(payment) == member.id) is kept


def test_a_refunded_row_holds_up_nothing(member):
    set_setting('payment_retention_days', 30)
    payment = a_payment(member, offer=an_offer(kind='donation', grants=True), status='refunded')
    pseudonymise_settled()
    assert link_of(payment) is None


def test_running_it_twice_is_harmless(member):
    set_setting('payment_retention_days', 30)
    a_payment(member)
    assert pseudonymise_settled() == 1
    assert pseudonymise_settled() == 0


def test_a_failure_rolls_back_and_is_raised(member, monkeypatch):
    set_setting('payment_retention_days', 30)
    monkeypatch.setattr('app.payments.retention.get_setting', lambda name: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        pseudonymise_settled()
