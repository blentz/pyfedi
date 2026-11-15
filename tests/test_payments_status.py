"""What /admin/users says about each local member's subscription (app/payments/status.py)."""
from datetime import datetime, timedelta

import pytest

from app import db
from app.models import MemberSubscription, Offer, Payment, User
from app.payments.status import subscription_filter, subscription_statuses
from tests.factories import make_instance, make_user

NOW = datetime(2026, 11, 15, 12, 0, 0)


@pytest.fixture
def instance(app, db_session):
    return make_instance('local.example')


@pytest.fixture
def member(instance):
    return make_user(instance, 'member', local=True)


def an_offer(title='Furcas', kind='btcpay'):
    offer = Offer(provider_kind=kind, kind='subscription', title=title, offer_group='tiers', amount_minor=666,
                  currency='usd', interval='month')
    db.session.add(offer)
    db.session.commit()
    return offer


def holds(member, offer=None, reference='r1', status='active', created=NOW - timedelta(days=20), **values):
    row = MemberSubscription(user_id=member.id, offer_id=offer.id if offer else None, provider_kind='stripe',
                             provider_reference=reference, status=status, created_at=created, **values)
    db.session.add(row)
    db.session.commit()
    return row


def status_of(member):
    return subscription_statuses([member.id]).get(member.id)


class TestWhatItSays:
    def test_never_subscribed_says_nothing(self, member):
        assert status_of(member) is None

    def test_a_prepaid_period_says_until_when(self, member):
        holds(member, an_offer(), current_period_end=NOW + timedelta(days=10))
        status = status_of(member)
        assert (status.title, status.state, status.date, status.since) == (
            'Furcas', 'paid_until', NOW + timedelta(days=10), NOW - timedelta(days=20))

    def test_one_that_renews_says_when(self, member):
        holds(member, an_offer(), renews_at=NOW + timedelta(days=5))
        assert (status_of(member).state, status_of(member).date) == ('renews', NOW + timedelta(days=5))

    def test_one_that_renews_with_no_date_yet_says_when_it_was_last_paid(self, member):
        offer = an_offer()
        holds(member, offer)
        for days, status in ((40, 'paid'), (10, 'paid'), (3, 'refunded')):
            db.session.add(Payment(user_id=member.id, offer_id=offer.id, provider_kind='stripe',
                                   provider_reference=f'in_{days}', amount_minor=666, currency='usd', status=status,
                                   created_at=NOW - timedelta(days=days)))
        db.session.commit()
        status = status_of(member)
        assert (status.state, status.date, status.last_paid) == ('renews', None, NOW - timedelta(days=10))

    def test_one_that_renews_with_nothing_paid_yet(self, member):
        holds(member, an_offer())
        assert (status_of(member).state, status_of(member).date, status_of(member).last_paid) == ('renews', None, None)

    def test_a_cancelled_one_says_when_it_ends(self, member):
        holds(member, an_offer(), ends_at=NOW + timedelta(days=7), renews_at=NOW + timedelta(days=7))
        assert (status_of(member).state, status_of(member).date) == ('cancelled', NOW + timedelta(days=7))

    def test_a_payment_unlike_its_offer_is_called_out(self, member):
        holds(member, an_offer(), status='mismatch')
        assert status_of(member).state == 'mismatch'

    def test_an_ended_one_says_when_it_lapsed(self, member):
        holds(member, an_offer('Old'), reference='r1', status='ended', ended_at=NOW - timedelta(days=30))
        holds(member, an_offer('Newer'), reference='r2', status='ended', ended_at=NOW - timedelta(days=2))
        status = status_of(member)
        assert (status.title, status.state, status.date) == ('Newer', 'lapsed', NOW - timedelta(days=2))

    def test_what_is_held_now_wins_over_what_ended(self, member):
        holds(member, an_offer('Old'), reference='r1', status='ended', ended_at=NOW - timedelta(days=1))
        holds(member, an_offer('Now'), reference='r2', current_period_end=NOW + timedelta(days=9))
        assert (status_of(member).title, status_of(member).state) == ('Now', 'paid_until')

    def test_one_that_ended_later_doesnt_displace_one_held(self, member):
        """Rows come in the order they were made: one made after what is held, and since ended, is passed over."""
        holds(member, an_offer('Now'), reference='r1', renews_at=NOW)
        holds(member, an_offer('Brief'), reference='r2', status='ended', created=NOW, ended_at=NOW)
        assert (status_of(member).title, status_of(member).state) == ('Now', 'renews')

    def test_the_newest_of_two_held_at_once_wins(self, member):
        holds(member, an_offer('First'), reference='r1', created=NOW - timedelta(days=50), renews_at=NOW)
        holds(member, an_offer('Second'), reference='r2', created=NOW - timedelta(days=5), renews_at=NOW)
        assert status_of(member).title == 'Second'

    def test_a_held_one_wins_over_a_mismatch(self, member):
        holds(member, an_offer('Odd'), reference='r1', status='mismatch', created=NOW)
        holds(member, an_offer('Good'), reference='r2', created=NOW - timedelta(days=9), renews_at=NOW)
        assert status_of(member).title == 'Good'

    def test_one_migrated_with_no_offer_has_no_title(self, member):
        holds(member, None, renews_at=NOW)
        assert status_of(member).title is None

    def test_many_members_at_once(self, instance, member):
        other = make_user(instance, 'other', local=True)
        holds(member, an_offer(), reference='r1', renews_at=NOW)
        holds(other, an_offer('Bael'), reference='r2', status='ended', ended_at=NOW)
        statuses = subscription_statuses([member.id, other.id, 999999])
        assert {key: value.state for key, value in statuses.items()} == {member.id: 'renews', other.id: 'lapsed'}

    def test_no_members_asks_nothing(self, app, db_session):
        assert subscription_statuses([]) == {}


class TestTheFilter:
    @pytest.fixture
    def people(self, instance):
        active, lapsed, never, mismatch = (make_user(instance, name, local=True)
                                           for name in ('active', 'lapsed', 'never', 'mismatch'))
        remote = make_user(make_instance('remote.example'), 'remote')
        offer = an_offer()
        holds(active, offer, reference='r1', renews_at=NOW)
        holds(active, offer, reference='r0', status='ended', ended_at=NOW)   # an earlier one, which doesn't make it lapsed
        holds(lapsed, offer, reference='r2', status='ended', ended_at=NOW)
        holds(mismatch, offer, reference='r3', status='mismatch')
        return {'active': active, 'lapsed': lapsed, 'never': never, 'mismatch': mismatch, 'remote': remote}

    @pytest.mark.parametrize('choice, expected', [('active', {'active'}), ('lapsed', {'lapsed'}), ('never', {'never'})],
                             ids=['active', 'lapsed', 'never'])
    def test_it_picks_who(self, people, choice, expected):
        ids = {user.id for user in User.query.filter(subscription_filter(choice))}
        assert ids == {people[name].id for name in expected}

    @pytest.mark.parametrize('choice', ['', 'everyone', None], ids=['empty', 'unknown', 'none'])
    def test_anything_else_filters_nothing(self, people, choice):
        assert subscription_filter(choice) is None
