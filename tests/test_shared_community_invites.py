"""Two small helpers of app/shared/community.py's invite-and-flair group:
`create_invite_token` (:176-186) and `get_comm_flair_list` (:667-681).

STEP 1'S ORACLE CHECK, CONFIRMED BEFORE WRITING ANYTHING ELSE: `/usr/bin/grep
-rln "create_invite_token" tests/ --include=*.py` and the same for
`get_comm_flair_list` both return nothing -- neither function is named by any
existing test file. `get_comm_flair_list` nonetheless has production callers
in FOUR other files that pass it a `Community` object: `app/api/alpha/
views.py:602`, `app/community/routes.py:677` and `:2474`, and `app/post/
routes.py:315` and `:737` (a fifth line, `app/shared/tasks/groups.py:5`, only
imports the name and never calls it). Whatever those other modules' test
suites already exercise reaches `:671-672` (the `isinstance(community,
Community)` arm) and the final query at `:681` through those call sites, so
a full-suite coverage run credits this file's target functions with lines
already green from elsewhere. Every number below states whether it is
measured against THIS FILE ALONE (`--cov=app.shared.community` run against
just this test module) or FULL-SUITE (deferred to Task 7's `--cov=app` run,
which is the only run that will see those other modules' contribution). A
per-file run over this test alone will report `get_comm_flair_list` with
more missing lines than the full-suite figure -- that gap is the callers
above, not a defect in this file.

`_burn_a_seed()` and `_seed()` below are consumed by this round's later
tasks (2-5) against the rest of this file's invite-and-flair surface, so
their shape (a `SimpleNamespace` with `.instance`, `.user`, `.community`)
and names are fixed by the plan, not just local convenience. Neither target
function in this task reads `current_user`, checks membership, or branches
on role, so unlike the moderation file's `_seed()`, no `CommunityMember` row
is minted here -- Tasks 2-5 add that where their own targets need it.

THE CREATE_INVITE_TOKEN FORK, `:179`: `existing_invite` is looked up by
`(community_id, recipient.id)`, and either the found row's token is
returned unchanged (`:180`) or a fresh `CommunityInvitation` is minted and
committed (`:182-186`). The two tests below assert the row COUNT as well as
the returned value in both arms -- a mutant that always mints a fresh
CommunityInvitation, discards it, and returns `existing_invite.token`
anyway would still make the equality assertion pass on the true arm, so the
count is what catches it; on the false arm, a mutant that mints two rows
instead of one would still return *a* valid token, so again the count is
what carries the test.

THE GET_COMM_FLAIR_LIST FORKS: `:668` (`int`), `:671` (`Community`), `:673`
(`str`) are the three `isinstance` arms. The `str` arm forks a second time:
`:675`'s exact `.filter_by(name=..., ap_domain=...).first()` either finds a
row directly, or returns `None`, in which case `:677-678`'s case-insensitive
fallback runs `.filter(func.lower(...) == ..., func.lower(...) == ...)
.one()`. `.first()` and `.one()` disagree on what "not found" means:
`.first()` returns `None` quietly, while `.one()` raises `NoResultFound`
directly -- for a ZERO-row result as much as for a multi-row one. The tests
below cover both the exact-match arm and the fallback arm succeeding, and a
third test pins the exact difference the docstring above names: a string
that matches nothing at all makes `.first()` return `None` (no exception)
and then makes the fallback `.one()` raise `NoResultFound` (an exception) --
smoothing that into "both branches return not-found" would hide a real
asymmetry in the production code.

`:681` orders by `CommunityFlair.flair`. The ordering test below seeds
flairs in an order that is NOT alphabetical ('zebra' before 'apple') so
that an assertion on the returned order actually exercises `order_by`
rather than merely reproducing insertion order, which SQLite/Postgres often
return by default without seeing a rows-happen-to-agree false pass.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import NoResultFound

from app import db
from app.models import Community, CommunityFlair, CommunityInvitation
from app.shared.community import create_invite_token, get_comm_flair_list
from tests.factories import make_community, make_community_flair, make_instance, make_user


def _burn_a_seed():
    """Mint and discard user id 1.

    app/models.py:1259-1261 makes id 1 an admin unconditionally, and
    conftest.py:131-132 resets every sequence between tests, so without this
    the first user a test creates is silently an admin. Neither target
    function in this file checks admin status, but `make_community`
    (tests/factories.py:124) hardcodes `instance_id=1, user_id=1`, so the
    burn is still load-bearing: it supplies the row those foreign keys point
    at.
    """
    inst = make_instance('burn.test')
    burn = make_user(inst, 'burn')
    assert burn.id == 1, f'expected the burn user at id 1, got {burn.id}'
    return inst


def _seed():
    """An instance, a non-admin user, and a local community, with id 1 burned.

    A bystander community is minted FIRST, purely to consume Community id 1.
    conftest.py:131-132 resets every sequence between tests, so without this
    the returned community's id would deterministically be 1 in every test,
    and a mutant hardcoding `community_id=1` anywhere in this round's later
    tasks would be indistinguishable from correct code by any assertion
    built from this seed alone. The bystander is otherwise unused and
    unreferenced.
    """
    _burn_a_seed()
    instance = make_instance('test.piefed.local')
    user = make_user(instance, 'alice', local=True)
    make_community('bystander', host='bystander.example')
    community = make_community()
    return SimpleNamespace(instance=instance, user=user, community=community)


# `create_invite_token` (app/shared/community.py:176-186).


def test_create_invite_token_mints_a_token_when_none_exists(app, db_session):
    """`:179`'s false arm: no `CommunityInvitation` row exists for this
    (community, recipient) pair, so `:182-186` create one and return its
    freshly generated token.

    Asserting the row COUNT (not just the returned token) is what catches a
    mutant that creates two rows here -- an "extra" mint that would still
    return a value equal to the second row's own token.
    """
    s = _seed()
    recipient = make_user(s.instance, 'bob', local=True)
    assert db.session.query(CommunityInvitation).count() == 0

    token = create_invite_token(s.community, recipient, s.user)

    rows = db.session.query(CommunityInvitation).filter_by(
        community_id=s.community.id, user_id=recipient.id).all()
    assert len(rows) == 1
    assert rows[0].token == token
    assert rows[0].inviter_id == s.user.id


def test_create_invite_token_returns_the_existing_token_unchanged(app, db_session):
    """`:179`'s true arm: `:180` returns the existing token rather than
    minting a new one.

    Asserting the returned value EQUALS the seeded token -- not merely that
    a token of the right shape came back -- is what carries this test: a
    mutant that minted a fresh token and returned THAT instead would also
    "return a token", but it would not equal the one seeded below. The row
    count staying at 1 catches a mutant that mints an extra row alongside
    the correct return value.
    """
    s = _seed()
    recipient = make_user(s.instance, 'bob', local=True)
    existing = CommunityInvitation(token='seededtoken123', community_id=s.community.id,
                                    user_id=recipient.id, inviter_id=s.user.id)
    db.session.add(existing)
    db.session.commit()

    token = create_invite_token(s.community, recipient, s.user)

    assert token == 'seededtoken123'
    assert db.session.query(CommunityInvitation).filter_by(
        community_id=s.community.id, user_id=recipient.id).count() == 1


# `get_comm_flair_list` (app/shared/community.py:667-681).


def test_get_comm_flair_list_int_arg_looks_up_by_id(app, db_session):
    """`:668`'s true arm: an `int` argument is used directly as
    `community_id` and also re-fetched (`:670`) as a `Community` row (the
    fetched object itself is not read again by this function, but a bad id
    would raise `NoResultFound` there -- covered separately below).
    """
    s = _seed()
    make_community_flair(s.community, name='alpha')

    result = get_comm_flair_list(s.community.id)

    assert [f.flair for f in result] == ['alpha']


def test_get_comm_flair_list_int_arg_missing_id_raises_NoResultFound(app, db_session):
    """`:670`'s `.one()` raises `NoResultFound` for an id with no matching
    row, even though `community_id` was already captured from the argument
    at `:669` -- the lookup still runs and still enforces existence.
    """
    _seed()

    with pytest.raises(NoResultFound):
        get_comm_flair_list(999999)


def test_get_comm_flair_list_community_arg_uses_id_directly(app, db_session):
    """`:671`'s true arm: passing a `Community` object sets `community_id =
    community.id` directly, with no extra query.
    """
    s = _seed()
    make_community_flair(s.community, name='alpha')

    result = get_comm_flair_list(s.community)

    assert [f.flair for f in result] == ['alpha']


def test_get_comm_flair_list_str_arg_exact_match(app, db_session):
    """`:673`'s true arm and `:675`'s true arm: the string splits into
    `name@ap_domain` matching a community exactly (same case), so
    `.filter_by(...).first()` finds it directly and `:676`'s `if community
    is None` is false -- `:677-678`'s fallback never runs.
    """
    s = _seed()
    make_community_flair(s.community, name='alpha')
    lookup = f'{s.community.name}@{s.community.ap_domain}'

    result = get_comm_flair_list(lookup)

    assert [f.flair for f in result] == ['alpha']


def test_get_comm_flair_list_str_arg_case_insensitive_fallback(app, db_session):
    """`:676`'s true arm: an exact-case lookup finds nothing (`.first()`
    returns `None`, no exception), so `:677-678`'s case-insensitive fallback
    runs and its `.one()` finds the row by folding both sides to lower case.
    `community_id = community.id` at `:679` then only executes on this path
    or the exact-match path above -- never on a bare `.first()` miss alone.
    """
    s = _seed()
    make_community_flair(s.community, name='alpha')
    exact = f'{s.community.name}@{s.community.ap_domain}'
    assert db.session.query(Community).filter_by(
        name=s.community.name, ap_domain=s.community.ap_domain).first() is not None
    mismatched_case = exact.upper()
    assert mismatched_case != exact
    assert db.session.query(Community).filter_by(
        name=mismatched_case.split('@')[0], ap_domain=mismatched_case.split('@')[1]).first() is None

    result = get_comm_flair_list(mismatched_case)

    assert [f.flair for f in result] == ['alpha']


def test_get_comm_flair_list_str_arg_no_match_raises_NoResultFound(app, db_session):
    """The asymmetry the module docstring names, pinned directly: a string
    matching NO community at all makes `:675`'s `.first()` return `None`
    quietly (no exception), then makes `:677-678`'s fallback `.one()` raise
    `NoResultFound` -- a zero-row result is treated identically to a
    multi-row one by `.one()`, unlike `.first()`'s silent `None`. This is
    the difference the docstring asks not to be smoothed over: the two
    "not found" outcomes at `:675` and `:677-678` are NOT the same shape.
    """
    _seed()

    with pytest.raises(NoResultFound):
        get_comm_flair_list('nobody@nowhere.example')


def test_get_comm_flair_list_orders_by_flair_name(app, db_session):
    """`:681`'s `order_by(CommunityFlair.flair)`. Flairs are seeded in
    non-alphabetical INSERTION order ('zebra' before 'apple') so that an
    assertion on the returned order actually exercises the `order_by` clause
    rather than coincidentally matching insertion order, which a query with
    no `order_by` at all could still satisfy on some backends.
    """
    s = _seed()
    make_community_flair(s.community, name='zebra')
    make_community_flair(s.community, name='apple')

    result = get_comm_flair_list(s.community)

    assert [f.flair for f in result] == ['apple', 'zebra']
