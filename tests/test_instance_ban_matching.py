"""`instance_banned` and `inbox_domain` -- two ways past a ban.

Sub-project 79, slice C. These live in `app/utils.py` rather than in the admin
blueprint, but both were found by probing the READ side of what
`admin_federation` writes: the blocklist text box is the only place a wildcard
pattern is entered, and the trailing-dot bypass defeats exactly the rows that
form creates.

`instance_banned` gates inbound activity processing and outbound delivery, so a
defect here is not a page that misbehaves -- it is the instance federating with
someone it has defederated, or not federating at all.
"""
import pytest

from app import cache, db
from app.models import AllowedInstances, BannedInstances
from app.utils import inbox_domain, instance_allowed, instance_banned

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture(autouse=True)
def no_memoized_answers():
    """`instance_banned` and `instance_allowed` are `@cache.memoize(150)`, and
    the rows these tests add would otherwise be masked by an answer another row
    computed for the same domain."""
    cache.clear()
    yield
    cache.clear()


def _ban(domain):
    db.session.add(BannedInstances(domain=domain))
    db.session.commit()
    cache.clear()


# --------------------------------------------------------------------------
# P1: a wildcard ban is a regex, and was not escaped
# --------------------------------------------------------------------------


def test_a_wildcard_ban_matches_one_letter_or_digit(no_memoized_answers, app, db_session):
    """The feature itself. Mastodon publishes bans like `cum.**mp`, where each
    `*` stands for one character, and PieFed honours them."""
    _ban('ev*l.com')

    assert instance_banned('evil.com') is True
    assert instance_banned('evXl.com') is True
    assert instance_banned('ev4l.com') is True
    assert instance_banned('evl.com') is False, 'a * must match exactly one character'


def test_a_wildcard_matches_only_a_letter_or_digit(no_memoized_answers, app,
                                                   db_session):
    """`[a-zA-Z0-9]`, not `.`. A ban on `ev*l.com` is a ban on one instance per
    substituted character, not on every string of that shape -- `ev-l.com` and
    `ev.l.com` are different owners, and a hyphen or a dot is legal in a
    hostname label boundary. Widening the class to `.` would defederate them
    too, silently."""
    _ban('ev*l.com')

    assert instance_banned('evil.com') is True
    assert instance_banned('ev-l.com') is False
    assert instance_banned('ev.l.com') is False
    assert instance_banned('ev_l.com') is False


def test_a_wildcard_ban_is_anchored_at_both_ends(no_memoized_answers, app,
                                                 db_session):
    """`'^' + ... + '$'`, and `re.match` only anchors the START.

    Without the trailing `$` a ban on `ev*l.com` would also match
    `evil.com.attacker.example` -- a domain the attacker controls and that
    merely begins with the banned one. That is the wrong direction for an
    allowlist-mode instance and the wrong direction for a blocklist one too:
    it defederates strangers.
    """
    _ban('ev*l.com')

    assert instance_banned('evil.com') is True
    assert instance_banned('evil.com.attacker.example') is False
    assert instance_banned('prefix.evil.com') is False


@pytest.mark.parametrize('absent', [None, ''])
def test_an_absent_domain_is_refused(no_memoized_answers, app, db_session,
                                     absent):
    """`if domain is None or domain == '': return True` -- the fail-CLOSED
    direction, changed on 2026-08-29 and explained at length in the function.

    `Instance.domain` is nullable and `inbox_domain` returns `''` for an actor
    id `urlparse` refuses, so this is the one input the callers cannot vouch
    for. Answering "not banned" is what let a banned instance evade its ban by
    malforming its actor id.
    """
    assert instance_banned(absent) is True


def test_a_dot_in_a_wildcard_ban_is_literal(no_memoized_answers, app, db_session):
    """P1's pin, inverted.

    The pattern was interpolated raw, so every `.` matched any character and a
    ban on `ev*l.com` also banned `evilXcom` -- an instance the admin never
    named. Measured before the fix:

        PROBE f3 instance_banned('evilXcom') -> True
    """
    _ban('ev*l.com')

    assert instance_banned('evilXcom') is False, (
        "the '.' in a wildcard ban must be literal, not a regex metacharacter")


def test_a_wildcard_ban_holding_a_metacharacter_does_not_break_federation(
        no_memoized_answers, app, db_session):
    """P1's other half, and the reason it is a P and not an R.

    `re.compile` raised on a pattern the admin typed, and `instance_banned`
    re-raises -- so a single malformed blocklist entry made every inbound
    activity and every outbound delivery raise, instance-wide, from a text box.
    Measured before the fix:

        PROBE f4 pattern='ev*l.co(m' RAISED: PatternError missing ), unterminated
                 subpattern at position 18
    """
    _ban('ev*l.co(m')

    assert instance_banned('good.com') is False
    assert instance_banned('evXl.co(m') is True


@pytest.mark.parametrize('pattern', ['ev*l.co(m', 'ev*l.com[', 'ev*l.com+',
                                     'ev*l.com)', 'ev*l.co{2}m'])
def test_no_wildcard_pattern_can_raise(no_memoized_answers, app, db_session,
                                       pattern):
    """The class, not the one instance of it. Every regex metacharacter an
    admin might type is asked for, and none may raise."""
    _ban(pattern)

    assert instance_banned('unrelated.example') is False


def test_a_ban_without_a_wildcard_never_reaches_the_regex_path(
        no_memoized_answers, app, db_session):
    """`.like('%*%')` is what selects the patterns, so an ordinary ban is
    matched by the exact-row query above and a metacharacter in it is inert.
    Measured before the fix as `PROBE f4 pattern='evil.com[' -> False`, i.e. it
    did not raise even then."""
    _ban('evil.com[')

    assert instance_banned('unrelated.example') is False
    assert instance_banned('evil.com[') is True


# --------------------------------------------------------------------------
# P2: the trailing dot
# --------------------------------------------------------------------------


@pytest.mark.parametrize('presented', [
    'evil.com',
    'evil.com.',
    'EVIL.COM.',
    'https://evil.com./users/someone',
    'https://EVIL.COM./inbox',
    'https://evil.com.:443/inbox',
])
def test_a_banned_instance_cannot_re_federate_by_adding_a_dot(
        no_memoized_answers, app, db_session, presented):
    """P2's pin, inverted.

    `evil.com.` is the fully-qualified form of `evil.com`: DNS resolves the two
    identically and TLS works either way, so an actor id of
    `https://evil.com./users/x` fetches fine and the activity is processed. It
    was a different STRING, so it missed its own row in `banned_instances`.
    Measured before the fix:

        PROBE f5 actor='https://evil.com/users/x'   inbox_domain='evil.com'   banned=True
        PROBE f5 actor='https://evil.com./users/x'  inbox_domain='evil.com.'  banned=False

    Every form a peer controls is asked for: bare, trailing-dot, upper-case,
    actor URL, inbox URL, and one with a port, since `inbox_domain` drops ports
    too and the dot must be stripped after that.
    """
    _ban('evil.com')

    assert instance_banned(presented) is True


def test_the_allowlist_normalises_the_same_way(no_memoized_answers, app, db_session):
    """The allowlist direction failed SAFE -- an unrecognised string is simply
    not on the list -- which is why P2 only ever showed up as a ban bypass. It
    must still normalise, or an allowlisted instance is refused for presenting
    its own fully-qualified name."""
    db.session.add(AllowedInstances(domain='good.com'))
    db.session.commit()
    cache.clear()

    assert instance_allowed('good.com') is True
    assert instance_allowed('good.com.') is True
    assert instance_allowed('GOOD.COM.') is True
    assert instance_allowed('elsewhere.com') is False


@pytest.mark.parametrize('given, expected', [
    ('evil.com', 'evil.com'),
    ('evil.com.', 'evil.com'),
    ('EVIL.COM.', 'evil.com'),
    ('https://evil.com./inbox', 'evil.com'),
    ('https://evil.com.:8443/inbox', 'evil.com'),
    ('evil.com..', 'evil.com'),
    ('.', ''),
    ('', ''),
])
def test_inbox_domain_normalises_the_trailing_dot(given, expected):
    """The normalisation itself, at the one place it is implemented, so every
    caller -- instance_banned, instance_allowed, instance_online,
    instance_gone_forever -- gets it.

    `'.'` reducing to `''` matters: `instance_banned` refuses an empty domain,
    so a peer presenting a bare root label is refused rather than waved
    through."""
    assert inbox_domain(given) == expected
