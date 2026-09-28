"""`awaken_dormant_instance` and the timestamp it shares with the maintenance
task.

Called from the inbox (`app/activitypub/routes.py:1989`) when something arrives
from an instance we had given up talking to. It had no tests: measured over the
whole suite, the only executed line in it was its `def`.

D1380. `start_trying_again` is meaningful only while the instance is dormant --
it is the time the wait ends -- and waking the instance left the value behind, so
it outlived the dormancy that produced it. Two readers then believed it.

    after update_dormant_gone   dormant=True  sta=None
    after awaken (sets sta)     dormant=True  sta=2026-09-28 21:40:31
    (the wait expires)
    after awaken (wakes)        dormant=False sta=2026-09-28 21:39:05   <-- stale
    after update_dormant_gone   dormant=True  sta=2026-09-28 21:39:05
    after awaken                dormant=False sta=2026-09-28 21:39:05   <-- no wait

First reader: this function. `Instance.update_dormant_gone` sets `dormant = True`
without setting the timestamp -- it is reached from `get_request_instance` on any
failed fetch -- so on the second dormancy the `else` arm ran against a value
already in the past and woke the instance immediately. The backoff applied once
per instance, ever.

Second reader, and the worse one:
`app/shared/tasks/maintenance.py:435` gives up on
`dormant == True, start_trying_again < five_days_ago`. A stale value is older than
five days by definition, so an instance that was dormant once months ago and has a
brief failure spell now is marked `gone_forever` on the next maintenance pass --
never delivered to again -- without having waited five days for anything.
Measured: the query matches.

THE INVARIANT THIS FILE PINS. `start_trying_again` is non-null only while
`dormant` is true. Every row below is a consequence of that one sentence, which is
why the fix is one line rather than a guard at each reader.
"""
from datetime import timedelta

import pytest

from app import db
from app.models import Instance, utcnow
from app.utils import awaken_dormant_instance
from tests.factories import make_instance


@pytest.fixture
def instance(app, db_session):
    i = make_instance('dorm.test')
    i.failures = 3
    i.dormant = False
    i.gone_forever = False
    i.start_trying_again = None
    db.session.commit()
    return i


def give_up_query_matches(instance):
    """The maintenance task's own filter, run against this row."""
    five_days_ago = utcnow() - timedelta(days=5)
    return instance in db.session.query(Instance).filter(
        Instance.dormant == True,
        Instance.start_trying_again < five_days_ago).all()


# --------------------------------------------------------------------------
# The first dormancy: the arms that already worked
# --------------------------------------------------------------------------


class TestTheFirstDormancy:
    def test_a_dormant_instance_with_no_wait_gets_one(self, instance):
        instance.dormant = True
        db.session.commit()
        before = utcnow()

        awaken_dormant_instance(instance)

        assert instance.dormant is True, 'it must not wake on the same call'
        assert instance.start_trying_again > before

    @pytest.mark.parametrize('failures, seconds', [
        (2, 16), (3, 81), (5, 625), (10, 10000),
    ])
    def test_the_wait_is_the_failure_count_to_the_fourth(self, app, db_session,
                                                         failures, seconds):
        """`timedelta(seconds=instance.failures ** 4)`, asserted as that exact
        formula at four counts.

        Comparing two instances' absolute `start_trying_again` values is what the
        first version of this test did, and it proved nothing: the later call
        produces the later timestamp whatever the formula is, so a fixed 60-second
        wait satisfied it. The duration is what the formula decides, so the
        duration is what is asserted -- with a window wide enough for the test's
        own execution time and narrow enough to exclude any other exponent.
        """
        i = make_instance(f'dorm-{failures}.test')
        i.dormant = True
        i.gone_forever = False
        i.failures = failures
        i.start_trying_again = None
        db.session.commit()
        before = utcnow()

        awaken_dormant_instance(i)

        waited = (i.start_trying_again - before).total_seconds()
        assert seconds <= waited < seconds + 5, waited

    def test_an_unexpired_wait_leaves_it_dormant(self, instance):
        instance.dormant = True
        instance.start_trying_again = utcnow() + timedelta(hours=1)
        db.session.commit()

        awaken_dormant_instance(instance)

        assert instance.dormant is True

    def test_an_expired_wait_wakes_it(self, instance):
        instance.dormant = True
        instance.start_trying_again = utcnow() - timedelta(seconds=5)
        db.session.commit()

        awaken_dormant_instance(instance)

        assert instance.dormant is False


class TestWhatIsLeftAlone:
    def test_an_instance_that_is_gone_forever_is_not_woken(self, instance):
        """`not instance.gone_forever` -- the whole point of that flag is that we
        stop trying, so an arriving activity must not undo it."""
        instance.dormant = True
        instance.gone_forever = True
        instance.start_trying_again = utcnow() - timedelta(days=1)
        db.session.commit()

        awaken_dormant_instance(instance)

        assert instance.dormant is True and instance.gone_forever is True

    def test_an_instance_that_is_not_dormant_is_untouched(self, instance):
        instance.start_trying_again = None
        db.session.commit()

        awaken_dormant_instance(instance)

        assert instance.dormant is False
        assert instance.start_trying_again is None

    def test_none_is_accepted(self, app, db_session):
        """`if instance and ...` -- the caller resolves the instance from an
        activity, and may not find one."""
        awaken_dormant_instance(None)


# --------------------------------------------------------------------------
# D1380: the timestamp must not outlive the dormancy
# --------------------------------------------------------------------------


class TestWakingClearsTheWait:
    def test_the_wait_is_cleared_when_it_wakes(self, instance):
        instance.dormant = True
        instance.start_trying_again = utcnow() - timedelta(seconds=5)
        db.session.commit()

        awaken_dormant_instance(instance)

        assert instance.dormant is False
        assert instance.start_trying_again is None

    def test_a_second_dormancy_waits_again(self, instance):
        """The measured defect. `update_dormant_gone` sets `dormant` without
        setting the timestamp, so a stale one made the `else` arm wake the
        instance on the first activity that arrived."""
        instance.dormant = True
        instance.start_trying_again = utcnow() - timedelta(seconds=5)
        db.session.commit()
        awaken_dormant_instance(instance)          # wakes, clearing the wait

        instance.failures = 3
        instance.update_dormant_gone()             # dormant again
        db.session.commit()
        assert instance.dormant is True

        awaken_dormant_instance(instance)

        assert instance.dormant is True, 'woken with no wait at all'
        assert instance.start_trying_again > utcnow()

    def test_the_wait_is_recomputed_from_the_current_failure_count(self, instance):
        """Not merely "a wait exists": the second dormancy has more failures
        behind it, so it must wait longer than the first did."""
        instance.dormant = True
        db.session.commit()
        awaken_dormant_instance(instance)
        first_wait = instance.start_trying_again - utcnow()

        instance.start_trying_again = utcnow() - timedelta(seconds=5)
        db.session.commit()
        awaken_dormant_instance(instance)          # wakes
        instance.failures = 6
        instance.update_dormant_gone()
        db.session.commit()
        awaken_dormant_instance(instance)

        assert instance.start_trying_again - utcnow() > first_wait


class TestTheMaintenanceTasksGiveUpQuery:
    """`dormant == True, start_trying_again < five_days_ago` sets
    `gone_forever`, which stops delivery for good."""

    def test_a_freshly_dormant_instance_is_not_given_up_on(self, instance):
        """The measured defect's worse half: a timestamp left over from a
        dormancy months ago is older than five days by definition, so this query
        matched an instance that had just gone dormant and had waited for
        nothing."""
        instance.dormant = True
        instance.start_trying_again = utcnow() - timedelta(seconds=5)
        db.session.commit()
        awaken_dormant_instance(instance)          # wakes, clearing the wait

        instance.failures = 3
        instance.update_dormant_gone()             # dormant again, no wait set
        db.session.commit()

        assert give_up_query_matches(instance) is False

    def test_an_instance_still_waiting_is_not_given_up_on(self, instance):
        instance.dormant = True
        db.session.commit()
        awaken_dormant_instance(instance)

        assert give_up_query_matches(instance) is False

    def test_an_instance_that_really_has_waited_five_days_is(self, instance):
        """The direction the query exists for, so the fix cannot be "never give
        up". A wait set five days ago and still dormant is a genuine candidate,
        and that is the state `maintenance.py:610` writes."""
        instance.dormant = True
        instance.start_trying_again = utcnow() - timedelta(days=6)
        db.session.commit()

        assert give_up_query_matches(instance) is True


class TestTheInvariant:
    """One sentence: `start_trying_again` is non-null only while dormant. Walked
    over the whole lifecycle rather than asserted at one point."""

    def test_over_a_full_lifecycle(self, instance):
        def holds():
            return instance.start_trying_again is None or instance.dormant

        assert holds()

        instance.failures = 3
        instance.update_dormant_gone()
        db.session.commit()
        assert holds(), 'dormant with no wait yet'

        awaken_dormant_instance(instance)
        assert holds(), 'dormant, now waiting'

        instance.start_trying_again = utcnow() - timedelta(seconds=1)
        db.session.commit()
        awaken_dormant_instance(instance)
        assert holds(), 'awake, so the wait must be gone'
        assert instance.dormant is False

        instance.failures = 5
        instance.update_dormant_gone()
        db.session.commit()
        assert holds(), 'dormant again, waiting recomputed on the next call'

        awaken_dormant_instance(instance)
        assert holds()
        assert instance.dormant is True
