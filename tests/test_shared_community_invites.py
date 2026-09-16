"""Two small helpers of app/shared/community.py's invite-and-flair group:
`create_invite_token` (:176-186) and `get_comm_flair_list` (:667-681).

STEP 1'S ORACLE CHECK, CONFIRMED BEFORE WRITING ANYTHING ELSE: `/usr/bin/grep
-rln "create_invite_token" tests/ --include=*.py` and the same for
`get_comm_flair_list` both return nothing -- neither function is named by any
existing test file. `get_comm_flair_list` nonetheless has production callers
in THREE other files that pass it a `Community` object, five call sites total:
`app/api/alpha/views.py:602`, `app/community/routes.py:677` and `:2474`, and
`app/post/routes.py:315` and `:737` (a FOURTH file, `app/shared/tasks/
groups.py:5`, only imports the name and never calls it). Whatever those other
modules' test suites already exercise reaches `:671-672` (the
`isinstance(community, Community)` arm) and the final query at `:681` through
those call sites, so a full-suite coverage run credits this file's target
functions with lines already green from elsewhere. Every number below states
whether it is measured against THIS FILE ALONE (`--cov=app.shared.community`
run against just this test module) or FULL-SUITE (deferred to Task 7's
`--cov=app` run, which is the only run that will see those other modules'
contribution). A per-file run over this test alone will report
`get_comm_flair_list` with more missing lines than the full-suite figure --
that gap is the callers above, not a defect in this file.

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

FINDING 3, PINNED NOT FIXED: `:668-679` is an `if`/`elif`/`elif` chain with
no `else`. An argument that is none of `int`, `Community`, or `str` --
out of contract for the declared signature `Community | int | str`, but
Python enforces no such thing at runtime -- falls through all three arms
without `community_id` ever being bound, and `:681`'s `CommunityFlair.query
.filter_by(community_id=community_id)` then raises `UnboundLocalError`
reading it. This round's production budget was spent elsewhere, so the test
below pins today's actual failure (an `UnboundLocalError` naming an internal
variable, not a `TypeError` naming the broken contract) rather than the
intended behaviour; a later round's fix should invert it once an `else`
raises something that names the caller's mistake instead.

D622, RECURRING: `:681`'s `.filter_by(community_id=community_id)` had NO
same-mechanism negative control before the test added below. Every
`make_community_flair` call above attaches to `s.community`, and no test
ever gave a SECOND community a `CommunityFlair` row -- so at the point any
of those five tests ran, the seeded flair was the only `CommunityFlair` row
in the whole database, and deleting the `filter_by(...)` outright (leaving
`CommunityFlair.query.order_by(CommunityFlair.flair).all()`) would still
return exactly that row. This is false-witness mechanism (c): emptiness
with no same-mechanism negative control, and it is a second instance of the
same gap the previous sub-project registered as D622 against
`remove_mod_from_community`'s `community_id` predicate in
tests/test_shared_community_moderation.py -- the bystander community there,
same as here, only defends a mutant that HARDCODES `community_id=1`; it does
nothing against a mutant that deletes the filter entirely, because nothing
ever populated the bystander with a row of the kind under query. `_seed()`
below now returns the bystander as `.bystander` specifically so a flair can
be attached to it and excluded, closing this gap for `get_comm_flair_list`
and handing later tasks in this round a ready-made second community for
their own cross-community negative controls.

`comm_flair_ap_format` (app/shared/community.py:684-707). THE THREE INPUT
TYPES: `:685-686` (`int`, via `CommunityFlair.query.get(flair)`), `:687-688`
(`str`, via `.filter_by(ap_id=flair).first()`), and a bare `CommunityFlair`
instance, which matches neither `isinstance` check and passes through
`:685-688` unchanged. THE `:686` DISTINCTION, NOT TO BE FLATTENED: `.get()`
returning `None` for a missing primary key is handled by `:690-691`'s guard
below it -- this is NOT the same defect as this round's production fix at
`:130` and `:196`, where a `.get()` result is dereferenced immediately with
no intervening guard. The two tests below that pass a non-existent `int`/
`str` cover `:690-691`'s bare `return` (the signature declares `-> dict`;
returning `None` is a real mismatch, registered here as a finding and left
unfixed per this round's production budget) and assert the result IS `None`,
not merely falsy.

THE `:698-699` UNREACHABILITY PROOF (established by reading, not assumed):
`:696-699` is
    if not flair.ap_id:
        ap_id = flair.get_ap_id()
        if not ap_id:
            return
`get_ap_id()` (app/models.py:4305-4313) is only called here when
`flair.ap_id` is already falsy (the `:696` guard), so its own early return
at `:4306-4307` (`if self.ap_id: return self.ap_id`) never fires from this
call site -- every call reaching `:698` falls through to `:4309-4313`:
`community = db.session.query(Community).get(self.community_id)`,
`self.ap_id = community.local_url() + f"/tag/{self.id}"`, `db.session.
commit()`, `return self.ap_id`. `Community.local_url()` (app/models.py:
798-802) returns `self.ap_profile_id` when `is_local()` is true and
`f"{SERVER_URL}/c/{self.ap_id}"` otherwise. Case A, `is_local()` true and
`ap_profile_id` truthy: `local_url()` returns a non-empty string, string-
concatenation with `f"/tag/{id}"` at `:4311` is always non-empty, so
`self.ap_id` is truthy -- `:698` is false. Case B, `is_local()` true and
`ap_profile_id` is `''` (empty but not `None`): `local_url()` returns `''`,
and `'' + f"/tag/{id}"` is STILL a non-empty, truthy string (`"/tag/5"`) --
`:698` is still false. Case C, `is_local()` true and `ap_profile_id` is
`None`: `local_url()` returns `None`, and `:4311`'s `None + f"/tag/{id}"`
raises `TypeError` before the assignment completes -- `:698` is never
reached at all, by a crash, not a falsy return. Case D, `is_local()` false:
`local_url()` returns `f"{SERVER_URL}/c/{self.ap_id}"`, a non-empty string
by construction (an `is_local()`-false `CommunityFlair`'s owning community
has a non-`None` `ap_id`, per `:795-796`'s first disjunct, and `SERVER_URL`
is a non-empty app config value) -- truthy, `:698` false. EVERY case is
either a truthy `ap_id` (`:698` false, falls through to `:701-707`) or a
crash before `:698` runs at all (Case C) -- there is no case in which
`ap_id` is assigned a falsy value and `:698`'s body at `:699` executes.
This was checked empirically before being written here: a throwaway test
seeding a `Community` with `ap_profile_id=None` (Case C) confirmed
`local_url() is None` and `flair.get_ap_id()` raising `TypeError`, exactly
as derived above, then was discarded -- it is not part of this file because
a crash is not evidence a falsy return is reachable, per this round's
explicit instruction not to accept one as proof either way.

CAUSE CHECK, FACT 75: none of the eight catalogued causes names this shape
precisely, and none is force-fitted here. `:698`'s clause is a single
condition on one local variable, not a compound `and` of two conjuncts of
the SAME expression, so cause 3 (subsumption) does not apply as written --
that cause's own example is two conjuncts of one boolean expression, and
this is one condition tested after a separate prior *statement* (`:697`'s
assignment), not after an earlier conjunct of `:698` itself. Cause 4(b)
("an invariant established BEFORE the guard runs -- by a caller, or by an
enclosing guard") is the closest textual neighbor, but its own two named
mechanisms are a caller passing pre-validated input and an enclosing `if`;
the invariant here is established by neither -- it is established by the
return-value contract of the callee invoked on the immediately preceding
line, which the text does not name as one of the two mechanisms it lists.
Causes 1, 2 and 5 are about a fixture's inability to populate an excluded
value or a column's own domain, not a callee's return contract. Cause 6 is
explicitly "the only cause on this list that is not about a clause" and
`:698` is a clause, so it is excluded on its own terms. Cause 7 is scoped to
"an ARM OF A CONDITIONAL EXPRESSION" (a ternary); `:696-699` is a plain
`if`/`return` statement, not a ternary, so cause 7's scope excludes it even
though its proof TECHNIQUE (read the callee's statements, show no path
returns the value that would discriminate) is exactly the technique used
above. Cause 8 is narrowed by its own text to "a `try`/`except` whose body
can never run"; there is no `try`/`except` here. Stated plainly, per this
round's brief allowing exactly this outcome: no catalogued cause fits this
survivor precisely, so it is recorded here as unreachable by direct proof
rather than tagged with a cause number that does not, on a text-first
reading, describe it. Consequently `comm_flair_ap_format` cannot reach
`missing_lines: []` / `missing_branches: []` by any legitimate test -- line
`:699` and the `:698`-true arc are asserted below to be the coverage
ceiling, not a gap left by an incomplete test suite.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import NoResultFound

from app import db
from app.models import Community, CommunityFlair, CommunityInvitation
from app.shared.community import comm_flair_ap_format, create_invite_token, get_comm_flair_list
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
    """An instance, a non-admin user, a local community, and a bystander
    community, with id 1 burned.

    The bystander is minted FIRST, purely to consume Community id 1.
    conftest.py:131-132 resets every sequence between tests, so without this
    the returned community's id would deterministically be 1 in every test,
    and a mutant hardcoding `community_id=1` anywhere in this round's later
    tasks would be indistinguishable from correct code by any assertion
    built from this seed alone.

    The bystander is also returned as `.bystander` so a caller can attach a
    row to it and assert that row is EXCLUDED from a query scoped to
    `.community` -- a same-mechanism negative control that a mutant deleting
    a `community_id` filter entirely (as opposed to one hardcoding
    `community_id=1`) needs to be caught, per this file's D622 note above.
    Before this round, nothing populated the bystander with a matching row
    of any kind, so that class of mutant went unkilled; Tasks 2-5 can reuse
    `.bystander` for their own cross-community negative controls rather than
    minting a second community themselves.
    """
    _burn_a_seed()
    instance = make_instance('test.piefed.local')
    user = make_user(instance, 'alice', local=True)
    bystander = make_community('bystander', host='bystander.example')
    community = make_community()
    return SimpleNamespace(instance=instance, user=user, community=community, bystander=bystander)


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


def test_get_comm_flair_list_out_of_contract_arg_raises_UnboundLocalError(app, db_session):
    """FINDING 3, PINNED NOT FIXED (see module docstring). The signature
    declares `Community | int | str`, but `:668-673`'s `if`/`elif`/`elif`
    chain has no final `else`, so passing something else entirely --
    `None`, chosen here as an unambiguous out-of-contract value no caller
    should pass -- satisfies none of the three `isinstance` checks.
    `community_id` is therefore never bound by any of `:669`, `:672`, or
    `:679`, and `:681`'s `CommunityFlair.query.filter_by(community_id=
    community_id)` raises `UnboundLocalError` reading a name that was never
    assigned on this code path.

    This is pinned as an honest description of today's behaviour, not the
    intended one: the failure names the internal variable `community_id`
    rather than the contract the caller actually broke (passing a type
    outside `Community | int | str`), which is exactly why this is
    registered as a finding rather than silently accepted. A later round
    that adds an `else` raising a caller-facing error (e.g. `TypeError`)
    should invert this test rather than leave it pinning `UnboundLocalError`.
    """
    _seed()

    with pytest.raises(UnboundLocalError):
        get_comm_flair_list(None)


def test_get_comm_flair_list_excludes_a_flair_belonging_to_another_community(
        app, db_session):
    """Kills the mutant that deletes `:681`'s `filter_by(community_id=
    community_id)` outright, leaving `CommunityFlair.query.order_by
    (CommunityFlair.flair).all()`. See this file's D622 note above: every
    OTHER test in this file attaches its `CommunityFlair` rows only to
    `s.community`, so at the point any of them run, the seeded flair is the
    only `CommunityFlair` row in the database and a query with no
    `community_id` filter at all would still return exactly it -- a
    same-mechanism false witness (emptiness with no same-mechanism negative
    control). This test attaches a flair to `s.bystander` instead, a SECOND
    community distinct from `s.community`, so a deleted filter would pull in
    both rows.

    The assertion compares the returned NAME SET, not just a count of 1: a
    mutant that swapped which community's flair got returned (rather than
    just adding the bystander's on top) would still pass a bare count
    assertion but fails this one, since `{'wanted'} != {'unwanted'}`.
    """
    s = _seed()
    make_community_flair(s.community, name='wanted')
    make_community_flair(s.bystander, name='unwanted')

    result = get_comm_flair_list(s.community)

    assert {f.flair for f in result} == {'wanted'}


# `comm_flair_ap_format` (app/shared/community.py:684-707).


def test_comm_flair_ap_format_int_arg_with_existing_ap_id_returns_full_dict(
        app, db_session):
    """`:685-686`'s true arm: `isinstance(flair, int)` is true, and
    `CommunityFlair.query.get(flair)` finds the row by primary key.

    `:696`'s FALSE arm: `flair.ap_id` is already set, so `:697-699`'s
    minting path never runs here -- that arm is covered separately by
    `test_comm_flair_ap_format_instance_arg_mints_and_persists_ap_id` below.

    Every key `:701-706` writes is asserted by VALUE via one dict equality,
    not by presence or truthiness, so a mutant dropping any single line (or
    swapping two right-hand sides, e.g. `backgroundColor`/`textColor`) fails
    this assertion. `text_color`/`background_color`/`blur_images` are set to
    distinct, non-default values below specifically so such a swap cannot
    hide behind two fields that happen to start out equal.
    """
    s = _seed()
    flair = make_community_flair(
        s.community, name='alpha', ap_id='https://test.piefed.local/c/x/tag/existing')
    flair.text_color = '#ff0000'
    flair.background_color = '#00ff00'
    flair.blur_images = True
    db.session.commit()

    result = comm_flair_ap_format(flair.id)

    assert result == {
        'type': 'CommunityPostTag',
        'id': 'https://test.piefed.local/c/x/tag/existing',
        'preferredUsername': 'alpha',
        'textColor': '#ff0000',
        'backgroundColor': '#00ff00',
        'blurImages': True,
    }


def test_comm_flair_ap_format_int_arg_missing_id_returns_none(app, db_session):
    """`:685-686`'s true arm with a primary key matching no row:
    `CommunityFlair.query.get(flair)` -- SQLAlchemy's legacy `Query.get`,
    unrelated to the `.get()` sites this round's production fix addresses at
    `:130`/`:196` (see the module docstring) -- returns `None` quietly, and
    `:690-691`'s guard catches it. The signature declares `-> dict`;
    returning bare `None` here is a real mismatch with that annotation,
    registered as a finding and left unfixed per this round's production
    budget, not smoothed over by asserting mere falsiness.
    """
    _seed()

    result = comm_flair_ap_format(999999)

    assert result is None


def test_comm_flair_ap_format_str_arg_returns_full_dict(app, db_session):
    """`:687-688`'s true arm: `isinstance(flair, str)` is true, and
    `.filter_by(ap_id=flair).first()` finds the row by its `ap_id` column.
    """
    s = _seed()
    flair = make_community_flair(
        s.community, name='beta', ap_id='https://test.piefed.local/c/x/tag/beta')
    flair.text_color = '#111111'
    flair.background_color = '#222222'
    flair.blur_images = False
    db.session.commit()

    result = comm_flair_ap_format('https://test.piefed.local/c/x/tag/beta')

    assert result == {
        'type': 'CommunityPostTag',
        'id': 'https://test.piefed.local/c/x/tag/beta',
        'preferredUsername': 'beta',
        'textColor': '#111111',
        'backgroundColor': '#222222',
        'blurImages': False,
    }


def test_comm_flair_ap_format_str_arg_no_match_returns_none(app, db_session):
    """`:687-688`'s true arm with an `ap_id` matching no row:
    `.filter_by(ap_id=flair).first()` returns `None` quietly -- no
    exception, unlike `get_comm_flair_list`'s `.one()` fallback above -- and
    `:690-691` returns bare `None`.
    """
    _seed()

    result = comm_flair_ap_format('no-such-ap-id')

    assert result is None


def test_comm_flair_ap_format_instance_arg_mints_and_persists_ap_id(app, db_session):
    """The third input type, per the module docstring's `:698-699` proof:
    a bare `CommunityFlair` instance matches neither `:685`'s nor `:687`'s
    `isinstance` check, so `flair` passes through `:685-688` unchanged with
    no query run.

    Also covers `:696`'s TRUE arm: `flair.ap_id` starts `None`, so `:697`'s
    `flair.get_ap_id()` mints one via `app/models.py:4305-4313`, which
    assigns `community.local_url() + f"/tag/{self.id}"` and commits --
    `s.community` is local with a non-`None` `ap_profile_id`
    (`tests/factories.py:143`), landing in Case A of the module docstring's
    proof, so the mint always succeeds here and `:698`'s guard is false.
    `db.session.expire_all()` before the re-query below forces SQLAlchemy to
    re-read `ap_id` from the database rather than returning the in-memory
    attribute this test already holds a reference to, so the assertion
    actually proves the commit at `app/models.py:4312` reached the database,
    not merely that the Python object was mutated.
    """
    s = _seed()
    flair = make_community_flair(s.community, name='gamma', ap_id=None)
    flair.text_color = '#333333'
    flair.background_color = '#444444'
    flair.blur_images = True
    db.session.commit()
    assert flair.ap_id is None

    result = comm_flair_ap_format(flair)

    expected_ap_id = s.community.ap_profile_id + f'/tag/{flair.id}'
    assert result == {
        'type': 'CommunityPostTag',
        'id': expected_ap_id,
        'preferredUsername': 'gamma',
        'textColor': '#333333',
        'backgroundColor': '#444444',
        'blurImages': True,
    }
    db.session.expire_all()
    persisted = db.session.query(CommunityFlair).filter_by(id=flair.id).one()
    assert persisted.ap_id == expected_ap_id
