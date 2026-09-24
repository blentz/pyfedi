"""`safe_order_by` -- the guard between a query string and an ORDER BY.

Five pages let the visitor choose the sort: the community directory
(`/communities`), the community list behind it, and three admin tables. All
of them pass `request.args.get('sort_by', ...)` through this one function,
whose job is to refuse anything that is not a column the caller named.

Two defects, found when a test that had passed for days failed on a run where
nothing had changed:

* the fallback for an invalid sort was `desc(getattr(model,
  next(iter(allowed_fields))))`. `allowed_fields` is a SET, so `next(iter(...))`
  picks an arbitrary element -- and because Python randomizes string hashing
  per process, a different one in every worker. Two Gunicorn workers sorted
  the same page differently, and any worker whose arbitrary pick was a name
  the model does not have answered 500 (D1266);
* the community directory's set named `average_rating`, which `Community`
  does not have, so that 500 was reachable: `AttributeError: type object
  'Community' has no attribute 'average_rating'` (D1267).

`sort_param` was also indexed as `parts[0]` straight after `.split()`, so
`/communities?sort_by=` -- an empty value anybody can type -- was
`IndexError`.
"""
import pytest
from sqlalchemy import asc, desc

from app.models import Community, Instance, User
from app.utils import safe_order_by

DIRECTORY_FIELDS = {'title', 'subscriptions_count', 'post_count',
                    'post_reply_count', 'last_active', 'created_at',
                    'active_weekly'}


def clause(*args, **kwargs):
    return str(safe_order_by(*args, **kwargs))


class TestASortTheCallerAllowed:
    def test_ascending_is_the_default_direction(self, app):
        assert clause('title', Community, DIRECTORY_FIELDS) == \
            str(asc(Community.title))

    def test_descending_is_asked_for_by_name(self, app):
        assert clause('title desc', Community, DIRECTORY_FIELDS) == \
            str(desc(Community.title))

    def test_the_direction_is_read_in_any_case(self, app):
        assert clause('title DESC', Community, DIRECTORY_FIELDS) == \
            str(desc(Community.title))

    def test_surrounding_space_is_ignored(self, app):
        assert clause('  title  desc  ', Community, DIRECTORY_FIELDS) == \
            str(desc(Community.title))

    def test_a_direction_that_is_not_one_sorts_ascending(self, app):
        assert clause('title sideways', Community, DIRECTORY_FIELDS) == \
            str(asc(Community.title))

    def test_a_second_sort_column_is_not_supported(self, app):
        """`'title desc, id desc'` splits to `['title', 'desc,', ...]`, so
        the direction is `desc,` -- not `desc` -- and the extra column is
        dropped. One column, one direction, nothing else."""
        assert clause('title desc, id desc', Community, DIRECTORY_FIELDS) == \
            str(asc(Community.title))


class TestASortTheCallerDidNotAllow:
    """Everything here must land on the default, and the default must be the
    same one every time."""

    def test_a_column_the_model_has_but_the_caller_did_not_offer(self, app):
        """`password_hash` is a real column on `User`. That is exactly why
        the allowed set exists."""
        allowed = {'user_name', 'banned', 'created', 'last_seen'}
        assert clause('password_hash desc', User, allowed) == \
            str(desc(User.banned))

    def test_a_column_nobody_has(self, app):
        assert clause('nonsense desc', Community, DIRECTORY_FIELDS) == \
            str(desc(Community.active_weekly))

    def test_something_that_is_not_a_column_name_at_all(self, app):
        assert clause('1; DROP TABLE community', Community,
                      DIRECTORY_FIELDS) == str(desc(Community.active_weekly))

    def test_an_empty_sort(self, app):
        """`/communities?sort_by=` was `IndexError: list index out of
        range`."""
        assert clause('', Community, DIRECTORY_FIELDS) == \
            str(desc(Community.active_weekly))

    def test_a_sort_that_is_only_space(self, app):
        assert clause('   ', Community, DIRECTORY_FIELDS) == \
            str(desc(Community.active_weekly))

    def test_no_sort_at_all(self, app):
        assert clause(None, Community, DIRECTORY_FIELDS) == \
            str(desc(Community.active_weekly))


class TestTheDefaultIsTheSameEveryTime:
    """D1266. A set has no order, and Python randomizes string hashing per
    process, so `next(iter(allowed_fields))` was a different column in every
    worker."""

    def test_it_does_not_depend_on_how_the_set_was_built(self, app):
        one = clause('nonsense', Community, {'title', 'post_count',
                                             'subscriptions_count'})
        two = clause('nonsense', Community, {'subscriptions_count',
                                             'post_count', 'title'})
        assert one == two

    def test_it_is_the_first_allowed_column_in_name_order(self, app):
        allowed = {'title', 'post_count', 'subscriptions_count'}
        assert clause('nonsense', Community, allowed) == \
            str(desc(Community.post_count))

    def test_every_call_site_agrees_with_itself(self, app):
        for model, allowed in (
                (Community, DIRECTORY_FIELDS),
                (User, {'user_name', 'banned', 'created', 'last_seen'}),
                (Instance, {'domain', 'software', 'version', 'last_seen'})):
            assert clause('nonsense', model, allowed) == \
                clause('nonsense', model, allowed)


class TestAnAllowedFieldTheModelDoesNotHave:
    """D1267. The community directory offered `average_rating`, which
    `Community` has never had."""

    def test_it_is_skipped_rather_than_raised_on(self, app):
        allowed = {'average_rating', 'title'}
        assert clause('nonsense', Community, allowed) == \
            str(desc(Community.title))

    def test_asking_for_it_by_name_falls_back_too(self, app):
        allowed = {'average_rating', 'title'}
        assert clause('average_rating desc', Community, allowed) == \
            str(desc(Community.title))

    def test_a_set_of_nothing_the_model_has_falls_back_to_the_id(self, app):
        assert clause('nonsense', Community, {'average_rating'}) == \
            str(desc(Community.id))

    def test_an_empty_set_of_allowed_fields(self, app):
        assert clause('title', Community, set()) == str(desc(Community.id))


class TestTheDirectoryAsItIsConfigured:
    """The set the community directory actually passes, checked against the
    model rather than against itself."""

    def test_every_field_it_offers_exists(self, app):
        missing = sorted(field for field in DIRECTORY_FIELDS
                         if not hasattr(Community, field))
        assert missing == []

    @pytest.mark.parametrize('field', sorted(DIRECTORY_FIELDS))
    def test_each_one_sorts_on_the_column_it_names(self, app, field):
        assert clause(f'{field} desc', Community, DIRECTORY_FIELDS) == \
            str(desc(getattr(Community, field)))
