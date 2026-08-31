"""`parse_ban_expiry` (app/activitypub/util.py) -- the shared parser for the
peer-supplied ban expiry carried by an incoming Block activity.

It was extracted from `ban_user`'s community branch, which already had the
hardened form (ISO first, `pendulum` as a fallback, naive values assumed
UTC, already-expired values rejected). Two other call sites had weaker
copies: `ban_user`'s instance-wide branch used a bare
`datetime.fromisoformat`, and the Block arm's ordinary site-ban path in
app/activitypub/routes.py did no parsing at all -- it assigned the raw peer
string to `blocked.ban_until`, a name `User` does not have, so every remote
temporary site ban silently became permanent (D91 in this campaign's
findings register). All three now share this one function.

The unit is pure: no app context, no database, no fixtures.

**Why unparseable input returns None instead of raising.**
`process_inbox_request` wraps every dispatch arm in
`except Exception: session.rollback(); raise` (app/activitypub/routes.py).
A parse error propagating out of a ban would therefore roll the whole ban
back -- a peer could drop any ban of their users by sending a malformed
date. Returning None means "no expiry", which every consumer of these
columns already reads as a permanent ban (see `User.banned_until`'s own
comment, app/models.py: "null == permanent ban"). Losing an expiry is the
lesser failure, and it is the one the surrounding code already models.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.activitypub.util import parse_ban_expiry


def _future_iso(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def test_no_expiry_field_at_all_is_none():
    """A Block with neither key is a permanent ban, not an error."""
    assert parse_ban_expiry({'type': 'Block'}) is None


def test_expires_with_a_trailing_z_is_parsed_as_utc():
    """Lemmy sends 'Z'-suffixed timestamps. `datetime.fromisoformat` accepts
    them on the Python this project runs (3.13, per the Dockerfile); the
    assertion below pins the parsed value rather than merely non-None, so it
    cannot pass on a wrongly-parsed date.
    """
    assert parse_ban_expiry({'expires': '2099-03-04T05:06:07Z'}) == \
        datetime(2099, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def test_a_naive_timestamp_is_assumed_to_be_utc():
    """No offset in the string, so none can be inferred -- UTC is the
    assumption the community branch already made before extraction, kept
    here deliberately rather than rediscovered.
    """
    assert parse_ban_expiry({'expires': '2099-03-04T05:06:07'}) == \
        datetime(2099, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def test_a_non_utc_offset_is_preserved_as_the_same_instant():
    """A peer in another timezone must not have its expiry shifted."""
    assert parse_ban_expiry({'expires': '2099-03-04T05:06:07+02:00'}) == \
        datetime(2099, 3, 4, 3, 6, 7, tzinfo=timezone.utc)


def test_end_time_is_used_when_expires_is_absent():
    """The `elif 'endTime'` precedence the Block arm used to implement
    inline. A different date from the `expires` tests above, so this cannot
    pass on a value some other field supplied.
    """
    assert parse_ban_expiry({'endTime': '2088-11-12T13:14:15Z'}) == \
        datetime(2088, 11, 12, 13, 14, 15, tzinfo=timezone.utc)


def test_expires_wins_when_both_keys_are_present():
    """Preserves the original `if expires / elif endTime` order. Both values
    are valid future dates, so whichever is returned is a real parse -- the
    test distinguishes precedence, not parseability.
    """
    assert parse_ban_expiry({'expires': '2099-03-04T05:06:07Z',
                             'endTime': '2088-11-12T13:14:15Z'}) == \
        datetime(2099, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def test_an_empty_expires_falls_through_to_end_time():
    """`expires: ''` is falsy. Documented because the function selects with
    `or` rather than `in`, so an empty string behaves as absent here where
    the original `if 'expires' in core_activity` would have taken the
    `expires` branch and then failed to parse it.
    """
    assert parse_ban_expiry({'expires': '', 'endTime': '2088-11-12T13:14:15Z'}) == \
        datetime(2088, 11, 12, 13, 14, 15, tzinfo=timezone.utc)


def test_the_pendulum_fallback_is_reachable_and_not_dead_code():
    """`'2099-001'` is an ISO ORDINAL date -- the 1st day of 2099.
    `datetime.fromisoformat` rejects it (verified in this project's own
    container, Python 3.13); `pendulum.parse` resolves it to 2099-01-01.

    This is the only test here that distinguishes the fallback from the
    primary parser, so it is what stops the `except (TypeError, ValueError)`
    branch from silently becoming dead code -- delete the `pendulum.parse`
    call and every other test in this file still passes.
    """
    assert parse_ban_expiry({'expires': '2099-001'}) == \
        datetime(2099, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize('raw', [
    '2020-01-01T00:00:00Z',           # long past
    '1999-12-31T23:59:59+00:00',      # long past, explicit offset
])
def test_an_already_expired_ban_is_permanent(raw):
    """A ban whose expiry has already passed cannot be a temporary ban. The
    community branch rejected these before extraction (`if ban_until >
    datetime.now(timezone.utc)`); the behaviour is kept.
    """
    assert parse_ban_expiry({'expires': raw}) is None


def test_a_moment_in_the_past_is_rejected_but_the_near_future_is_kept():
    """Pins the boundary as 'now', not some coarser granularity -- a pair,
    so neither half can pass by the function ignoring the comparison
    entirely (returning everything, or returning nothing).
    """
    assert parse_ban_expiry({'expires': _future_iso(seconds=-5)}) is None
    assert parse_ban_expiry({'expires': _future_iso(hours=1)}) is not None


@pytest.mark.parametrize('raw', [
    'not-a-real-date',
    'yesterday',
    '2099-13-45T99:99:99Z',   # ISO-shaped but impossible
    '{}',
])
def test_unparseable_input_is_treated_as_no_expiry(raw):
    """See this module's docstring: returning None keeps the ban and loses
    the expiry; raising would have lost the ban. `'2099-13-45T99:99:99Z'`
    is included because a value that LOOKS ISO-shaped is the one most
    likely to slip past a parser that only checks the format loosely.
    """
    assert parse_ban_expiry({'expires': raw}) is None


@pytest.mark.parametrize('raw', [0, 12345, [], {}, True])
def test_non_string_values_are_treated_as_no_expiry(raw):
    """Peer JSON is untrusted, so `expires` need not be a string at all.
    Falsy values are selected away; truthy non-strings must not raise out of
    `fromisoformat` or `pendulum.parse`.
    """
    assert parse_ban_expiry({'expires': raw}) is None
