"""Core turns neutral events into the ledger, subscriptions and the Donor label (plan phase 1, Task 4)."""
from datetime import timedelta
from unittest.mock import patch

import pytest

from app import db
from app.constants import DONOR_WHILE_SUBSCRIBED
from app.models import MemberSubscription, Offer, Payment, PaymentEvent, utcnow
from app.payments.core import apply_event, holds_offer, refresh_donor
from app.payments.events import CustomerKnown, Paid, Refunded, SubscriptionEnded, SubscriptionStarted
from tests.factories import make_instance, make_user


@pytest.fixture
def member(app, db_session):
    return make_user(make_instance('local.example'), 'member', local=True)


@pytest.fixture(autouse=True)
def cancelled():
    with patch('app.payments.core.cancel_at_provider') as cancel:
        yield cancel


def an_offer(kind='subscription', group='tiers', amount=500, grants=True, days=None, title='A plan'):
    offer = Offer(provider_kind='stripe', kind=kind, title=title, offer_group=group, provider_price_id='price',
                  amount_minor=amount, currency='usd', grants_donor=grants, donor_days=days)
    db.session.add(offer)
    db.session.commit()
    return offer


def apply(event_id, *events):
    return apply_event('stripe', event_id, list(events))


def paid(member, offer, reference='in_1', amount=500, currency='usd'):
    return Paid(reference, amount, currency, member.id, offer.id)


def started(member, offer, reference='sub_1'):
    return SubscriptionStarted(reference, member.id, offer.id)


class TestTheLedger:
    def test_a_payment_makes_one_row(self, member):
        offer = an_offer()
        assert apply('evt_1', paid(member, offer)) is True
        row = Payment.query.one()
        assert (row.user_id, row.offer_id, row.provider_kind, row.provider_reference, row.amount_minor, row.currency,
                row.status) == (member.id, offer.id, 'stripe', 'in_1', 500, 'usd', 'paid')
        assert row.settled_at is not None

    def test_a_repeated_event_does_nothing(self, member):
        offer = an_offer()
        apply('evt_1', paid(member, offer))
        assert apply('evt_1', paid(member, offer, reference='in_2')) is False
        assert Payment.query.count() == 1 and PaymentEvent.query.count() == 1

    def test_two_events_about_one_payment_make_one_row(self, member):
        """A checkout and its first invoice both report the first month."""
        offer = an_offer()
        apply('evt_session', paid(member, offer))
        apply('evt_invoice', paid(member, offer))
        assert Payment.query.count() == 1

    @pytest.mark.parametrize('amount, currency', [(499, 'usd'), (500, 'eur')], ids=['amount', 'currency'])
    def test_a_payment_unlike_its_offer_is_recorded_and_grants_nothing(self, member, amount, currency):
        offer = an_offer()
        apply('evt_1', paid(member, offer, amount=amount, currency=currency), started(member, offer))
        assert Payment.query.one().status == 'mismatch'
        assert MemberSubscription.query.one().status == 'mismatch'
        assert member.donor_until is None

    def test_a_mismatch_replaces_nothing(self, member, cancelled):
        offer = an_offer()
        apply('evt_1', started(member, offer, reference='sub_old'))
        apply('evt_2', paid(member, offer, amount=1), started(member, offer, reference='sub_new'))
        assert not cancelled.called
        assert MemberSubscription.query.filter_by(provider_reference='sub_old').one().status == 'active'

    def test_currency_case_does_not_matter(self, member):
        offer = an_offer()
        apply('evt_1', paid(member, offer, currency='USD'))
        row = Payment.query.one()
        assert (row.status, row.currency) == ('paid', 'usd')

    def test_an_offer_with_no_amount_is_not_checked(self, member):
        apply('evt_1', paid(member, an_offer(amount=None), amount=123))
        assert Payment.query.one().status == 'paid'

    @pytest.mark.parametrize('user_id', [None, 0, -1, 2**40, 987654, 'x', True],
                             ids=['none', 'zero', 'negative', 'huge', 'unknown', 'text', 'bool'])
    def test_an_account_that_names_nobody_is_recorded_without_one(self, app, db_session, user_id):
        apply('evt_1', Paid('in_1', 500, 'usd', user_id, None), SubscriptionStarted('sub_1', user_id, None))
        assert Payment.query.one().user_id is None
        assert MemberSubscription.query.count() == 0

    def test_an_offer_that_names_nothing_is_recorded_without_one(self, member):
        apply('evt_1', Paid('in_1', 500, 'usd', member.id, 987654))
        row = Payment.query.one()
        assert row.offer_id is None and row.status == 'paid'

    def test_a_failure_leaves_nothing_behind_and_can_be_retried(self, member):
        offer = an_offer()
        with patch('app.payments.core.refresh_donor', side_effect=RuntimeError):
            with pytest.raises(RuntimeError):
                apply('evt_1', paid(member, offer))
        assert Payment.query.count() == 0 and PaymentEvent.query.count() == 0
        assert apply('evt_1', paid(member, offer)) is True


class TestRefunds:
    def test_a_refund_marks_its_payment(self, member):
        apply('evt_1', paid(member, an_offer()))
        apply('evt_2', Refunded('in_1'))
        assert Payment.query.one().status == 'refunded'

    def test_a_refund_of_something_unknown_does_nothing(self, app, db_session):
        assert apply('evt_1', Refunded('in_unknown')) is True
        assert Payment.query.count() == 0

    def test_a_refund_of_a_payment_with_no_account(self, app, db_session):
        apply('evt_1', Paid('in_1', 500, 'usd'))
        apply('evt_2', Refunded('in_1'))
        assert Payment.query.one().status == 'refunded'


class TestGroups:
    def test_a_plan_replaces_another_in_its_group(self, member, cancelled):
        small, big = an_offer(title='small'), an_offer(title='big', amount=1000)
        apply('evt_1', started(member, small, reference='sub_small'))
        apply('evt_2', started(member, big, reference='sub_big'))
        assert MemberSubscription.query.filter_by(provider_reference='sub_small').one().status == 'ended'
        assert MemberSubscription.query.filter_by(provider_reference='sub_big').one().status == 'active'
        cancelled.assert_called_once_with('stripe', 'sub_small')

    def test_plans_in_different_groups_run_together(self, member, cancelled):
        apply('evt_1', started(member, an_offer(group='one'), reference='sub_1'))
        apply('evt_2', started(member, an_offer(group='two'), reference='sub_2'))
        assert MemberSubscription.query.filter_by(status='active').count() == 2
        assert not cancelled.called

    def test_a_migrated_subscription_is_in_the_legacy_group(self, member, cancelled):
        db.session.add(MemberSubscription(user_id=member.id, provider_kind='stripe', provider_reference='sub_old',
                                          status='active'))
        db.session.commit()
        apply('evt_1', started(member, an_offer(group='legacy'), reference='sub_new'))
        cancelled.assert_called_once_with('stripe', 'sub_old')

    def test_a_migrated_subscription_stays_beside_another_group(self, member, cancelled):
        db.session.add(MemberSubscription(user_id=member.id, provider_kind='stripe', provider_reference='sub_old',
                                          status='active'))
        db.session.commit()
        apply('evt_1', started(member, an_offer(group='tiers'), reference='sub_new'))
        assert not cancelled.called

    def test_another_members_plan_is_left_alone(self, member, cancelled):
        other = make_user(make_instance('other.example'), 'other', local=True)
        offer = an_offer()
        apply('evt_1', started(other, offer, reference='sub_theirs'))
        apply('evt_2', started(member, offer, reference='sub_mine'))
        assert not cancelled.called

    def test_nothing_is_cancelled_when_the_transaction_fails(self, member, cancelled):
        offer = an_offer()
        apply('evt_1', started(member, offer, reference='sub_old'))
        with patch('app.payments.core.refresh_donor', side_effect=RuntimeError), pytest.raises(RuntimeError):
            apply('evt_2', started(member, offer, reference='sub_new'))
        assert not cancelled.called


class TestEndings:
    def test_an_ending_ends_it(self, member):
        apply('evt_1', started(member, an_offer()))
        apply('evt_2', SubscriptionEnded('sub_1'))
        row = MemberSubscription.query.one()
        assert row.status == 'ended' and row.ended_at is not None

    def test_an_ending_of_something_unknown_does_nothing(self, app, db_session):
        assert apply('evt_1', SubscriptionEnded('sub_unknown')) is True
        assert MemberSubscription.query.count() == 0

    def test_a_subscription_starting_twice_stays_one_row(self, member):
        offer = an_offer()
        apply('evt_1', started(member, offer))
        apply('evt_2', started(member, offer))
        assert MemberSubscription.query.count() == 1


class TestTheLabel:
    def test_a_subscription_grants_it_while_active(self, member):
        apply('evt_1', started(member, an_offer()))
        assert member.donor_until == DONOR_WHILE_SUBSCRIBED and member.is_donor()
        apply('evt_2', SubscriptionEnded('sub_1'))
        assert member.donor_until is None and not member.is_donor()

    def test_a_migrated_subscription_grants_it(self, member):
        db.session.add(MemberSubscription(user_id=member.id, provider_kind='stripe', provider_reference='sub_old',
                                          status='active'))
        db.session.commit()
        refresh_donor(member)
        assert member.is_donor()

    def test_an_offer_that_does_not_grant_it_does_not(self, member):
        apply('evt_1', started(member, an_offer(grants=False)))
        assert member.donor_until is None

    def test_a_one_off_grants_it_for_its_length(self, member):
        apply('evt_1', paid(member, an_offer(kind='donation', days=30)))
        assert timedelta(days=29) < member.donor_until - utcnow() <= timedelta(days=30)
        assert member.is_donor()

    def test_a_one_off_with_no_length_grants_it_for_good(self, member):
        apply('evt_1', paid(member, an_offer(kind='donation')))
        assert member.donor_until == DONOR_WHILE_SUBSCRIBED

    def test_the_longest_one_off_counts(self, member):
        short, long_ = an_offer(kind='donation', days=1), an_offer(kind='donation', days=90)
        apply('evt_1', paid(member, long_, reference='pi_long'))
        apply('evt_2', paid(member, short, reference='pi_short'))
        assert member.donor_until - utcnow() > timedelta(days=89)

    def test_a_lapsed_one_off_is_no_longer_shown(self, member):
        apply('evt_1', paid(member, an_offer(kind='donation', days=30)))
        Payment.query.one().created_at = utcnow() - timedelta(days=31)
        db.session.commit()
        refresh_donor(member)
        assert not member.is_donor()

    def test_a_refund_takes_it_back(self, member):
        apply('evt_1', paid(member, an_offer(kind='donation')))
        apply('evt_2', Refunded('in_1'))
        assert member.donor_until is None

    def test_a_subscriptions_payment_alone_does_not_grant_it_after_it_ends(self, member):
        offer = an_offer()
        apply('evt_1', paid(member, offer), started(member, offer))
        apply('evt_2', SubscriptionEnded('sub_1'))
        assert member.donor_until is None


class TestCustomers:
    def test_a_customer_is_remembered(self, member):
        apply('evt_1', CustomerKnown(member.id, 'cus_1'))
        assert member.stripe_customer_id == 'cus_1'

    def test_a_customer_for_nobody_is_ignored(self, app, db_session):
        assert apply('evt_1', CustomerKnown(987654, 'cus_1')) is True


class TestHoldsOffer:
    def test_a_subscription_is_held_while_active(self, member):
        offer = an_offer()
        assert not holds_offer(member, offer)
        apply('evt_1', started(member, offer))
        assert holds_offer(member, offer)
        apply('evt_2', SubscriptionEnded('sub_1'))
        assert not holds_offer(member, offer)

    def test_a_one_off_is_held_once_paid_until_refunded(self, member):
        offer = an_offer(kind='donation')
        assert not holds_offer(member, offer)
        apply('evt_1', paid(member, offer))
        assert holds_offer(member, offer)
        apply('evt_2', Refunded('in_1'))
        assert not holds_offer(member, offer)
