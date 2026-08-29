"""create_resolved_object (app/activitypub/util.py) is the half of the resolver
pair that turns an already-fetched remote document into a row. Its callers --
resolve_remote_post, process_microblog_announce and the alpha API's
get_resolve_object -- each supply `uri_domain` from a different place, which is
why this file calls it DIRECTLY rather than through any of them: the parameter
is the thing under test, and a test that reached it through one caller would
pin that caller's derivation instead.

Derived from this function's own source, not copied from the near-duplicate
resolve_remote_post_from_search (that one is Task 5's, and deriving it again
there is the point):

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast
    src = open('app/activitypub/util.py').read()
    n = next(x for x in ast.walk(ast.parse(src))
             if isinstance(x, ast.FunctionDef) and x.name == 'create_resolved_object')
    print('span', n.lineno, n.end_lineno)
    print('If:', len([x for x in ast.walk(n) if isinstance(x, ast.If)]))
    print('For:', len([x for x in ast.walk(n) if isinstance(x, ast.For)]))
    print('Try:', len([x for x in ast.walk(n) if isinstance(x, ast.Try)]))
    "

    span 4171 4236
    If: 21
    For: 1
    Try: 0

This file covers the first three of those regions -- the `attributedTo` walk,
the domain gate, and the three-part `if user and community and post_data`.
The dispatch below them (create/update, the inReplyTo split, the `posted_at`
enrichment) is Task 3's, in this same file.

THE `attributedTo` WALK has six outcomes, and the loop's control flow is not
symmetric between them:

- the key is absent -- `actor` and `actor_domain` both stay None;
- a bare string -- parsed directly;
- a list whose first usable element is a `Person` dict with a string `id`;
- a list whose first usable element is a bare string;
- a list element that is a `Person` dict whose `id` is NOT a string -- the
  `isinstance(actor, str)` guard leaves `actor_domain` None while `actor` is
  bound to whatever the peer sent, and the `break` still fires;
- a list containing neither shape -- the loop runs off the end.

**A `Person` dict BREAKS, a non-`Person` dict CONTINUES.** That asymmetry is
the walk's only real subtlety and it is pinned in both directions below: a
useless `Person` dict ends the search, while a useless non-`Person` dict does
not. A reader who assumes "first element wins" is right in one case and wrong
in the other.

THE DOMAIN GATE is `uri_domain != actor_domain`, and `actor_domain` starts as
None, so a document with no usable author reaches the comparison with None on
one side. That refusal is a different path from a domain MISMATCH even though
both return None, and both are covered.

This is **D22's surface**. Both sides are raw `urlparse(...).netloc`, so the
comparison is case-, port- and userinfo-sensitive, and the register rates the
alpha-API call path worse than the inbox ones because that path lowercases one
side and not the other. The tests below pin what it does TODAY. They are
characterisation, not endorsement: a fix to D22 flips the case and port cases
from refusal to a created post, and it should, which is exactly what makes
them useful to whoever lands it.

THE THREE-PART GUARD `if user and community and post_data` has one operand
that cannot be tested the way the other two can. See TestPostDataOperandIsDead
at the bottom -- the finding is that it is unreachable as the deciding
operand, and that is reported rather than papered over with a test that
pretends otherwise.

Assertions here are on the row's CONTENTS or on the returned object's
identity, never on a row's mere existence: create_post commits from inside
this function, so "a Post exists" would be true on paths this file means to
distinguish.

D23 IS A TRIPLE, NOT A PAIR -- found by the mutation harness, not by reading.
The first mutation run could not apply a single one of its nine mutants: every
target string occurs more than once in the file, because the walk and the gate
are copied. Scoped to each function by AST:

    the walk `actor_domain = None ... attributedTo`   2 copies
    the gate `if uri_domain != actor_domain:`         3 copies
    `if user and community and post_data:`            2 copies

The third copy of the gate is in **verify_object_from_source**, which
sub-project 2b already fixed: it reads `host_of(...)` where both copies here
read raw `urlparse(...).netloc` (0 netloc / 6 host_of there, against 3 netloc
in this function and 5 in resolve_remote_post_from_search). So D23's "fixing
one leaves the other" is not a forecast -- it has already happened once, and
the register describes the surviving duplication as a pair when the family is
three functions wide and one member is already correct. **The fixed copy is
also the fix template**, which is worth more to whoever takes D22 than the
register's prose is. Task 5 owns the drift report and Task 7 the register;
this is recorded here because it was found here.

MUTATION. Nine mutants over this file's three regions, each reverted before
the next, all scoped to this function's line span:

| mutant | failed |
|---|---|
| the `break` moved inside the `isinstance(actor, str)` guard | 1 |
| the bare-string list arm deleted | 2 |
| the string `attributedTo` arm deleted | 1 |
| `actor_domain` initialised to `uri_domain` instead of None | 4 |
| the gate never fires | 9 |
| the gate always fires | 5 |
| the `user` operand dropped | 1 -- see below |
| the `community` operand dropped | 1 |
| the `post_data` operand dropped | **0 -- SURVIVES** |

Two of these are worth more than their counts.

**The `user` mutant survived the first run and the test was rewritten, not the
count.** All sixteen tests passed with the operand deleted, because create_post
swallows the resulting exception and returns the same None. TestTheUserOperand
now asserts the operand is never REACHED rather than that the answer is None,
and the mutant dies. The original test asserted a true thing that no production
change could falsify.

**The `post_data` mutant survives by design and is left surviving.** It is
reported, not fixed, because the operand is unreachable as a decision -- the
argument is in TestPostDataOperandIsDead below. A mutant that cannot be killed
without changing production code is a finding about the code, and silently
dropping it from the table would hide exactly the thing worth knowing.

COVERAGE, from this file alone over span 4171-4236: 37 of 60 statements and
23 of 44 branch arcs. That is deliberately a SLICE, not the function -- the
dispatch below the guard is Task 3's, and Task 3 owns the whole-function
confirmation that the two slices together leave no gap between them. The
ledger's starting figure of 25/59 came from a full-suite run and counts the
body without the `def`; 60 is that body plus the def, the same reconciliation
Task 1 recorded.
"""

import pytest

from app.activitypub.util import create_resolved_object
from app.models import ActivityPubLog, Post
from tests.factories import (AS_PUBLIC_URI, PEER_OBJECT_HOST, PEER_OBJECT_URI, make_community,
                             make_site, note_document, resolvable_remote_author,
                             seed_community_owner)

URI = PEER_OBJECT_URI
AUTHOR_URI = f'https://{PEER_OBJECT_HOST}/users/alice'
OTHER_HOST_AUTHOR = 'https://elsewhere.example/users/mallory'


@pytest.fixture
def peer_author(db_session):
    """The Instance and remote User every accepted document below is attributed
    to, seeded so find_actor_or_create takes its found-existing path.

    Same shape as the fixture in test_ap_resolve_remote_post.py and for the
    same reason: creating the actor instead would fetch the actor document,
    and no test in this file registers any HTTP route at all. The session-wide
    block_outbound_http router raises on unmatched requests, so a change that
    made this function fetch would fail these tests rather than reach the
    network.
    """
    make_site()
    instance = seed_community_owner(PEER_OBJECT_HOST)
    return resolvable_remote_author(instance, 'alice')


def public_note(attributed_to=AUTHOR_URI):
    """The document handed to the function, addressed to the public collection.

    Without 'to', activitypub_visibility classifies the object as 'direct' and
    create_post refuses it -- so an accepted case would return None for a
    reason that has nothing to do with the walk or the gate under test.
    """
    return note_document(attributed_to=attributed_to, uri=URI,
                         fields={'to': [AS_PUBLIC_URI]})


def resolved(post_data, community, uri_domain=PEER_OBJECT_HOST):
    """create_resolved_object called directly, with announce_id None and
    store_ap_json False -- neither is read on any path this file exercises."""
    return create_resolved_object(URI, post_data, uri_domain, community, None, False)


class TestAttributedToIsAString:
    """The simplest usable shape: `attributedTo` is the author's URI. Its
    netloc becomes actor_domain, matches uri_domain, and the document is
    written.

    Production change that fails this: deleting the `isinstance(attributed_to,
    str)` branch, or making it parse anything other than the string itself.
    """

    def test_a_string_author_on_the_uris_host_is_written(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note(), community)

        assert result.id == Post.query.filter_by(ap_id=URI).one().id
        assert result.user_id == peer_author.id


class TestAttributedToIsAListOfOneUsableElement:
    """Both list shapes the walk accepts, each on its own.

    Production change that fails these: deleting either arm of the loop's
    if/elif, or removing the `isinstance(attributed_to, list)` branch.
    """

    def test_a_person_dict_with_a_string_id_is_used(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note([{'type': 'Person', 'id': AUTHOR_URI}]), community)

        assert result.user_id == peer_author.id

    def test_a_bare_string_element_is_used(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note([AUTHOR_URI]), community)

        assert result.user_id == peer_author.id


class TestTheBreakIsAsymmetric:
    """The walk's one genuine subtlety, pinned in both directions.

    A `Person` dict breaks the loop whether or not it yielded a usable author,
    so a later element that WOULD have matched is never read. A non-`Person`
    dict does not break, so a later element still gets its turn. Same list
    position, same uselessness, opposite consequence.

    Production change that fails the first test: moving the `break` inside the
    `isinstance(actor, str)` guard, which is the obvious "fix" and would
    change behaviour. Production change that fails the second: adding a
    `break` to the dict arm's non-Person path, or turning the `elif` chain
    into a sequence of `if`s.
    """

    def test_a_person_dict_ends_the_search_even_when_it_yields_nothing(self, app, peer_author):
        """The Person dict's `id` is not a string, so actor_domain stays None
        and the gate refuses -- despite a perfectly good author URI sitting in
        the very next element."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        attributed_to = [{'type': 'Person', 'id': {'nested': 'not a string'}}, AUTHOR_URI]

        assert resolved(public_note(attributed_to), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_person_dict_on_another_host_ends_the_search_too(self, app, peer_author):
        """The same break, reached by the ordinary route: the first Person
        wins outright, so a second author on the URI's own host cannot rescue
        a document the first one condemns."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        attributed_to = [{'type': 'Person', 'id': OTHER_HOST_AUTHOR}, AUTHOR_URI]

        assert resolved(public_note(attributed_to), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_non_person_dict_does_not_end_the_search(self, app, peer_author):
        """The mirror image: a Service dict is neither arm of the if/elif, so
        the loop moves on and the string behind it is used."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        attributed_to = [{'type': 'Service', 'id': OTHER_HOST_AUTHOR}, AUTHOR_URI]

        result = resolved(public_note(attributed_to), community)

        assert result.user_id == peer_author.id


class TestTheWalkYieldsNoAuthor:
    """Three ways to reach the gate with actor_domain still None: the key
    absent, a list of no usable elements, and a list that is empty. All three
    refuse, and they refuse by a different route than a domain mismatch does.

    Production change that fails these: initialising actor_domain to anything
    but None, or defaulting it to uri_domain -- which would turn every
    authorless document into an accepted one.
    """

    def test_no_attributed_to_key_at_all_refuses(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        document = note_document(uri=URI, fields={'to': [AS_PUBLIC_URI]})
        del document['attributedTo']

        assert resolved(document, community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_list_of_neither_shape_runs_off_the_end_and_refuses(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        attributed_to = [{'type': 'Service', 'id': OTHER_HOST_AUTHOR}, 42, None]

        assert resolved(public_note(attributed_to), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_an_empty_list_refuses(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        assert resolved(public_note([]), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestTheDomainGate:
    """`uri_domain != actor_domain`, the impersonation check the function's own
    comment advertises. A document whose stated author lives on a different
    host from the URI it was served at is refused before any actor lookup.

    Production change that fails this: deleting the gate, or inverting it.
    """

    def test_an_author_on_another_host_is_refused(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        assert resolved(public_note(OTHER_HOST_AUTHOR), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestRawNetlocComparison:
    """D22, pinned as it behaves today. Both operands are raw
    `urlparse(...).netloc`: no case fold, no default-port handling, no
    userinfo stripping. Each case below is the SAME host written two ways, and
    every one is refused.

    The register rates the alpha-API call path worse than the inbox paths
    precisely because get_resolve_object lowercases `uri_domain` and nothing
    lowercases `actor_domain`; the first test is that situation reproduced at
    the parameter, which is where it actually bites.

    Characterisation only. A fix flips all three to a created post -- these
    tests are how whoever lands it can tell it worked, and today nothing else
    would notice.
    """

    def test_a_lowercased_uri_domain_against_a_mixed_case_author_refuses(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        mixed_case_author = f'https://{PEER_OBJECT_HOST.capitalize()}/users/alice'

        assert resolved(public_note(mixed_case_author), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_an_explicit_default_port_on_the_author_refuses(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        ported_author = f'https://{PEER_OBJECT_HOST}:443/users/alice'

        assert resolved(public_note(ported_author), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_userinfo_in_the_author_uri_refuses(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        userinfo_author = f'https://alice@{PEER_OBJECT_HOST}/users/alice'

        assert resolved(public_note(userinfo_author), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestTheUserOperand:
    """`user` is the first operand of `if user and community and post_data`,
    and find_actor_or_create returns None for an actor validate_remote_actor
    rejects.

    The rejected actor here is the public collection URI itself, which
    validate_remote_actor (app/activitypub/actor.py) refuses by name in its
    first statement. It needs no banned-instance row or blocked-word setting,
    so the test states one fact rather than depending on three -- and a peer
    really can send `attributedTo: Public`, which is why the refusal exists.

    Note the gate PASSES here: the public URI's netloc is www.w3.org and
    uri_domain is set to match, so the operand under test is the only thing
    refusing.

    **THE RETURN VALUE ALONE CANNOT TEST THIS OPERAND, and the first version of
    this test did not.** Dropping `user` from the conjunction left all sixteen
    tests passing. The reason is downstream: create_post wraps `Post.new` in
    `except Exception` and returns None, so a None user produces the same None
    this function returns when the operand refuses. Two different events, one
    indistinguishable answer -- which is this campaign's dominant failure mode
    seen once more, and it was the mutation run that exposed it rather than any
    amount of reading.

    What separates them is whether create_post is REACHED. It is reached only
    in the mutant, and reaching it logs an APLOG_FAILURE for the swallowed
    exception. That log is a no-op under the suite's default config
    (LOG_ACTIVITYPUB_TO_DB is False), so the test turns it on for its own
    duration and asserts on the row. The assertion is on a real side effect of
    real production code, not on a mock: with the operand intact, nothing
    downstream runs and there is nothing to log.

    Production change that fails this: dropping the `user` operand, which
    reaches create_post with a None user and logs the failure it swallows.
    """

    def test_an_unresolvable_author_returns_none_without_reaching_create_post(
            self, app, peer_author, monkeypatch):
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note(AS_PUBLIC_URI), community, uri_domain='www.w3.org')

        assert result is None
        assert Post.query.filter_by(ap_id=URI).count() == 0
        assert ActivityPubLog.query.count() == 0


class TestTheCommunityOperand:
    """`community` is the second operand, and its falsy case is simply None --
    the alpha API reaches this function with whatever find_community returned,
    which is None for a URI naming no community this instance knows.

    Everything else about this call is the accepted shape, so `community` is
    the only thing refusing.

    Production change that fails this: dropping the `community` operand, which
    would pass None to create_post.
    """

    def test_no_community_returns_none(self, app, peer_author):
        assert resolved(public_note(), None) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestPostDataOperandIsDead:
    """**A finding, reported rather than covered.** The third operand of
    `if user and community and post_data` cannot be the one that decides.

    Every route to a falsy `post_data` leaves `actor` None:

    - the JSON falsy values a peer can serve are `{}`, `[]`, `''`, `0`, `false`
      and `null`;
    - `'attributedTo' in post_data` is False for the first three and raises
      TypeError for the rest, so `actor` and `actor_domain` stay None either
      way;
    - the gate `uri_domain != actor_domain` then refuses -- unless `uri_domain`
      is ALSO None, which is the only way through;
    - and on that one path, find_actor_or_create(None) raises AttributeError on
      `actor.strip()` before the conjunction is ever evaluated.

    So there is no input for which control reaches `and post_data` with the
    first two operands true and that operand false. It is dead as a decision.
    The test below pins the escape hatch -- the None/None path that gets past
    the gate -- and asserts the crash, because the crash is what proves the
    operand unreachable rather than merely untested.

    Task 7 owns the register; this belongs in it as a new entry, alongside the
    older observation that this function's callers differ in whether they can
    supply a None uri_domain at all.

    Production change that fails this: guarding find_actor_or_create against a
    None actor, which would make the third operand reachable -- and would be a
    fine fix. The test is then the record of what changed, not an objection.
    """

    def test_a_none_uri_domain_and_an_empty_document_crash_before_the_operand(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        with pytest.raises(AttributeError):
            resolved({}, community, uri_domain=None)

        assert Post.query.filter_by(ap_id=URI).count() == 0
