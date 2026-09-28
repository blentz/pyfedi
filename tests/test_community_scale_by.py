"""`Community.scale_by`: the small-community boost in `ranking_scaled`.

Four places add it to a post's ranking -- `app/models.py:2793` and `:3579`,
`app/activitypub/util.py:3491`, `app/shared/post.py:246` -- so this function
decides feed order. It had no tests: measured over the whole suite, the only
executed line in it was its `def`.

D1378. The `subscriptions_count <= 1` fast path returned 3 where the computation
below it returns 4 for the same community. The two agreed by construction until
`0aa6993d7` ("smarter large community calculation #495") added an
`influence < 0.05` band returning 4 -- before that commit the top band and the
guard were both 3 -- and left the guard at the old maximum.

Measured across instance sizes, `subscribers:scale_by`:

    top-15% avg=20     1:3   2:3   3:3   5:2   25:0
    top-15% avg=100    1:3   2:4   3:4   5:3   25:2
    top-15% avg=1000   1:3   2:4   5:4  25:4  50:3

A brand-new community has exactly one subscriber, because its creator joined it.
On any instance whose top-15% average exceeds 20 it got LESS boost than a
community with two subscribers -- the opposite of what the band exists for. At 20
or below, `1/largest` is not under 0.05 and the stale value was right by
coincidence, which is why a small test instance would never show it.

WHAT THIS FILE ASSERTS. Every band, at its boundaries; the two early returns; and
the property the bands exist to express -- that the boost never increases with
size. That property is what makes the defect a defect rather than a tuning
choice, and it is asserted over a sweep rather than at one point.
"""
import pytest
from unittest.mock import patch

from app.models import Community


def scale_with(count, largest):
    """`scale_by` reads `self.subscriptions_count` and the cached top-15%
    average, and touches nothing else -- so an unsaved instance is the whole
    fixture, and the cache is patched rather than seeded."""
    community = Community(subscriptions_count=count)
    with patch('app.models._large_community_subscribers', return_value=largest):
        return community.scale_by()


# --------------------------------------------------------------------------
# The two early returns
# --------------------------------------------------------------------------


class TestTheSmallestCommunities:
    @pytest.mark.parametrize('count', [0, 1])
    @pytest.mark.parametrize('largest', [21, 40, 100, 1000, 10000])
    def test_they_get_the_largest_boost(self, app, count, largest):
        """D1378: this was 3. One subscriber is what a community has the moment
        it is created, so the case is the common one rather than an edge."""
        assert scale_with(count, largest) == 4

    @pytest.mark.parametrize('count', [0, 1])
    def test_the_fast_path_agrees_with_the_computation(self, app, count):
        """The guard exists to skip a cached query, not to state a policy, so it
        must answer what the computation below would. Asserted by computing the
        same influence the slow path would and checking the band."""
        largest = 100
        influence = count / largest

        assert influence < 0.05        # the band the slow path would take
        assert scale_with(count, largest) == scale_with(2, largest) == 4

    @pytest.mark.parametrize('largest', [1, 5, 20])
    def test_a_tiny_instance_is_where_the_old_value_looked_right(self, app,
                                                                largest):
        """At a top-15% average of 20 or below, `1/largest` is not under 0.05, so
        the slow path would have said 3 for a two-subscriber community and the
        stale 3 agreed. Pinned because it explains why this survived: a small
        test instance cannot show the defect.
        """
        assert scale_with(2, largest) <= 3
        # ... and the smallest community still takes the top band, because the
        # guard is about this community's size, not the instance's.
        assert scale_with(1, largest) == 4


class TestWhenThereIsNoLargestCommunity:
    @pytest.mark.parametrize('largest', [None, 0])
    def test_no_boost_at_all(self, app, largest):
        """`largest_community is None or largest_community == 0` -- an empty
        instance, or a cache holding a NULL from the AVG over no rows. Returning
        0 rather than dividing is the guard; without it this is
        `TypeError: unsupported operand` or `ZeroDivisionError`.
        """
        assert scale_with(5, largest) == 0

    @pytest.mark.parametrize('largest', [None, 0])
    def test_but_the_smallest_communities_still_short_circuit(self, app, largest):
        """Order matters: the `<= 1` guard is above the None/0 check, so a
        one-subscriber community never reaches it."""
        assert scale_with(1, largest) == 4


# --------------------------------------------------------------------------
# The bands
# --------------------------------------------------------------------------


class TestTheInfluenceBands:
    """`influence = subscriptions_count / int(largest_community)`, with
    `largest = 1000` throughout so each boundary is an exact integer count."""

    LARGEST = 1000

    @pytest.mark.parametrize('count, expected', [
        # influence < 0.05 -> 4
        (2, 4), (10, 4), (49, 4),
        # 0.05 <= influence < 0.25 -> 3
        (50, 3), (100, 3), (249, 3),
        # 0.25 <= influence < 0.60 -> 2
        (250, 2), (400, 2), (599, 2),
        # 0.60 <= influence < 1.0 -> 1
        (600, 1), (800, 1), (999, 1),
        # influence >= 1.0 -> 0
        (1000, 0), (1001, 0), (5000, 0),
    ])
    def test_each_band_and_its_boundary(self, app, count, expected):
        assert scale_with(count, self.LARGEST) == expected

    def test_the_first_two_bands_are_not_elif(self, app):
        """`if influence < 0.05: return 4` then `if influence < 0.25: return 3`
        -- the second is a plain `if`, so 0.01 must take 4 and never fall into
        the 3. Pinned because the two spellings read identically here and a
        reordering would silently make every tiny community a 3 again."""
        assert scale_with(10, self.LARGEST) == 4

    def test_the_average_is_truncated_not_rounded(self, app):
        """`int(largest_community)`. The cached value is an `AVG`, so a float,
        and truncating it moves every boundary.

        40.9 with two subscribers is the case that straddles one: truncated,
        `2 / 40 == 0.05`, which is NOT under 0.05 and lands in the second band;
        untruncated, `2 / 40.9 == 0.0489`, which is under it and lands in the
        first. A test at any other value agrees whichever way the division goes,
        which is how the first version of this test left the `int()` untested.
        """
        assert 2 / 40 == 0.05 and 2 / 40.9 < 0.05      # the two answers differ
        assert scale_with(2, 40.9) == 3
        assert scale_with(2, 40) == 3


# --------------------------------------------------------------------------
# The property the bands exist to express
# --------------------------------------------------------------------------


class TestTheBoostNeverGrowsWithSize:
    """What makes D1378 a defect rather than a tuning choice: within one
    instance, a bigger community may never get a bigger boost than a smaller
    one. The old value broke this between one subscriber and two."""

    @pytest.mark.parametrize('largest', [21, 40, 100, 1000, 10000])
    def test_over_a_sweep_of_sizes(self, app, largest):
        counts = [0, 1, 2, 3, 5, 10, 25, 50, 99, 100, 250, 500, 999, 1000, 5000]

        scales = [scale_with(c, largest) for c in counts]

        assert scales == sorted(scales, reverse=True), list(zip(counts, scales))

    @pytest.mark.parametrize('largest', [21, 100, 1000])
    def test_the_step_the_defect_put_between_one_and_two(self, app, largest):
        """The measured discontinuity, asserted as itself: 1 gave 3 and 2 gave 4."""
        assert scale_with(1, largest) >= scale_with(2, largest)

    def test_the_boost_stays_in_range(self, app):
        """`ranking_scaled = ranking + scale_by()`, and three of the four callers
        wrap it in `int()`. Every answer is one of the five documented steps --
        a band returning something else would move posts by an arbitrary amount.
        """
        answers = {scale_with(c, largest)
                   for largest in [None, 0, 1, 20, 100, 1000]
                   for c in [0, 1, 2, 5, 25, 100, 1000, 5000]}

        assert answers <= {0, 1, 2, 3, 4}
