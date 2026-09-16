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

PIN, `:130-131`, NAMED DISTINCTLY FROM TASK 3'S: `community: Community =
db.session.query(Community).get(community_id)` immediately followed by
`if community.banned:` shares the exact `.get()`-then-dereference shape
Task 3 pinned at `invite_with_email:196-197`
(`test_invite_with_email_missing_community_raises_AttributeError`) -- a
missing id makes `.get()` return `None` quietly, and the very next line
dereferences it, raising `AttributeError: 'NoneType' object has no
attribute 'banned'`. Task 3's test and this task's test assert the
identical EXCEPTION TYPE at two DIFFERENT call sites, so this one is named
`test_invite_with_chat_missing_community_raises_AttributeError` --
distinct from Task 3's `test_invite_with_email_missing_community_raises_
AttributeError` by function name -- so Task 6's planned inversion of both
(once each site is rewritten to `.filter_by(id=...).one()`, raising
`NoResultFound` instead) can target each by name without a collision.

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


def test_invite_with_email_missing_community_raises_AttributeError(app, db_session):
    """PIN, NOT A FIX. `:196` fetches the community with `db.session.query
    (Community).get(community_id)` -- SQLAlchemy's legacy `Query.get` --
    which returns `None` quietly for an id matching no row, with nothing
    guarding `:197`'s immediate `community.banned` dereference. Python
    therefore raises `AttributeError: 'NoneType' object has no attribute
    'banned'`.

    This module's OTHER `Community`-by-id lookup, `restore_community:522`,
    uses `.filter_by(id=community_id).one()` for the identical "does this id
    exist" question, and `.one()` raises `sqlalchemy.exc.NoResultFound`
    instead -- a different exception for the same kind of missing input.
    `invite_with_chat:130` shares today's `.get()`-then-dereference shape
    and is pinned by a SEPARATELY NAMED test in this round's Task 4, so that
    Task 6's fix of both call sites cannot collide on a shared test name.

    THIS TEST ASSERTS TODAY'S ACTUAL, BROKEN BEHAVIOUR -- NOT THE INTENDED
    ONE -- AND PASSES AGAINST CURRENT CODE. A later task (Task 6) rewrites
    `:196` to use `.filter_by(id=...).one()` (matching `restore_community`'s
    own pattern) and must INVERT this test to expect `NoResultFound` in
    place of `AttributeError` once that fix lands.
    """
    s = _seed()

    with web_ctx(app, s.user):
        with pytest.raises(AttributeError):
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


def test_invite_with_chat_missing_community_raises_AttributeError(app, db_session):
    """PIN, NOT A FIX -- see the module docstring's PIN section above for
    the full rationale and why this is named distinctly from Task 3's
    `test_invite_with_email_missing_community_raises_AttributeError`.

    `:129`'s guard is genuinely TRUE here (a real, unbanned local recipient
    on a real, unbanned instance), so control reaches `:130`'s
    `db.session.query(Community).get(community_id)` for a `community_id`
    that matches no row. `.get()` returns `None` quietly, and `:131`'s
    `if community.banned:` dereferences it immediately, raising
    `AttributeError: 'NoneType' object has no attribute 'banned'`.

    THIS TEST ASSERTS TODAY'S ACTUAL, BROKEN BEHAVIOUR -- NOT THE INTENDED
    ONE -- AND PASSES AGAINST CURRENT CODE. Task 6 rewrites `:130` to use
    `.filter_by(id=...).one()` (matching `restore_community:522`'s own
    pattern) and must INVERT this test to expect `NoResultFound` in place
    of `AttributeError` once that fix lands.
    """
    s = _seed()
    recipient = make_user(s.instance, 'bob', local=True)

    with web_ctx(app, s.user):
        with pytest.raises(AttributeError):
            invite_with_chat(999999, 'bob', SRC_WEB)


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
    """
    s = _seed()
    recipient = make_user(s.instance, 'carol', local=True)
    s.community.banned = True
    db.session.commit()

    result = invite_with_chat(s.community.id, 'carol', SRC_API, bearer(s.user))

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

    result = invite_with_chat(s.community.id, 'erin', SRC_API, bearer(s.user))

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
