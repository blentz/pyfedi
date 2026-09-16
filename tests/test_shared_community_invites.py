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

`invite_with_chat` (app/shared/community.py:121-173), FIRST HALF ONLY --
`:122-143` and `:173`. Task 5 covers `:144-172`, the software-fork half; a
scope ruling assigned `:173`'s bare `return 0` here because it is the
fall-through of `:129`'s guard, which this task owns.

THE `:129` THREE-OPERAND GUARD, INDEPENDENCE CHECKED BEFORE ASSUMING IT:

    if recipient and not recipient.banned and not instance_banned(recipient.instance.domain):

`recipient` (a lookup result that may be `None`), `recipient.banned` (a
boolean column on the found `User` row), and `instance_banned(recipient.
instance.domain)` (a query against a DIFFERENT table, `BannedInstances`,
keyed on the recipient's instance's domain) are three genuinely separate
data sources: a `User` row's own ban flag does not derive from, or get
derived from, its instance's presence in `BannedInstances`, and neither
predicate constrains what `search_for_user` can return. This is NOT
`delete_community:494`'s shape (sub-project 46, fact 75 cause 3): that
guard's first operand, `is_owner(user)`, was PROVABLY SUBSUMED by its
second, `is_moderator(user)`, because `is_moderator()`'s own body tested
membership in a list built by unioning owners into it -- true owner
implied true moderator by construction, so no input could make the first
operand true while the second was false. Here, nothing analogous holds:
a `User.banned=True` row on an unbanned instance, and a `User.banned=False`
row on a banned instance, are both perfectly constructible rows -- neither
predicate's truth value constrains the other's. This was verified
empirically, not just algebraically, by the three tests below: each holds
the OTHER two operands at their PASSING value and flips exactly one to
failing, and each independently drives the guard false and the function to
`:173`'s `return 0` -- three configurations that are not restatements of
each other, and are not in lockstep with each other (mechanism (e)'s
concern) because each test's failing operand differs while the other two's
values are held at whichever combination makes them irrelevant or passing.

FORMER PIN, `:130-131`, NOW FIXED (TASK 6), NAMED DISTINCTLY FROM TASK 3'S:
`community: Community = db.session.query(Community).get(community_id)`
immediately followed by `if community.banned:` used to share the exact
`.get()`-then-dereference shape Task 3 pinned at `invite_with_email:196-197`
(formerly `test_invite_with_email_missing_community_raises_AttributeError`)
-- a missing id made `.get()` return `None` quietly, and the very next line
dereferenced it, raising `AttributeError: 'NoneType' object has no
attribute 'banned'`. Task 3's test and this task's test asserted the
identical EXCEPTION TYPE at two DIFFERENT call sites, so this one was named
`test_invite_with_chat_missing_community_raises_AttributeError` --
distinct from Task 3's `test_invite_with_email_missing_community_raises_
AttributeError` by function name -- so that Task 6's fix of both call
sites (each rewritten to `.filter_by(id=...).one()`) could invert both
without a name collision. Task 6 has since landed: both sites now raise
`sqlalchemy.exc.NoResultFound`, and the two tests are renamed
`test_invite_with_chat_missing_community_raises_NoResultFound` and
`test_invite_with_email_missing_community_raises_NoResultFound`
respectively.

FOUR PATHS, ONE RETURN VALUE: `:129`'s false arm (via `:173`), `:132`'s
banned-community arm, and `:172`'s failure arm (`reply` falsy; Task 5's)
all return plain `0`, and `app/community/routes.py:2614` sums exactly this
return value into a count it reports to the requesting user as "invites
sent" -- indistinguishable to that caller. Every test below for a
`return 0` path therefore asserts WHICH path ran by checking for the
absence of a `Conversation` row (created only at `:134-137`, strictly
inside the guard and after the banned-community check), not merely that
the return value equals `0`.

MECHANISM (c) FOR THE SUCCESS PATH: the two message-fork tests both assert
a `Conversation` was created with exactly `{user, recipient}` as its
members. A bare `Conversation.query.first()` after the call would pass
even under a mutant that created the RIGHT SHAPE of conversation attached
to the WRONG pair of users, if the table were otherwise empty at that
point (the same false-witness-by-emptiness gap this file's D622 note
registered against `get_comm_flair_list`). Both tests seed an unrelated
decoy `Conversation` (via `make_conversation`, between two bystander users
who play no other role) BEFORE calling `invite_with_chat`, then find the
new row by filtering on its member-id SET (`{m.id for m in c.members} ==
{inviter.id, recipient.id}`) rather than assuming order or position --
per this round's ban on ordered assertions over query-planner rows -- and
assert there is exactly one such row, distinct from the untouched decoy.

MESSAGE-FORK VALUE-SWAP RESISTANCE, `:140-143`: `s.community.title` is set
to a string DIFFERENT from `s.community.name` before these two tests run.
`Community.link()` returns `self.name` (`:706`, `ap_id` is `None` for every
community this suite's factory builds) and `Community.display_name()`
returns `self.title` (`:700`, same `ap_id is None` condition) -- with
`title == name` (the factory default, since `make_community(name=...)`
also sets `title=name`), a mutant that swapped WHICH of the two methods
`:141`/`:143` call would produce byte-identical message text, and no
assertion on the rendered string could tell the swap from correct code.
Diverging `title` from `name` first makes the two calls' outputs diverge,
so a value-swap mutant is caught by an exact-substring assertion built
from the CORRECT method's output. `:144-146`'s `community.link()` (in the
subscribe-link suffix, reached identically on BOTH the private and public
arms for a local recipient at default `invitations`) is why neither test
asserts `community.link() not in message` as a blanket negative check --
that substring legitimately appears in every such message regardless of
the `:140` fork, on Task 5's territory this task's tests merely pass
through rather than target. Instead each test asserts (a) the correct
fork's exact phrase, INCLUDING its correct interpolated value, is present,
which a value-swap alone already falsifies since `link() != display_name()`
here, and (b) the OTHER fork's distinctive wording (`"check it out"` for
public, `"the private community called"` for private) is wholly absent,
which catches a mutant that swaps the entire branch body rather than only
its interpolated value.

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
from flask import render_template
from sqlalchemy.exc import NoResultFound

from app import db
from app.constants import INVITE_APPLY, SRC_API, SRC_WEB
from app.models import Community, CommunityFlair, CommunityInvitation, Conversation
from app.shared.community import (comm_flair_ap_format, create_invite_token, get_comm_flair_list,
                                  invite_with_chat, invite_with_email)
from app.utils import markdown_to_html
from tests.factories import (bearer, make_community, make_community_flair, make_conversation,
                             make_instance, make_user, web_ctx)


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

    A SECOND `CommunityFlair` row (`decoy`) is seeded first, with a
    different id, name, ap_id and colors, so this test also kills the
    mutant that deletes `:686`'s primary-key lookup outright -- turning
    `CommunityFlair.query.get(flair)` into a bare `CommunityFlair.
    query.first()`. Without a decoy, the table would hold exactly one row
    at the point this test runs, and `.first()` would return that same row
    by emptiness alone: false-witness mechanism (c), the same gap this
    file's D622 note registered against `get_comm_flair_list`'s
    `community_id` filter. With the decoy present (and created first, so
    it holds the lower id), `.first()` returns the WRONG row, and the
    dict-equality assertion below -- which pins the WANTED flair's own
    values, not merely "some dict came back" -- catches it.
    """
    s = _seed()
    decoy = make_community_flair(
        s.bystander, name='decoy', ap_id='https://test.piefed.local/c/decoy/tag/decoy')
    decoy.text_color = '#000000'
    decoy.background_color = '#000000'
    decoy.blur_images = False
    db.session.commit()
    flair = make_community_flair(
        s.community, name='alpha', ap_id='https://test.piefed.local/c/x/tag/existing')
    flair.text_color = '#ff0000'
    flair.background_color = '#00ff00'
    flair.blur_images = True
    db.session.commit()
    assert flair.id != decoy.id

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

    A `CommunityFlair` row is seeded so the table is NOT empty at the point
    `comm_flair_ap_format` runs -- otherwise a mutant that deletes `:686`'s
    primary-key lookup (bare `CommunityFlair.query.first()`) would also see
    an empty table and return `None` for the wrong reason, an
    indistinguishable false witness. With the row present, the mutant's
    `.first()` returns THAT row (truthy), so `flair` is no longer falsy at
    `:690` and the function falls through to the happy path and returns a
    dict, not `None` -- caught by the assertion below.
    """
    s = _seed()
    make_community_flair(
        s.community, name='present', ap_id='https://test.piefed.local/c/x/tag/present')

    result = comm_flair_ap_format(999999)

    assert result is None


def test_comm_flair_ap_format_str_arg_returns_full_dict(app, db_session):
    """`:687-688`'s true arm: `isinstance(flair, str)` is true, and
    `.filter_by(ap_id=flair).first()` finds the row by its `ap_id` column.

    A SECOND `CommunityFlair` row (`decoy`), with a DIFFERENT `ap_id`, name
    and colors, is seeded first so this test also kills the mutant that
    deletes `:688`'s `filter_by(ap_id=flair)` outright -- turning it into a
    bare `CommunityFlair.query.first()`. Without the decoy, the table would
    hold exactly the one row this test looks up, and `.first()` would
    return it by emptiness alone (the same false-witness mechanism (c) as
    the int-arg test above). With the decoy present and created first (so
    it holds the lower id), `.first()` returns the WRONG row, and the
    dict-equality assertion below -- pinned to the WANTED flair's own
    values -- catches it.
    """
    s = _seed()
    decoy = make_community_flair(
        s.bystander, name='decoy', ap_id='https://test.piefed.local/c/decoy/tag/decoy')
    decoy.text_color = '#000000'
    decoy.background_color = '#000000'
    decoy.blur_images = True
    db.session.commit()
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

    A `CommunityFlair` row with a DIFFERENT `ap_id` is seeded so the table
    is NOT empty when the lookup, which matches nothing, runs -- otherwise a
    mutant that deletes `:688`'s `filter_by(ap_id=flair)` (bare
    `CommunityFlair.query.first()`) would also see an empty table and
    return `None` for the wrong reason. With the row present, the mutant's
    `.first()` returns it (truthy) instead of `None`, caught below.
    """
    s = _seed()
    make_community_flair(
        s.community, name='present', ap_id='https://test.piefed.local/c/x/tag/present')

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


# `invite_with_email` (app/shared/community.py:189-210).


def test_invite_with_email_api_src_authorises_user_and_sends_plain_invite(
        app, db_session, monkeypatch):
    """`:190-192`'s SRC_API arm: `user_id = authorise_api_user(auth)` then
    `User.query.get(user_id)` resolves the inviter, unlike the web arm's
    bare `current_user` (covered by the sibling test below).

    `:201`'s FALSE arm: `s.community.invitations` is left at its factory
    default of `0`, which is not greater than `INVITE_APPLY` (`1`), so
    `:200`'s plain `'subscribe'` literal survives unchanged into the
    template -- the `lemmy_link()` fallback (the sibling test below) never
    runs on this path. `:210`'s return of `1` is asserted directly.

    `send_email` is patched on `app.shared.community`, never on its source
    module `app.email`: `from app.email import send_email` (`:17`) binds the
    name into THIS module's globals at import time, so a patch on
    `app.email.send_email` would leave the already-bound reference here
    untouched and the real network/SMTP path would still run. Every
    argument `send_email` receives is asserted by VALUE -- the subject, the
    from-address string, the recipient list, and the rendered body (built
    independently here via the same `render_template` call with the same
    context, so this test does not merely echo the production code's own
    string-building back at itself for the parts that differ per test:
    `user`, `community`, and `subscribe`) -- not merely that it was called
    once. A mutant that called `send_email` with a corrupted subject, the
    wrong recipient, or a hand-rolled body would still pass a bare
    call-count assertion but fails every one of these.
    """
    s = _seed()
    calls = []
    monkeypatch.setattr('app.shared.community.send_email',
                        lambda *a, **kw: calls.append(a))

    result = invite_with_email(s.community.id, 'invitee@example.com', SRC_API, bearer(s.user))

    assert result == 1
    assert len(calls) == 1
    subject, sender, recipients, text_body, html_body = calls[0]
    assert subject == f"{s.community.display_name()} on {app.config['SERVER_NAME']}"
    assert sender == f"{s.user.display_name()} <{app.config['MAIL_FROM']}>"
    assert recipients == ['invitee@example.com']
    expected_message = render_template(
        'email/invite_to_community.txt', user=s.user, community=s.community,
        host=app.config['SERVER_URL'], subscribe='subscribe')
    assert text_body == expected_message
    assert html_body == markdown_to_html(expected_message)


def test_invite_with_email_web_src_uses_current_user_and_lemmy_link_when_apply_required(
        app, db_session, monkeypatch):
    """`:193-194`'s else arm: `user = current_user`, the web-request path's
    sibling to the SRC_API arm above.

    `:201`'s TRUE arm: `s.community.invitations` is raised above
    `INVITE_APPLY` (`1`), so `:202` sets `subscribe = f'accept_invite/
    {user.lemmy_link()}'` instead of the plain `'subscribe'` literal.

    THIS SHAPE IS DELIBERATE, NOT A DEFECT -- unlike this module's other
    `.get()` finding pinned separately below. `invite_with_chat:148` mints a
    real, single-use token via `create_invite_token` because its recipient
    already has an account row to key the resulting `CommunityInvitation` on
    (`community_id`, `recipient.id`). An email invitee has no account yet --
    there is no `recipient.id` to key a token row on -- so no such token can
    exist for them, and the emailed link instead carries the INVITER's own
    handle. `community_invite_accept` (app/community/routes.py:2641) opens
    with `if '@' in token:` at `:2644` specifically to detect this shape (a
    `lemmy_link()` always contains an `@`, confirmed below) and flashes
    "Ask %(token)s to send an invite to %(current_user)s" rather than treat
    the handle as a real, redeemable token. This test asserts that the
    `lemmy_link()` form actually reaches the rendered template body, so a
    later round reading `:202` beside `:148` does not "fix" this deliberate
    fallback into matching the chat arm's real-token shape.
    """
    s = _seed()
    s.community.invitations = INVITE_APPLY + 1
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.community.send_email',
                        lambda *a, **kw: calls.append(a))

    with web_ctx(app, s.user):
        result = invite_with_email(s.community.id, 'invitee@example.com', SRC_WEB)

    assert result == 1
    assert len(calls) == 1
    subject, sender, recipients, text_body, html_body = calls[0]
    assert subject == f"{s.community.display_name()} on {app.config['SERVER_NAME']}"
    assert sender == f"{s.user.display_name()} <{app.config['MAIL_FROM']}>"
    assert recipients == ['invitee@example.com']
    expected_subscribe = f'accept_invite/{s.user.lemmy_link()}'
    assert '@' in s.user.lemmy_link()
    expected_message = render_template(
        'email/invite_to_community.txt', user=s.user, community=s.community,
        host=app.config['SERVER_URL'], subscribe=expected_subscribe)
    assert text_body == expected_message
    assert html_body == markdown_to_html(expected_message)


def test_invite_with_email_api_src_with_invitations_above_apply_uses_lemmy_link(
        app, db_session, monkeypatch):
    """D469 (docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md
    :11352), false-witness mechanism (e): two independent conditions
    exercised only in lockstep with each other cannot detect a mutant that
    CONJOINS them, however precise the assertions on the resulting state.

    Every OTHER test above pairs `src == SRC_API` with `:201`'s FALSE arm
    (the first test), or `SRC_WEB` with its TRUE arm (the previous test) --
    never crossed. A reviewer proved by hand-applied mutation that
    `if community.invitations > INVITE_APPLY:` at `:201` can be silently
    rewritten to `if community.invitations > INVITE_APPLY and src ==
    SRC_WEB:` with the full file still reporting `20 passed`: every existing
    test's `src` and `:201` truth value already agreed with that added
    conjunct, so the conjunct was free -- it never had to fire.

    This test BREAKS the lockstep: `src == SRC_API` (via `bearer`), crossed
    with `:201`'s TRUE arm (`invitations` raised above `INVITE_APPLY` as in
    the sibling test above). Under the mutant that conjoins `and src ==
    SRC_WEB`, this test's `src` is `SRC_API`, so the conjoined condition
    is FALSE even though `invitations > INVITE_APPLY` is TRUE -- `:200`'s
    plain `'subscribe'` literal would survive instead of `:202`'s
    `lemmy_link()` form, and the `subscribe`/body assertions below catch
    that divergence directly (verified by hand-applying the mutant and
    running this file; see the task report for the exact failure text,
    restored afterwards). The sibling SRC_WEB+true-arm test above supplies
    the other half: it dies under the MIRROR-IMAGE mutant that conjoins
    `and src == SRC_API` instead, since ITS `src` is `SRC_WEB`. Between the
    two tests, both conjoining directions are caught; neither alone would
    be.
    """
    s = _seed()
    s.community.invitations = INVITE_APPLY + 1
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.community.send_email',
                        lambda *a, **kw: calls.append(a))

    result = invite_with_email(s.community.id, 'invitee@example.com', SRC_API, bearer(s.user))

    assert result == 1
    assert len(calls) == 1
    subject, sender, recipients, text_body, html_body = calls[0]
    assert subject == f"{s.community.display_name()} on {app.config['SERVER_NAME']}"
    assert sender == f"{s.user.display_name()} <{app.config['MAIL_FROM']}>"
    assert recipients == ['invitee@example.com']
    expected_subscribe = f'accept_invite/{s.user.lemmy_link()}'
    assert '@' in s.user.lemmy_link()
    expected_message = render_template(
        'email/invite_to_community.txt', user=s.user, community=s.community,
        host=app.config['SERVER_URL'], subscribe=expected_subscribe)
    assert text_body == expected_message
    assert html_body == markdown_to_html(expected_message)


def test_invite_with_email_banned_community_returns_0(app, db_session, monkeypatch):
    """`:197`'s TRUE arm: a banned community makes `invite_with_email`
    return `0` immediately -- `:200-209` never run, so `send_email` is never
    called at all, asserted here directly rather than inferred from the
    return value alone.
    """
    s = _seed()
    s.community.banned = True
    db.session.commit()
    calls = []
    monkeypatch.setattr('app.shared.community.send_email',
                        lambda *a, **kw: calls.append(a))

    with web_ctx(app, s.user):
        result = invite_with_email(s.community.id, 'invitee@example.com', SRC_WEB)

    assert result == 0
    assert calls == []


def test_invite_with_email_missing_community_raises_NoResultFound(app, db_session):
    """FIX APPLIED, TASK 6. `:196` now fetches the community with
    `db.session.query(Community).filter_by(id=community_id).one()`, matching
    this module's OTHER `Community`-by-id lookup, `restore_community:522`.
    An id matching no row makes `.one()` raise `sqlalchemy.exc.NoResultFound`
    -- a recognisable, specific error -- instead of quietly returning `None`
    for `:197`'s `community.banned` to dereference into an opaque
    `AttributeError: 'NoneType' object has no attribute 'banned'`.

    `invite_with_chat:130` shared the old `.get()`-then-dereference shape and
    was fixed identically by this same Task 6 change; it is covered by a
    SEPARATELY NAMED test,
    `test_invite_with_chat_missing_community_raises_NoResultFound`, so the
    two do not collide on a shared test name.

    THIS TEST IS THE INVERSION of the former PIN,
    `test_invite_with_email_missing_community_raises_AttributeError`, which
    asserted the pre-fix `AttributeError` and passed against the code before
    this fix. That test's name and assertion no longer apply.
    """
    s = _seed()

    with web_ctx(app, s.user):
        with pytest.raises(NoResultFound):
            invite_with_email(999999, 'invitee@example.com', SRC_WEB)


# `invite_with_chat` (app/shared/community.py:121-173), first half only:
# `:122-143` and `:173`. See the module docstring above for the `:129`
# independence proof, the pin's naming rationale, the four-paths-return-0
# problem, and this section's mechanism (c)/(e) mitigations.


def test_invite_with_chat_no_matching_recipient_returns_0_and_creates_no_conversation(
        app, db_session, monkeypatch):
    """`:129`'s FIRST operand isolated: `search_for_user(handle)` is patched
    to return `None` -- no such handle -- so `recipient` is falsy and the
    `and`-chain short-circuits before either `recipient.banned` or
    `instance_banned(...)` is ever evaluated. Control falls through to
    `:173`'s bare `return 0`.

    `search_for_user` is patched on `app.shared.community`, never on its
    source module `app.user.utils`: `from app.user.utils import
    search_for_user` (`:22`) binds the name into THIS module's globals at
    import time, so a patch on the source module would leave the
    already-bound reference here untouched.

    Asserting `Conversation.query.count() == 0` (not merely `result == 0`)
    is what distinguishes this path from `:132`'s banned-community arm and
    `:172`'s failure arm -- see the module docstring's FOUR PATHS note --
    all three return the identical `0`.
    """
    s = _seed()
    monkeypatch.setattr('app.shared.community.search_for_user', lambda handle: None)

    result = invite_with_chat(s.community.id, 'nobody', SRC_API, bearer(s.user))

    assert result == 0
    assert db.session.query(Conversation).count() == 0


def test_invite_with_chat_banned_recipient_returns_0_and_creates_no_conversation(
        app, db_session, monkeypatch):
    """`:129`'s SECOND operand isolated: `search_for_user` is patched to
    return a real, banned `User` row, while `instance_banned` is patched to
    return `False` -- the recipient's OWN ban flag is what drives the guard
    false here, not their instance's status, which is held at its passing
    value.

    Both `search_for_user` and `instance_banned` are patched on
    `app.shared.community`, matching how `:22`/`:24-27` bind those names
    into this module's globals (see the sibling test above and the
    `invite_with_email` tests' `send_email` patches for the same pattern).
    """
    s = _seed()
    recipient = make_user(s.instance, 'banneduser', local=True)
    recipient.banned = True
    db.session.commit()
    monkeypatch.setattr('app.shared.community.search_for_user', lambda handle: recipient)
    monkeypatch.setattr('app.shared.community.instance_banned', lambda domain: False)

    with web_ctx(app, s.user):
        result = invite_with_chat(s.community.id, 'banneduser', SRC_WEB)

    assert result == 0
    assert db.session.query(Conversation).count() == 0


def test_invite_with_chat_banned_instance_returns_0_and_creates_no_conversation(
        app, db_session, monkeypatch):
    """`:129`'s THIRD operand isolated: `search_for_user` is patched to
    return a real recipient with `banned=False` (the second operand held at
    its passing value), while `instance_banned` is patched to return `True`
    regardless of the domain it is passed -- the recipient's INSTANCE being
    banned is what drives the guard false here.

    Together with the two tests above, these three configurations flip
    exactly one operand each away from its passing value while holding the
    other two passing (or, for the first test, irrelevant post-short-circuit)
    -- the empirical half of the module docstring's independence proof: no
    two of these three tests vary the same operand in lockstep with another,
    per this round's mechanism (e) caution.
    """
    s = _seed()
    recipient = make_user(s.instance, 'remoteuser', local=True)
    recipient.banned = False
    db.session.commit()
    monkeypatch.setattr('app.shared.community.search_for_user', lambda handle: recipient)
    monkeypatch.setattr('app.shared.community.instance_banned', lambda domain: True)

    result = invite_with_chat(s.community.id, 'remoteuser', SRC_API, bearer(s.user))

    assert result == 0
    assert db.session.query(Conversation).count() == 0


def test_invite_with_chat_missing_community_raises_NoResultFound(app, db_session):
    """FIX APPLIED, TASK 6 -- see the module docstring's PIN section above
    for the full rationale and why this is named distinctly from
    `test_invite_with_email_missing_community_raises_NoResultFound`.

    `:129`'s guard is genuinely TRUE here (a real, unbanned local recipient
    on a real, unbanned instance), so control reaches `:130`'s
    `db.session.query(Community).filter_by(id=community_id).one()` for a
    `community_id` that matches no row. `.one()` now raises
    `sqlalchemy.exc.NoResultFound` immediately, before `:131`'s
    `community.banned` is ever evaluated.

    THE CONVERSATION-COUNT ASSERTION SURVIVES THE INVERSION UNCHANGED, AND
    STILL MATTERS: asserting the exception TYPE alone does not prove WHERE
    it was raised. Before this fix, a reviewer-applied mutant, `if src ==
    SRC_API and community.banned:` at `:131`, short-circuited on this
    test's `src=SRC_WEB` before ever touching the `None` community -- so
    `:131`'s guard was false, execution fell through to `:134-138` and
    COMMITTED a spurious `Conversation` row for the `None` community, and
    only THEN crashed at `:140`'s `community.private` read. The exception
    type was still `AttributeError` either way, so a bare
    `pytest.raises(AttributeError)` could not tell "died at :131 before
    doing anything" (real behaviour) from "did :134-138's work first, then
    died two lines later" (the mutant's) -- only the `Conversation` count
    distinguished them. Now that `:130` itself raises `NoResultFound`
    before `:131` is reached at all, the count assertion below confirms
    that no such fallthrough occurred: it must remain `0`.

    THIS TEST IS THE INVERSION of the former PIN,
    `test_invite_with_chat_missing_community_raises_AttributeError`, which
    asserted the pre-fix `AttributeError` and passed against the code
    before this fix. That test's name and assertion no longer apply.
    """
    s = _seed()
    recipient = make_user(s.instance, 'bob', local=True)

    with web_ctx(app, s.user):
        with pytest.raises(NoResultFound):
            invite_with_chat(999999, 'bob', SRC_WEB)

    assert db.session.query(Conversation).count() == 0


def test_invite_with_chat_banned_community_returns_0_and_creates_no_conversation(
        app, db_session):
    """`:131`'s TRUE arm: a banned community makes `:132` return `0` before
    any `Conversation` is created -- one of the FOUR paths sharing that same
    return value (see the module docstring), distinguished here the same
    way as the `:129`-false tests above: by the absence of a `Conversation`
    row, not merely by the return value.

    The recipient here is real and passes `:129`'s guard outright (local,
    unbanned, on an unbanned instance), so this test isolates `:131-132`
    on its own rather than conflating it with the guard.

    Uses `SRC_API`; the sibling test directly below uses `SRC_WEB` against
    an identically banned community specifically so `src` and
    `community.banned` are NOT paired in only one direction across this
    file's tests -- see that test's docstring for the mutant this decouples.
    """
    s = _seed()
    recipient = make_user(s.instance, 'carol', local=True)
    s.community.banned = True
    db.session.commit()

    result = invite_with_chat(s.community.id, 'carol', SRC_API, bearer(s.user))

    assert result == 0
    assert db.session.query(Conversation).count() == 0


def test_invite_with_chat_banned_community_web_src_returns_0_and_creates_no_conversation(
        app, db_session):
    """`:131`'s TRUE arm again, this time with `SRC_WEB` -- added after
    review found the sibling test above, the ONLY test reaching `:131`'s
    true arm before this one, always paired a banned community with
    `SRC_API`. That lockstep let a reviewer-applied mutant, `if src ==
    SRC_API and community.banned:`, pass the whole file: under `SRC_API` it
    behaves identically to the real `if community.banned:`, so nothing
    already in this file could tell the two apart.

    This test breaks that lockstep: `community.banned=True` with
    `src=SRC_WEB`. Under the real guard this still returns `0` and creates
    no `Conversation`. Under the mutant, `src == SRC_API` is FALSE, so the
    conjunction is false regardless of `community.banned`, execution falls
    through to `:134-138` and creates a `Conversation` for the (banned, but
    now unchecked) community, then continues to `:140` onward and returns
    `1` -- both assertions below fail under it. Verified by hand-applying
    the mutant and running this file: `AssertionError: assert 1 == 0` on
    the `result == 0` line (the `Conversation`-count assertion never even
    runs, since `assert` stops at the first failure), then restored.
    """
    s = _seed()
    recipient = make_user(s.instance, 'dana', local=True)
    s.community.banned = True
    db.session.commit()

    with web_ctx(app, s.user):
        result = invite_with_chat(s.community.id, 'dana', SRC_WEB)

    assert result == 0
    assert db.session.query(Conversation).count() == 0


def test_invite_with_chat_public_community_message_embeds_community_link(
        app, db_session, monkeypatch):
    """`:140`'s FALSE arm (`community.private` is `False`, the factory
    default): `:141` builds the message around `community.link()`, embedded
    in a "check it out" sentence -- see the module docstring's MESSAGE-FORK
    VALUE-SWAP RESISTANCE section for why `s.community.title` is diverged
    from `s.community.name` first, and why the assertions below check for
    the presence of the correct exact phrase and the absence of the OTHER
    arm's wording rather than a blanket absence of `link()`'s value (which
    legitimately reappears in `:146`'s subscribe-link suffix regardless of
    this fork).

    Also covers `:134-138`'s conversation creation on the success path: a
    decoy `Conversation` between two unrelated users is seeded first (the
    module docstring's mechanism (c) note), and the assertions below find
    the NEW conversation by its member-id set rather than assuming it is
    the only row or the first one returned.

    `send_message` is patched on `app.shared.community`, never on its
    source module `app.chat.util`: `from app.chat.util import send_message`
    binds the name into this module's globals at import time. The patched
    replacement returns a truthy sentinel so `:172`'s `return 1 if reply
    else 0` -- Task 5's territory, merely passed through here -- takes its
    true arm, and captures the exact `message` argument so the fork's
    content can be asserted directly.

    Uses `SRC_WEB`, same as the sibling private-arm test below (see its own
    docstring for why): a review found this file originally paired
    `SRC_WEB` with EVERY public-community test and `SRC_API` with EVERY
    private-community one, letting `if src == SRC_WEB:` stand in for
    `:140`'s real `if not community.private:` undetected. Both tests now
    share `src=SRC_WEB` and differ only in `community.private`, which
    decouples the two.
    """
    s = _seed()
    s.community.title = 'A Community Worth Joining'
    assert s.community.title != s.community.name
    db.session.commit()
    recipient = make_user(s.instance, 'dora', local=True)
    decoy_a = make_user(s.instance, 'decoy_a', local=True)
    decoy_b = make_user(s.instance, 'decoy_b', local=True)
    make_conversation(decoy_a, decoy_b)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    with web_ctx(app, s.user):
        result = invite_with_chat(s.community.id, 'dora', SRC_WEB)

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    pattern_public = (f"this community, check it out: {app.config['SERVER_URL']}/c/"
                      f"{s.community.link()}.\n\n")
    assert pattern_public in message
    assert 'the private community called' not in message

    all_conversations = db.session.query(Conversation).all()
    assert len(all_conversations) == 2
    matches = [c for c in all_conversations
              if {m.id for m in c.members} == {s.user.id, recipient.id}]
    assert len(matches) == 1


def test_invite_with_chat_private_community_message_embeds_display_name(
        app, db_session, monkeypatch):
    """`:140`'s TRUE arm: `community.private = True` makes `:143` build the
    message around `community.display_name()` instead, embedded in a
    "the private community called" sentence -- the mirror of the public-arm
    test above. See that test's docstring and the module docstring's
    MESSAGE-FORK VALUE-SWAP RESISTANCE section for the shared rationale.

    Also covers the `:134-138`/mechanism (c) success-path assertion a
    second time, with a decoy conversation seeded the same way.

    USES `SRC_WEB`, NOT `SRC_API` -- CHANGED AFTER REVIEW. This test
    originally used `SRC_API`, which meant every test in this file reaching
    `:140` paired `SRC_WEB` with a public community and `SRC_API` with a
    private one, in lockstep. A reviewer-applied mutant, `if src ==
    SRC_WEB:` in place of `:140`'s real `if not community.private:`,
    reproduced both outcomes exactly and passed the whole file: under
    `SRC_WEB` it took the `link()` arm (matching the public test, which was
    also public), and under `SRC_API` it took the `display_name()` arm
    (matching this test, which was also private) -- purely because `src`
    tracked `community.private` everywhere the fork was exercised, never
    the reverse. Switching this test to `SRC_WEB` -- now identical to the
    public-arm test's `src`, differing only in `community.private` -- makes
    the mutant's `src == SRC_WEB` branch fire for BOTH tests regardless of
    which is actually private, so this test's `pattern_private` assertion
    now fails under it: the mutant returns the `link()`-based message where
    `display_name()` was expected. Verified by hand-applying the mutant and
    running this file; the exact failure text is recorded in this round's
    task report, then the mutant was reverted.
    """
    s = _seed()
    s.community.title = 'A Secret Society'
    assert s.community.title != s.community.name
    s.community.private = True
    db.session.commit()
    recipient = make_user(s.instance, 'erin', local=True)
    decoy_a = make_user(s.instance, 'decoy_c', local=True)
    decoy_b = make_user(s.instance, 'decoy_d', local=True)
    make_conversation(decoy_a, decoy_b)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    with web_ctx(app, s.user):
        result = invite_with_chat(s.community.id, 'erin', SRC_WEB)

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    pattern_private = (f"the private community called {s.community.display_name()} on "
                       f"{app.config['SERVER_NAME']}")
    assert pattern_private in message
    assert 'check it out' not in message

    all_conversations = db.session.query(Conversation).all()
    assert len(all_conversations) == 2
    matches = [c for c in all_conversations
              if {m.id for m in c.members} == {s.user.id, recipient.id}]
    assert len(matches) == 1


# `invite_with_chat` (app/shared/community.py:121-173), SECOND half: `:144-172`,
# the software fork. A scope ruling assigned `:173` to the first half above --
# it is `:129`'s fall-through, not this fork's -- so this section's range is
# `:144-172`, not the `:144-173` the plan originally named.
#
# THE TABLE MUST BE KEYED BY BRANCH SITE, NOT BY CONDITION NAME -- LEARNED
# THE HARD WAY. An earlier draft of this section built one column named
# "apply" and aggregated `:145`, `:152` and `:161` into it, on the theory
# that "invitations <= INVITE_APPLY" is one condition wherever it appears.
# It is not, for lockstep purposes: `:145`, `:152` and `:161` are three
# separate branch sites, each with its own small set of tests reaching it,
# and each capable of hiding its OWN conjoined mutant independently of
# whatever the aggregated column showed. Review found two: `:156` (an
# analogous slip -- `local_only` was checked against `apply` and never
# against `src` at all, because the row-per-condition shape had no column
# for "this specific site's src pairing") and `:145` itself, both surviving
# an `and src == SRC_X` conjunction that the aggregated table reported as
# "decoupled" by averaging across sites where it manifestly was not. A
# systematic re-check of every remaining site by this same method (not
# prompted by review, but by applying its method everywhere) turned up a
# third instance at `:161`, fixed alongside the other two. THE CORRECTED
# TABLE HAS ONE ROW PER TEST AND ONE COLUMN PER BRANCH SITE -- `:144`,
# `:145`, `:151`, `:152`, `:156`, `:160`, `:161`, `:166`, `:172` -- plus
# `src`, recording which sites each test actually REACHES (blank, not
# `False`, when a test's control flow never evaluates that site) and what
# value it took there. It is reproduced in full in this round's task
# report, along with the pairwise check: for every two columns both reached
# by at least two tests, do the values that appear together vary
# independently rather than moving in lockstep.
#
# THE FORK HAS FIVE INDEPENDENT CONDITION *KINDS* (`recipient.is_local()`,
# `recipient.instance.software`, `invitations <= INVITE_APPLY`, `community.
# local_only`, and `src`), but the SITES are what the table tracks, since a
# condition kind repeated at multiple sites (`invitations <= INVITE_APPLY`
# at three of them) is three separate lockstep opportunities, not one.
# Mechanism (e) has now hit this file's `:129`/`:131`/`:140` (module
# docstring) and this section's own `:145`/`:156`/`:161` -- always via `src`
# silently tracking a real production condition at ONE specific site while
# looking decoupled in aggregate. Two of the three fixes here rebalance an
# existing test's `src` rather than add a test (cheaper, and avoids growing
# the file for a fix that a swap already covers); each rebalance was
# rechecked against every OTHER site-pair that test participates in before
# being accepted, so closing one site's gap does not silently reopen
# another's (the risk this section's own history shows is real).
#
# Two conjoined mutants from the ORIGINAL (pre-review) table are still
# valid kills and remain below with one correction: `is_local()` x `src` at
# `:144` (`if recipient.is_local() and src == SRC_WEB:`) kills via
# `test_invite_with_chat_local_recipient_invite_required_message_has_token_accept_invite_link`,
# but that test's failure is `sqlalchemy.exc.NoResultFound` -- A CRASH, not
# an assertion failure. A crash kill on its own is not trustworthy evidence
# of decoupling (the crash could as easily be an artifact of test plumbing
# as of the mutant), so this round also built the non-crashing sibling `if
# recipient.is_local() or src == SRC_WEB:` and confirmed it fails cleanly,
# on a plain string-containment assertion, via
# `test_invite_with_chat_remote_piefed_apply_open_message_has_remote_subscribe_link`.
# The software-family check x `invitations <= INVITE_APPLY` mutant at
# `:151` has the identical defect for the identical reason -- conjoining the
# OUTER family gate with `apply` skips `create_invite_token` entirely on the
# affected tests, so their `.one()` lookups raise `NoResultFound` rather
# than failing an assertion -- and gets the same treatment: the non-crashing
# sibling `if (...piefed check...) or community.invitations <= INVITE_APPLY:`
# was built and confirmed to fail cleanly (wrong-arm message text) via
# `test_invite_with_chat_remote_lemmy_apply_open_message_has_join_link`. All
# four mutants (two AND-conjoined, two OR-sibling) and their exact failure
# text are recorded in the task report, along with the three CRITICAL fixes
# above; none of the four changed `app/`, and all were reverted by hand.
#
# SEVEN OF EIGHT TERMINAL FORMS APPEND TO `message`; `:167` REPLACES IT.
# Every append-arm test below asserts its own arm's distinguishing exact
# substring is present AND the substrings unique to every SIBLING arm within
# the same immediate `if`/`elif`/`else` are absent -- a mutant swapping two
# neighbouring arms (e.g. `:153`'s open-apply text for `:159`'s token text)
# must fail at least one of these tests. `:157` and `:165` happen to build
# BYTE-IDENTICAL text (same f-string, reached via two unrelated branches --
# piefed/pylova with `local_only` true, versus lemmy/mbin requiring a
# token) -- this is not a value-swap gap to defend against, since swapping
# two textually identical statements produces no mutant at all. `:167`
# REPLACES `message` (`=`, not `+=`); its test builds the exact expected
# string via the same `render_template` call invite_with_email's own
# `:167`-sibling tests use, then asserts the REPLACED-away `:141` greeting
# phrase (`'check it out'`) is absent from the final message -- the only
# assertion a `+=` mutant at `:167` fails, since a blanket "template text
# present" check would still pass under it.
#
# `:151` and `:160` EACH GET A MIXED-CASE SOFTWARE VALUE (`'PieFed'` at
# `:151`'s first test, `'LEMMY'` at `:160`'s first test) so a mutant
# deleting either site's `.lower()` call dies at that site specifically --
# every other software value below is lower-case already and would pass
# under such a mutant, so mixed case is not spread across every test, only
# at least once per site as the brief requires.
#
# `:170-172`'s RETURN FORK is orthogonal to the message fork: `reply =
# send_message(...)` runs identically regardless of which arm built
# `message`, so one truthy-reply test per arm (all eight below) and one
# additional falsy-reply test (the ninth) are enough to cover both `return 1
# if reply else 0` arcs without re-deriving the message fork's own table.
# The falsy-reply test is the FOURTH `return 0` path the module docstring's
# FOUR PATHS note names; unlike `:129`'s and `:132`'s arms (which return `0`
# having created NO `Conversation`), this path returns `0` AFTER `:134-138`
# already created and committed one -- so its own assertion checks the
# `Conversation` WAS created, the mirror image of the other three paths'
# assertions, keeping all four `0`-returning paths distinguishable from each
# other by more than their shared return value.


def test_invite_with_chat_local_recipient_apply_open_message_has_subscribe_link(
        app, db_session, monkeypatch):
    """`:144`'s TRUE arm (`recipient.is_local()`) and `:145`'s TRUE arm
    (`community.invitations` left at the factory default `0`, `<=
    INVITE_APPLY`): `:146` appends the plain subscribe-link sentence, and
    `:148-149`'s token-minting sibling never runs -- no `CommunityInvitation`
    row is created.

    Sites reached: `:144`=T, `:145`=T; `src`=API; `reply`=T.

    `src=API` HERE, NOT `WEB` -- CHANGED DURING THIS FIX ROUND. This test
    originally used `SRC_WEB`, and so did the falsy-delivery test below
    (the only other test reaching `:145`=T), pairing `:145`=T with `src=WEB`
    on BOTH of its samples -- the same diagonal shape `:156` and `:161` had.
    Hand-verified: `if community.invitations <= INVITE_APPLY and src ==
    SRC_WEB:` at `:145` passed the whole file (38 passed) under that
    pairing. An initial attempt to fix this by flipping the OTHER test
    (falsy-delivery) to `src=API` instead of this one did NOT work: that
    test carries no assertion on `message` content, only on the return
    value and the `Conversation` row, neither of which differs between
    `:146`'s and `:148-149`'s arms -- so a wrong-arm mutant is invisible to
    it regardless of which `src` it uses. Flipping THIS test instead (real
    `:145`=T, mutant now `T and (API==WEB is False)` = False) is what
    actually catches the mutant, since this test's assertions below check
    the exact substring `:146` produces and its absence rules out `:148-
    149`'s token substring -- both fail cleanly when the mutant takes the
    wrong arm. `:145` now has one sample at each `src` value (this test at
    API, falsy-delivery below at WEB), closing the gap in both directions
    with only two tests, unlike `:156`'s and `:161`'s single-direction
    residual (each of those has only one sample on its TRUE side).
    """
    s = _seed()
    recipient = make_user(s.instance, 'fiona', local=True)
    decoy_a = make_user(s.instance, 'decoy_e', local=True)
    decoy_b = make_user(s.instance, 'decoy_f', local=True)
    make_conversation(decoy_a, decoy_b)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    result = invite_with_chat(s.community.id, 'fiona', SRC_API, bearer(s.user))

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    assert f"{app.config['SERVER_URL']}/c/{s.community.link()}/subscribe." in message
    assert 'accept_invite' not in message
    assert db.session.query(CommunityInvitation).count() == 0

    all_conversations = db.session.query(Conversation).all()
    assert len(all_conversations) == 2
    matches = [c for c in all_conversations
              if {m.id for m in c.members} == {s.user.id, recipient.id}]
    assert len(matches) == 1


def test_invite_with_chat_local_recipient_invite_required_message_has_token_accept_invite_link(
        app, db_session, monkeypatch):
    """`:144`'s TRUE arm again, but `:145`'s FALSE arm this time:
    `community.invitations` is raised above `INVITE_APPLY`, so `:148` mints a
    real `CommunityInvitation` token via `create_invite_token` and `:149`
    embeds it in an `accept_invite` link instead of `:146`'s plain
    `subscribe` link.

    Sites reached: `:144`=T, `:145`=F; `src`=API; `reply`=T. `src` flips to
    API relative to the sibling test above while `:144`=T stays the same,
    decoupling `:144` from `src`; a mutant conjoining `:144`'s check with
    `src == SRC_WEB` would make THIS test take the `:150` remote branch
    instead (the local recipient's own instance's `software` is
    `'mastodon'` by factory default, landing on `:167`'s template-
    replacement arm) and fail every assertion below -- confirmed by hand:
    this is the test that catches that exact mutant, via
    `sqlalchemy.exc.NoResultFound` on the `.one()` lookup below (a CRASH
    kill; see the section comment for the non-crashing sibling mutant that
    corroborates it).

    Asserting `'subscribe' not in message` is what catches a mutant that
    left `:146`'s literal fallback in place instead of taking this arm.
    """
    s = _seed()
    s.community.invitations = INVITE_APPLY + 1
    db.session.commit()
    recipient = make_user(s.instance, 'gabe', local=True)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    result = invite_with_chat(s.community.id, 'gabe', SRC_API, bearer(s.user))

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    invite_row = db.session.query(CommunityInvitation).filter_by(
        community_id=s.community.id, user_id=recipient.id).one()
    assert (f"{app.config['SERVER_URL']}/community/{s.community.link()}/accept_invite/"
           f"{invite_row.token}.") in message
    assert 'subscribe' not in message


def test_invite_with_chat_remote_piefed_apply_open_message_has_remote_subscribe_link(
        app, db_session, monkeypatch):
    """`:144`'s FALSE arm (remote recipient) and `:151`'s TRUE arm: the
    recipient's instance software is `'PieFed'`, MIXED CASE, so this test
    also proves `:151`'s `.lower()` is load-bearing -- a mutant deleting it
    makes `'PieFed' == 'piefed'` false, falls through to `:160`'s lemmy/mbin
    check (also false), and lands on `:166`'s `else`, replacing `message`
    with the rendered template instead of appending this arm's text.

    `:152`'s TRUE arm (`invitations` at the factory default `0`) appends
    `:153`'s remote-subscribe sentence, which embeds both a direct subscribe
    link AND the `add_remote` fallback hint -- asserted together since a
    mutant could drop either half independently. `'accept_invite'` and `'You
    need to be logged in'` (the two `:152`-FALSE siblings' distinguishing
    text) are asserted absent.

    Sites reached: `:144`=F, `:151`=T, `:152`=T; `src`=WEB; `reply`=T. This
    is also the non-crashing sibling of the `:144` x `src` mutant discussed
    in the section comment: under `if recipient.is_local() or src ==
    SRC_WEB:`, this test's `src=WEB` makes the mutant wrongly true despite a
    remote recipient, routing it into `:145`'s local-arm text instead of
    this arm's remote-subscribe text -- confirmed to fail cleanly on the
    plain-string assertion below, not a crash.
    """
    s = _seed()
    remote_instance = make_instance('remote-piefed-open.example', software='PieFed')
    recipient = make_user(remote_instance, 'hana', local=False)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    with web_ctx(app, s.user):
        result = invite_with_chat(s.community.id, recipient.ap_id, SRC_WEB)

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    assert (f"https://{remote_instance.domain}/c/{s.community.link()}@{s.community.ap_domain}"
           f"/subscribe") in message
    assert f"https://{remote_instance.domain}/community/add_remote." in message
    assert 'accept_invite' not in message
    assert 'You need to be logged in' not in message


def test_invite_with_chat_remote_piefed_local_only_invite_required_message_has_local_accept_invite_link(
        app, db_session, monkeypatch):
    """`:151`'s TRUE arm again with the OTHER alternative, `'pylova'`
    (lower-case; the sibling test above already pins `:151`'s `.lower()`
    with `'PieFed'`), and `:152`'s FALSE arm: `invitations` is raised above
    `INVITE_APPLY`, so `:155` mints a token and `:156`'s local_only check
    runs. `community.local_only = True` here takes `:157`, embedding the
    token in a LOCAL `accept_invite` link (this instance's own `SERVER_URL`,
    not the recipient's remote domain).

    Sites reached: `:144`=F, `:151`=T, `:152`=F, `:156`=T; `src`=WEB;
    `reply`=T.

    `src=WEB` HERE, NOT `API` -- CHANGED AFTER REVIEW. This test originally
    used `SRC_API`, pairing `:156`=T with `src=API` while the sibling test
    below paired `:156`=F with `src=WEB` -- a perfect diagonal that let a
    reviewer-applied mutant, `if community.local_only and src == SRC_API:`
    at `:156`, pass the whole file (38 passed): whenever `:156` was really
    true, `src` was always `API` too, so the conjunction agreed with the
    real condition on both tests that ever reached it. Flipping THIS test
    to `src=WEB` (real `:156`=T, mutant now `T and (WEB==API is False)` =
    False) breaks that: the mutant takes `:159`'s remote-link text instead
    of this arm's local-link text, and the assertions below -- built for
    the local-link shape -- fail. The sibling test below keeps `src=API`,
    so `:156` now has one sample at each `src` value instead of one
    correlated pair; a mutant using the mirror literal (`src == SRC_WEB`)
    would still slip through with only two tests total reaching `:156` --
    recorded as a known residual limitation in the task report, not silently
    left unstated.

    Asserting `'add_remote' not in message` is what catches a mutant that
    took `:159`'s remote-accept-invite sibling instead -- both arms mint a
    token and mention `accept_invite`, so the `add_remote` hint (present
    only on `:159`) is the discriminator, and `remote_instance.domain not in
    message` gives a second, independent check that no remote-domain URL
    leaked into what should be an entirely local link. Both are exactly the
    assertions the `:156` x `src` mutant above now fails.
    """
    s = _seed()
    s.community.invitations = INVITE_APPLY + 1
    s.community.local_only = True
    db.session.commit()
    remote_instance = make_instance('remote-piefed-local-only.example', software='pylova')
    recipient = make_user(remote_instance, 'ivan', local=False)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    with web_ctx(app, s.user):
        result = invite_with_chat(s.community.id, recipient.ap_id, SRC_WEB)

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    invite_row = db.session.query(CommunityInvitation).filter_by(
        community_id=s.community.id, user_id=recipient.id).one()
    assert (f"{app.config['SERVER_URL']}/community/{s.community.link()}/accept_invite/"
           f"{invite_row.token}.") in message
    assert 'You need to be logged in to a' in message
    assert 'add_remote' not in message
    assert remote_instance.domain not in message


def test_invite_with_chat_remote_piefed_not_local_only_invite_required_message_has_remote_accept_invite_link(
        app, db_session, monkeypatch):
    """`:156`'s FALSE arm, the mirror of the sibling test above:
    `community.local_only` stays at the factory default `False`, so `:159`
    embeds the token in a REMOTE `accept_invite` link (the recipient's own
    instance domain) plus the `add_remote` fallback hint, instead of `:157`'s
    local link.

    Software is `'piefed'` (lower-case) here -- the mixed-case pin for
    `:151` already lives on the apply-open test above, so every arm within
    this sub-branch does not need its own mixed-case value, only the site
    does, once.

    Sites reached: `:144`=F, `:151`=T, `:152`=F, `:156`=F; `src`=API;
    `reply`=T. `src=API` HERE, NOT `WEB` -- also changed after review, as
    the other half of the `:156` x `src` rebalance the sibling test above's
    docstring explains: that test now supplies `:156`=T at `src=WEB`, this
    one supplies `:156`=F at `src=API`, so `:156`'s two reaching tests no
    longer share a single `src` value between them.

    Asserting `'You need to be logged in' not in message` catches a mutant
    that took `:157`'s sibling instead; `add_remote` and the remote domain's
    presence catch the reverse swap.
    """
    s = _seed()
    s.community.invitations = INVITE_APPLY + 1
    assert s.community.local_only is False
    db.session.commit()
    remote_instance = make_instance('remote-piefed-remote.example', software='piefed')
    recipient = make_user(remote_instance, 'jill', local=False)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    result = invite_with_chat(s.community.id, recipient.ap_id, SRC_API, bearer(s.user))

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    invite_row = db.session.query(CommunityInvitation).filter_by(
        community_id=s.community.id, user_id=recipient.id).one()
    assert (f"https://{remote_instance.domain}/community/{s.community.link()}@"
           f"{s.community.ap_domain}/accept_invite/{invite_row.token}") in message
    assert f"https://{remote_instance.domain}/community/add_remote." in message
    assert 'You need to be logged in' not in message


def test_invite_with_chat_remote_lemmy_apply_open_message_has_join_link(
        app, db_session, monkeypatch):
    """`:160`'s TRUE arm (the `:151` check having already failed since
    software is a lemmy-family value): `'LEMMY'`, MIXED CASE, proves `:160`'s
    own `.lower()` is load-bearing the same way the piefed apply-open test
    proves `:151`'s -- a mutant deleting `:160`'s `.lower()` makes
    `'LEMMY' == 'lemmy'` false, falls to `:166`'s `else`, and replaces
    `message` with the template instead.

    `:161`'s TRUE arm (`invitations` at the factory default) appends `:162`'s
    join-link sentence. `'accept_invite'` (the `:161`-FALSE sibling's
    distinguishing text, shared with the piefed-family arms above) is
    asserted absent.

    Sites reached: `:144`=F, `:151`=F, `:160`=T, `:161`=T; `src`=WEB;
    `reply`=T.

    `src=WEB` HERE, NOT `API` -- CHANGED DURING THIS FIX ROUND, by the same
    method review used on `:156` and `:145`, applied here proactively
    rather than in response to a reported survivor. This test originally
    used `SRC_API` and the sibling test below used `SRC_WEB`, pairing
    `:161`=T with `src=API` and `:161`=F with `src=WEB` -- the identical
    diagonal shape that let `:156`'s and `:145`'s mutants slip through.
    Hand-verified: `if community.invitations <= INVITE_APPLY and src ==
    SRC_API:` at `:161` passed the whole file (38 passed) under the
    original pairing. Flipping THIS test to `src=WEB` (real `:161`=T,
    mutant now `T and (WEB==API is False)` = False) makes the mutant take
    `:164-165`'s token-and-accept_invite text instead of this arm's
    join-link text, failing the assertions below. This test's `:160`=T
    pairs with `src=WEB` and the sibling test below's `:160`=T pairs with
    `src=API`, so `:160` itself stays decoupled from `src` in both
    directions even after this swap (both `src` values occur at `:160`=T
    across the two tests) -- `:161`, with only two tests reaching it total,
    keeps the same single-direction residual the `:156` fix has, recorded
    in the task report.

    This also serves as the non-crashing sibling for the `:151` x `apply`
    mutant in the section comment: under `if (...piefed check...) or
    community.invitations <= INVITE_APPLY:`, this test's real software is a
    lemmy value (not piefed) but `:161`'s `apply`=T here makes the OR
    mutant true anyway, routing it into `:151`'s piefed-family text instead
    of this arm's lemmy join-link text -- confirmed to fail cleanly on the
    plain-string assertions below, not a crash.
    """
    s = _seed()
    remote_instance = make_instance('remote-lemmy-open.example', software='LEMMY')
    recipient = make_user(remote_instance, 'kara', local=False)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    with web_ctx(app, s.user):
        result = invite_with_chat(s.community.id, recipient.ap_id, SRC_WEB)

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    assert (f"clicking 'Join' at https://{remote_instance.domain}/c/{s.community.link()}@"
           f"{s.community.ap_domain}") in message
    assert 'into your search function.' in message
    assert 'accept_invite' not in message
    assert 'You need to be logged in' not in message


def test_invite_with_chat_remote_mbin_invite_required_message_has_token_accept_invite_link(
        app, db_session, monkeypatch):
    """`:160`'s TRUE arm again with the OTHER alternative, `'mbin'`
    (lower-case; `:160`'s `.lower()` is already pinned by the sibling test
    above's `'LEMMY'`), and `:161`'s FALSE arm: `invitations` is raised
    above `INVITE_APPLY`, so `:164` mints a token and `:165` embeds it in the
    same LOCAL `accept_invite` link shape `:157` builds (a genuinely
    identical f-string reached via an unrelated branch, per the section
    comment above -- not a value-swap gap).

    Sites reached: `:144`=F, `:151`=F, `:160`=T, `:161`=F; `src`=API;
    `reply`=T. `src=API` HERE, NOT `WEB` -- the other half of the `:161` x
    `src` rebalance the sibling test above's docstring explains: that test
    now supplies `:161`=T at `src=WEB`, this one supplies `:161`=F at
    `src=API`, and `:160`=T now has one sample at each `src` value across
    the two tests (WEB above, API here), keeping `:160` itself decoupled
    from `src` in both directions.

    Asserting `"clicking 'Join'" not in message` catches a mutant that took
    `:162`'s sibling instead.
    """
    s = _seed()
    s.community.invitations = INVITE_APPLY + 1
    db.session.commit()
    remote_instance = make_instance('remote-mbin-token.example', software='mbin')
    recipient = make_user(remote_instance, 'liam', local=False)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    result = invite_with_chat(s.community.id, recipient.ap_id, SRC_API, bearer(s.user))

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    invite_row = db.session.query(CommunityInvitation).filter_by(
        community_id=s.community.id, user_id=recipient.id).one()
    assert (f"{app.config['SERVER_URL']}/community/{s.community.link()}/accept_invite/"
           f"{invite_row.token}.") in message
    assert 'You need to be logged in to a' in message
    assert "clicking 'Join'" not in message


def test_invite_with_chat_remote_other_software_message_replaces_greeting_with_rendered_template(
        app, db_session, monkeypatch):
    """`:151` and `:160` BOTH false: the recipient's instance software is
    left at `make_instance`'s own default, `'mastodon'`, which matches
    neither the piefed/pylova nor the lemmy/mbin families, so `:166`'s
    `else` runs `:167`.

    `:167` is different IN KIND from every other arm in this fork: `message
    = render_template(...)` REPLACES the greeting `:141` already built,
    rather than appending to it (every other arm uses `+=`). The expected
    string is built the same way invite_with_email's own `:167`-sibling
    tests build theirs (same template, same `render_template` call, same
    keyword arguments this call site actually passes -- `user`, `community`,
    `host=SERVER_NAME`, no `subscribe` -- so this test does not echo
    production's string-building back at itself for anything this call
    varies). Asserting `text == expected_message` on its own would still
    pass under a mutant that changed `=` to `+=` at `:167`, SINCE THE
    TEMPLATE ITSELF ALSO OPENS WITH "Hi there," -- the mutant's message
    would contain BOTH the old greeting and the template text, and a bare
    "template text present" check cannot see the extra leftover text. The
    assertion that actually catches that mutant is `'check it out' not in
    message`: `:141`'s public-community greeting phrase (asserted present in
    this file's own message-fork tests above) would still be sitting at the
    front of the message under a `+=` mutant, and this test's community is
    left non-private (the factory default) specifically so `:141`, not
    `:143`, is the phrase that would leak.

    Sites reached: `:144`=F, `:151`=F, `:160`=F (falls to the `:166` else);
    `src`=API; `reply`=T. `:145`/`:152`/`:156`/`:161` are never reached by
    this test.
    """
    s = _seed()
    assert s.community.private is False
    remote_instance = make_instance('remote-other-software.example')
    assert remote_instance.software == 'mastodon'
    recipient = make_user(remote_instance, 'maya', local=False)
    calls = []
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: calls.append(message) or object())

    result = invite_with_chat(s.community.id, recipient.ap_id, SRC_API, bearer(s.user))

    assert result == 1
    assert len(calls) == 1
    message = calls[0]
    expected_message = render_template('email/invite_to_community.txt', user=s.user,
                                       community=s.community, host=app.config['SERVER_NAME'])
    assert message == expected_message
    assert 'check it out' not in message
    assert 'Create an account' in message


def test_invite_with_chat_failed_delivery_returns_0_and_still_creates_conversation(
        app, db_session, monkeypatch):
    """`:170-172`'s return fork, FALSE arm: `send_message` is patched to
    return a falsy sentinel (`None`), so `reply` is falsy and `:172` returns
    `0` -- the FOURTH path sharing that return value, per the module
    docstring's FOUR PATHS note.

    Unlike the other three `return 0` paths (`:129`'s guard failing,
    `:131`'s banned-community check, both covered in the first-half tests
    above with an ASSERTED-ABSENT `Conversation`), THIS path reaches
    `:134-138` and commits a real `Conversation` BEFORE `send_message` is
    even called -- so asserting the row WAS created (the mirror image of the
    other three tests' assertions) is what keeps all four `0`-returning
    paths distinguishable from each other despite the identical return
    value. The arm exercised to reach this point (local recipient,
    apply-open) is arbitrary -- the return fork at `:170-172` runs
    identically after every message-building arm -- so no new arm-specific
    assertion is needed on `message` itself here.

    Sites reached: `:144`=T, `:145`=T; `src`=WEB; `reply`=F. This test's own
    `:145`=T sample stays at `src=WEB`, same as it always was -- this test
    carries no assertion on `message` content (only on the return value and
    the `Conversation` row, neither of which differs between `:146`'s and
    `:148-149`'s arms), so it cannot distinguish a wrong-arm mutant
    regardless of which `src` it uses, and an earlier attempt to close the
    `:145` x `src` gap by flipping THIS test's `src` instead of the
    apply-open test above's was verified NOT to work for exactly that
    reason (documented in that test's own docstring, where the working fix
    lives).
    """
    s = _seed()
    recipient = make_user(s.instance, 'noor', local=True)
    monkeypatch.setattr('app.shared.community.send_message',
                        lambda message, conversation_id: None)

    with web_ctx(app, s.user):
        result = invite_with_chat(s.community.id, 'noor', SRC_WEB)

    assert result == 0

    all_conversations = db.session.query(Conversation).all()
    assert len(all_conversations) == 1
    assert {m.id for m in all_conversations[0].members} == {s.user.id, recipient.id}
