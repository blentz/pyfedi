from datetime import datetime

import pytest

from app.utils import (days_to_add_for_next_month, expand_hex_color, human_filesize,
                       intlist_to_strlist, paginate_post_ids, sha256_digest,
                       shorten_number, wilson_confidence_lower_bound)


class TestWilsonConfidenceLowerBound:
    """Fails if the n == 0 guard or the negative clamp is removed."""

    def test_no_votes_scores_zero(self):
        assert wilson_confidence_lower_bound(0, 0) == 0.0

    def test_none_is_treated_as_zero(self):
        assert wilson_confidence_lower_bound(None, None) == 0.0

    def test_negative_counts_are_clamped_not_propagated(self):
        assert wilson_confidence_lower_bound(-5, -5) == 0.0
        assert wilson_confidence_lower_bound(-5, 10) == wilson_confidence_lower_bound(0, 10)

    def test_more_upvotes_scores_higher(self):
        assert wilson_confidence_lower_bound(100, 0) > wilson_confidence_lower_bound(10, 0)

    def test_confidence_grows_with_sample_size(self):
        """Ten of ten is a stronger claim than one of one, at the same ratio."""
        assert wilson_confidence_lower_bound(10, 0) > wilson_confidence_lower_bound(1, 0)

    def test_result_is_a_probability(self):
        for ups, downs in [(1, 0), (0, 1), (5, 5), (99, 1)]:
            assert 0.0 <= wilson_confidence_lower_bound(ups, downs) <= 1.0


class TestDaysToAddForNextMonth:
    """Fails if the backtracking loop or the December rollover is removed."""

    def test_mid_month_is_the_month_length(self):
        assert days_to_add_for_next_month(datetime(2026, 1, 15)) == 31

    def test_december_rolls_over_to_january(self):
        assert days_to_add_for_next_month(datetime(2026, 12, 15)) == 31

    def test_the_31st_backtracks_to_the_last_valid_day(self):
        """Jan 31 has no Feb 31; the loop must land on Feb 28 in a non-leap year."""
        assert days_to_add_for_next_month(datetime(2026, 1, 31)) == 28

    def test_the_31st_backtracks_to_february_29_in_a_leap_year(self):
        assert days_to_add_for_next_month(datetime(2028, 1, 31)) == 29

    def test_the_31st_reaches_a_31_day_month_intact(self):
        assert days_to_add_for_next_month(datetime(2026, 3, 31)) == 30


class TestHumanFilesize:
    """Fails if the zero guard or the unit-cap on the while loop is removed."""

    def test_zero(self):
        assert human_filesize(0) == '0 B'

    def test_bytes_below_one_kilobyte(self):
        assert human_filesize(1023) == '1023.0 B'

    def test_exactly_one_kilobyte(self):
        assert human_filesize(1024) == '1.0 KB'

    def test_each_unit_step(self):
        assert human_filesize(1024 ** 2) == '1.0 MB'
        assert human_filesize(1024 ** 3) == '1.0 GB'
        assert human_filesize(1024 ** 4) == '1.0 TB'
        assert human_filesize(1024 ** 5) == '1.0 PB'

    def test_beyond_the_largest_unit_stays_in_petabytes(self):
        """The loop is capped at len(units) - 1, so it must not run off the end."""
        assert human_filesize(1024 ** 6) == '1024.0 PB'


class TestShortenNumber:
    def test_below_one_thousand_is_unchanged(self):
        assert shorten_number(999) == '999'

    def test_thousands(self):
        assert shorten_number(1000) == '1.0k'

    def test_millions(self):
        assert shorten_number(1_500_000) == '1.5M'


class TestPaginatePostIds:
    def test_first_page(self):
        assert paginate_post_ids([1, 2, 3, 4, 5, 6, 7], 0, 3) == [1, 2, 3]

    def test_second_page(self):
        assert paginate_post_ids([1, 2, 3, 4, 5, 6, 7], 1, 3) == [4, 5, 6]

    def test_page_past_the_end_is_empty(self):
        assert paginate_post_ids([1, 2, 3], 9, 3) == []


class TestSha256Digest:
    def test_known_digest_of_the_empty_string(self):
        assert sha256_digest('') == (
            'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')

    def test_non_ascii_is_encoded_as_utf8(self):
        """Fails if the .encode('utf-8') is dropped, which would raise."""
        assert len(sha256_digest('café')) == 64


class TestIntlistToStrlist:
    def test_converts_each_element(self):
        assert intlist_to_strlist([1, 2, 3]) == ['1', '2', '3']

    def test_empty(self):
        assert intlist_to_strlist([]) == []


class TestExpandHexColor:
    def test_three_digit_shorthand_expands(self):
        assert expand_hex_color('#abc') == '#aabbcc'

    def test_case_is_preserved(self):
        assert expand_hex_color('#ABC') == '#AABBCC'
        assert expand_hex_color('#aB3') == '#aaBB33'

    @pytest.mark.parametrize('text', ['#ab', '#', '', '#abcd', '#abcdef', 'abc', 'abcd',
                                      '#ab!', '#a-c', '#ab ', ' #abc', '#абв', 'rebeccapurple',
                                      'var(--x)', '#12', '#1234567'])
    def test_anything_that_is_not_a_three_digit_hex_colour_is_returned_unchanged(self, text):
        """WAS A BUG, now fixed: this used to index text[1]..text[3] unguarded.

        '#ab' raised IndexError; the old test here asserted
        `pytest.raises(IndexError)` because the defect was reported rather than
        fixed at the time. Every call site is in
        app/api/alpha/utils/community.py (lines 547, 553, 600, 606) and each one
        guards with `len(...) == 4`, so the crash was not reachable -- but that
        guard checks length, not format, and the value comes straight from API
        input. 'abcd' has length 4, passed the guard, and was silently expanded
        to '#bbccdd'. Returning the input unchanged is what makes that stop:
        garbage in, the same garbage out, and no 500.

        The function does NOT raise. These are API-supplied CSS colours, and
        raising would turn bad input into an unhandled 500 at all four sites.
        """
        assert expand_hex_color(text) == text

    def test_a_six_digit_colour_is_left_alone(self):
        """The call sites only invoke this for len == 4, but the function has to
        be total on its own: an already-expanded colour must not be mangled."""
        assert expand_hex_color('#DEDDDA') == '#DEDDDA'
