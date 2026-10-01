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

This file covers the whole function, in two passes. Task 2 took the
`attributedTo` walk, the domain gate and the three-part `if user and community
and post_data`; Task 3 took everything below them -- the create/update
dispatch, the inReplyTo split and the `posted_at` enrichment. The dividing
comment is still in the file, because the two passes' mutation runs and
findings are reported separately below and the boundary is what makes them
readable.

THE `attributedTo` WALK has seven outcomes, and the loop's control flow is not
symmetric between them:

- the key is absent -- `actor` and `actor_domain` both stay None;
- a bare embedded object, ordinary ActivityStreams -- walked as a
  one-element list (D38, fixed; both arms used to miss it);
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

This was **D22's surface**. Both sides were raw `urlparse(...).netloc`, so the
comparison was case-, port- and userinfo-sensitive; both now go through
host_of, and an empty `uri_domain` is refused rather than compared.
TestRawNetlocComparison covers the fix.

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

MUTATION, TASK 3's REGION -- the dispatch, the split and the enrichment. Nine
mutants, plus three re-run after a test was strengthened:

| mutant | failed |
|---|---|
| `activity` forced to 'create' | 2 -- after strengthening; 1 before |
| `activity` forced to 'update' | **0 -- SURVIVES** |
| the post branch's update fallback deleted | 1 |
| the reply branch's update fallback deleted | **0 -- SURVIVES** |
| the `inReplyTo` truthiness test made `is not None` | 1 |
| the post branch's `published` guard forced true | 9 |
| the post branch's `last_active` set to now | 1 |
| the post branch's `posted_at` set to now | 2 |
| the post branch's create-result guard removed | 1 |

**`activity` forced to 'update' survives, and it should.** The update path
falls back to create when the row is absent, so choosing 'update' for a
document that carries no 'updated' key costs one extra lookup and then does
exactly what 'create' would have done. The dispatch is observable in one
direction only. Left surviving, because killing it would mean asserting on the
lookup rather than on behaviour.

**The reply branch's fallback deletion survived for a worse reason:** the
create it fell back to was broken (D35), so removing the fallback removed a
path to a failure rather than a path to a row. The prediction recorded here was
that fixing the defect would make the mutant die; the fix landed 2026-08-29 and
`test_an_update_for_a_reply_that_does_not_exist_falls_back_to_create` now
asserts the created reply, which the mutant cannot satisfy.

**A second test that pinned nothing, found the same way as Task 2's.**
`test_an_update_for_a_post_that_exists_updates_it_in_place` asserted the
returned id and the row count, and passed with the dispatch forced to 'create'
-- because Post.new catches the duplicate ap_id's IntegrityError and returns
the existing row untouched (app/models.py). Same id, same count, document
discarded. It now asserts the body was rewritten, and the mutant dies. That is
twice in two tasks that identity-and-count assertions have proved hollow here;
in this function, assert on what the path WROTE.

COVERAGE. Task 2's slice alone was 37 of 60 statements and 23 of 44 arcs.
Tasks 2 and 3 together left it at 55 of 60 statements and 41 of 44 arcs; the
five missing statements and three missing arcs were one region -- the REPLY
branch's copy of the enrichment -- and were unreachable, because D35 meant no
reply was ever created and so nothing ever enriched one. The gap was explained
rather than merely reported, and the prediction was that fixing D35 would close
it with no new tests aimed at it. That held: with the one-line guard added to
`PostReply.new` on 2026-08-29 this file measures **60 of 60 statements and 44
of 44 branch arcs**, complete, and the two tests written against the newly
reachable region assert its behaviour rather than its existence.

The ledger's starting figure of 25/59 came from a full-suite run and counts the
body without the `def`; 60 is that body plus the def, the same reconciliation
Task 1 recorded.
"""

from datetime import datetime

import pytest

from app import db
from app.activitypub import util
from app.activitypub.util import create_resolved_object
from app.models import ActivityPubLog, BannedInstances, CommunityBan, Post, PostReply
from tests.factories import (AS_PUBLIC_URI, PEER_OBJECT_HOST, PEER_OBJECT_URI, make_community,
                             make_post, make_post_reply, make_site, note_document,
                             resolvable_remote_author, seed_community_owner)

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

    def test_a_bare_person_object_is_used(self, app, peer_author):
        """D38, fixed: a single embedded object -- `attributedTo: {'type':
        'Person', 'id': ...}`, ordinary ActivityStreams -- used to match
        neither the string nor the list arm and was refused for its container
        type alone. It is now walked as a one-element list, so it is accepted
        exactly when the same object inside a list is."""
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note({'type': 'Person', 'id': AUTHOR_URI}), community)

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


def author_lookup_spy(monkeypatch, user):
    """Stand in for find_actor_or_create, returning `user` and recording the
    URIs asked for -- being asked at all means the domain gate let the call
    through."""
    asked = []

    def lookup(actor, *args, **kwargs):
        asked.append(actor)
        return user

    monkeypatch.setattr(util, 'find_actor_or_create', lookup)
    return asked


class TestRawNetlocComparison:
    """D22, fixed. Both operands used to be raw `urlparse(...).netloc`, so the
    same host written with a capital, an explicit `:443` or a userinfo was
    refused -- systematically on the alpha-API path, where get_resolve_object
    lowercases `uri_domain` and nothing lowercased `actor_domain`. Both now go
    through host_of, so each case below is created.

    The port and userinfo cases stub the author lookup: find_actor_or_create
    keys on the exact URI and would fetch that spelling, which is a question
    about actor identity, not about this gate.

    `uri_domain` is an authority from the caller, so it is normalised too, and
    the normalised host is what goes into the synthesised activity id. An
    author URI urlparse rejects used to raise ValueError; it now degrades to ''
    and compares unequal.
    """

    def test_a_lowercased_uri_domain_against_a_mixed_case_author_is_created(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        mixed_case_author = f'https://{PEER_OBJECT_HOST.capitalize()}/users/alice'

        assert resolved(public_note(mixed_case_author), community).ap_id == URI

    def test_an_explicit_default_port_on_the_author_is_created(self, app, peer_author, monkeypatch):
        community = make_community('news', host=PEER_OBJECT_HOST)
        ported_author = f'https://{PEER_OBJECT_HOST}:443/users/alice'

        passed_the_gate = author_lookup_spy(monkeypatch, peer_author)

        assert resolved(public_note(ported_author), community).ap_id == URI
        assert passed_the_gate == [ported_author]

    def test_userinfo_in_the_author_uri_is_created(self, app, peer_author, monkeypatch):
        community = make_community('news', host=PEER_OBJECT_HOST)
        userinfo_author = f'https://alice@{PEER_OBJECT_HOST}/users/alice'

        passed_the_gate = author_lookup_spy(monkeypatch, peer_author)

        assert resolved(public_note(userinfo_author), community).ap_id == URI
        assert passed_the_gate == [userinfo_author]

    def test_a_ported_mixed_case_uri_domain_is_stripped_into_the_synthesised_id(
            self, app, peer_author, monkeypatch):
        community = make_community('news', host=PEER_OBJECT_HOST)
        seen = []
        real_create_post = util.create_post

        def spy(store_ap_json, community, request_json, user, announce_id=None):
            seen.append(request_json['id'])
            return real_create_post(store_ap_json, community, request_json, user, announce_id)

        monkeypatch.setattr(util, 'create_post', spy)

        assert resolved(public_note(), community, uri_domain=f'{PEER_OBJECT_HOST.capitalize()}:443').ap_id == URI
        assert seen[0].startswith(f'https://{PEER_OBJECT_HOST}/activities/create/')

    def test_an_author_uri_urlparse_rejects_is_refused_without_raising(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        assert resolved(public_note('https://[broken/users/alice'), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_an_unparseable_uri_domain_does_not_match_an_unparseable_author(
            self, app, peer_author, monkeypatch):
        """Both degrade to '', and '' == '' -- the empty-host refusal is what
        keeps two failed parses from agreeing. The lookup is stubbed to accept
        anything, so only the gate can refuse."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        passed_the_gate = author_lookup_spy(monkeypatch, peer_author)

        assert resolved(public_note('https://[broken/users/alice'), community, uri_domain='[broken') is None
        assert passed_the_gate == []


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
    - the gate then refuses. It used to let one path through, a None
      `uri_domain` comparing equal to the None `actor_domain`, which then
      crashed with AttributeError in find_actor_or_create(None). Since D22's
      fix an empty `uri_domain` is refused before any comparison, so that
      escape hatch is closed too.

    So there is no input for which control reaches `and post_data` with the
    first two operands true and that operand false. It is dead as a decision.
    The test below pins that the old None/None path is now an ordinary
    refusal.

    Production change that fails this: letting a None `uri_domain` reach the
    comparison unrefused, which reopens the None/None path and its crash.
    """

    def test_a_none_uri_domain_and_an_empty_document_are_refused(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        assert resolved({}, community, uri_domain=None) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


# ---------------------------------------------------------------------------
# Task 3: the create/update dispatch, the inReplyTo split, and the posted_at
# enrichment. Everything above this line reaches at most the three-part guard;
# everything below it gets through and exercises what the function DOES.
# ---------------------------------------------------------------------------

PARENT_URI = f'https://{PEER_OBJECT_HOST}/objects/parent'
PUBLISHED = '2024-01-01T00:00:00Z'
PUBLISHED_AS_DATETIME = datetime(2024, 1, 1, 0, 0)


def reply_note(in_reply_to=PARENT_URI, **fields):
    """A document that takes the reply branch: public, with a usable inReplyTo."""
    document = public_note()
    document['inReplyTo'] = in_reply_to
    document.update(fields)
    return document


def parent_post(community, author):
    """The Post a reply document's inReplyTo resolves to, found by find_reply_parent
    (app/activitypub/util.py) on ap_id alone -- its final lookup runs whether or
    not the URI carries a 'post' or 'comment' hint."""
    return make_post(community, author, ap_id=PARENT_URI)


class TestTheCreateUpdateDispatch:
    """`activity` is 'update' when the document carries 'updated' and 'create'
    otherwise, and it selects between four paths. The two update paths FALL
    BACK to create when the row they expect is not there, which is a fifth
    path and the one a reader is most likely to miss.

    Production change that fails these: inverting the `'updated' in post_data`
    test, or deleting either `else: activity = 'create'` fallback.
    """

    def test_an_update_for_a_post_that_exists_updates_it_in_place(self, app, peer_author):
        """**Identity and row count cannot tell this path from a create.** The
        first version of this test asserted only those two and passed with the
        dispatch forced to 'create', because Post.new catches the duplicate
        ap_id's IntegrityError and returns the EXISTING row (app/models.py) --
        same id, same count, and the document's content silently discarded.

        So the assertion that separates them is the body: the update path
        rewrites it from the document, and the create path leaves the row
        exactly as it found it.
        """
        community = make_community('news', host=PEER_OBJECT_HOST)
        existing = make_post(community, peer_author, ap_id=URI, title='the old title',
                             microblog=True)
        document = public_note()
        document['content'] = 'the new body'
        document['updated'] = PUBLISHED

        result = resolved(document, community)

        assert result.id == existing.id
        assert Post.query.filter_by(ap_id=URI).count() == 1
        assert 'the new body' in result.body_html

    def test_an_update_for_a_post_that_does_not_exist_falls_back_to_create(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        document = public_note()
        document['updated'] = PUBLISHED

        result = resolved(document, community)

        assert result.id == Post.query.filter_by(ap_id=URI).one().id
        assert result.user_id == peer_author.id

    def test_an_update_for_a_reply_that_exists_updates_it_in_place(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        post = parent_post(community, peer_author)
        existing = make_post_reply(post, peer_author)
        existing.ap_id = URI
        db.session.commit()
        document = reply_note(updated=PUBLISHED)

        result = resolved(document, community)

        assert result.id == existing.id
        assert PostReply.query.filter_by(ap_id=URI).count() == 1

    def test_an_update_for_a_reply_that_does_not_exist_falls_back_to_create(self, app, peer_author):
        """`activity` starts as 'update' because the document carries 'updated',
        no PostReply with this ap_id exists, so it flips to 'create' and the
        reply is created by that fallback.

        Until D35 was fixed this asserted None: the fallback fired correctly
        but the create it fell back to could not succeed, so the test could not
        distinguish a working fallback from a broken one.

        Production change that fails this: deleting the reply branch's
        `else: activity = 'create'`."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        parent_post(community, peer_author)
        document = reply_note(updated=PUBLISHED)

        result = resolved(document, community)

        assert result.id == PostReply.query.filter_by(ap_id=URI).one().id


class TestAnUpdateMustComeFromTheOwner:
    """PERM-3 residue A, owner ruling: the update branch rewrites a stored post or
    reply only when the fetched document's author owns it, as the inbox Update
    path checks `user.id == post.user_id`. A document on the same host naming a
    different author is refused and the stored row is left as it was.
    """

    def test_a_post_owned_by_someone_else_is_not_rewritten(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        owner = resolvable_remote_author(peer_author.instance, 'bob')
        existing = make_post(community, owner, ap_id=URI, title='the old title', microblog=True)
        old_body = existing.body_html
        document = public_note()
        document['content'] = 'the new body'
        document['updated'] = PUBLISHED

        result = resolved(document, community)

        assert result is None
        assert db.session.get(Post, existing.id).body_html == old_body

    def test_a_reply_owned_by_someone_else_is_not_rewritten(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        owner = resolvable_remote_author(peer_author.instance, 'bob')
        post = parent_post(community, owner)
        existing = make_post_reply(post, owner)
        existing.ap_id = URI
        db.session.commit()
        old_body = existing.body
        document = reply_note(updated=PUBLISHED, content='the new body')

        result = resolved(document, community)

        assert result is None
        assert db.session.get(PostReply, existing.id).body == old_body


class TestTheInReplyToSplit:
    """The branch turns on `'inReplyTo' in ... and ...['inReplyTo']`, so a
    PRESENT BUT FALSY inReplyTo takes the POST branch, not the reply branch.

    This is the divergence the plan flagged in advance: the near-duplicate
    resolve_remote_post_from_search tests `is not None` here, so the very same
    document becomes a Post through this function and a reply attempt through
    that one. Task 6 pins the other side; this is the half that lives here.

    Production change that fails these: changing the truthiness test to
    `is not None`, which is precisely the drift.
    """

    def test_an_empty_string_in_reply_to_becomes_a_post(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(reply_note(in_reply_to=''), community)

        assert isinstance(result, Post)
        assert PostReply.query.filter_by(ap_id=URI).count() == 0

    def test_a_null_in_reply_to_becomes_a_post(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(reply_note(in_reply_to=None), community)

        assert isinstance(result, Post)
        assert PostReply.query.filter_by(ap_id=URI).count() == 0


class TestThePostedAtEnrichment:
    """Guarded only by `'published' in post_data`, and it runs AFTER the row
    has been created. Both branches set `posted_at` on the row they made; the
    post branch sets `last_active` on the post itself, and the reply branch
    sets it on the reply's PARENT post, not on the reply.

    Production change that fails these: deleting the `'published' in post_data`
    guard, or moving `last_active` off the parent in the reply branch.
    """

    def test_a_published_value_lands_on_the_post_and_its_last_active(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note() | {'published': PUBLISHED}, community)

        assert result.posted_at == PUBLISHED_AS_DATETIME
        assert result.last_active == PUBLISHED_AS_DATETIME

    def test_a_published_value_lands_on_the_reply_and_on_its_parents_last_active(self, app, peer_author):
        """The reply branch's copy of the enrichment, which was unreachable
        until D35 was fixed and is the assertion the placeholder here was
        standing in for.

        Note what it sets and where: `posted_at` goes on the REPLY, but
        `last_active` goes on `post_reply.post` -- the parent -- not on the
        reply itself. That asymmetry with the post branch (which sets both on
        the same row) is the reason this deserves its own test rather than
        being assumed to mirror it.

        Production change that fails this: setting `last_active` on the reply
        instead of on its parent, or dropping the `'published' in post_data`
        guard.
        """
        community = make_community('news', host=PEER_OBJECT_HOST)
        post = parent_post(community, peer_author)

        result = resolved(reply_note(published=PUBLISHED), community)

        assert result.posted_at == PUBLISHED_AS_DATETIME
        assert result.post.id == post.id
        assert result.post.last_active == PUBLISHED_AS_DATETIME

    def test_without_published_the_row_keeps_the_time_it_was_created(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note(), community)

        assert result.posted_at is not None
        assert result.posted_at != PUBLISHED_AS_DATETIME


class TestAPublishedOffsetIsConvertedNotDiscarded:
    """This round's repair, and the test that asked for it.

    It used to read `TestAPublishedOffsetIsDiscardedNotConverted` and assert the
    defect: `posted_at` is a timestamp WITHOUT time zone, the peer's raw string
    went straight into it, and Postgres cast an offset-bearing ISO string by
    DROPPING the offset rather than converting. A peer publishing at 00:00+05:00 --
    19:00 the previous day in UTC -- got a row reading 00:00, so every timestamp
    from a peer in a non-zero offset was wrong by that offset, and `last_active`
    with it, which is what orders a community's listings.

    Its own docstring named the fix: "parsing `published` before assigning it --
    which is the fix, and this test then records what changed." D1340 did that.
    `parse_ap_timestamp` reads the string, converts an aware value to UTC and drops
    the tzinfo, so the stored instant no longer depends on the database session's
    TimeZone either.
    """

    def test_a_five_hour_offset_is_converted_to_utc(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note() | {'published': '2024-01-01T00:00:00+05:00'}, community)

        assert result.posted_at == datetime(2023, 12, 31, 19, 0)
        assert result.posted_at != datetime(2024, 1, 1, 0, 0)

    def test_a_utc_offset_is_unchanged(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note() | {'published': '2024-01-01T00:00:00+00:00'}, community)

        assert result.posted_at == datetime(2024, 1, 1, 0, 0)

    def test_the_last_active_it_orders_listings_by_moves_with_it(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note() | {'published': '2024-01-01T00:00:00+05:00'}, community)

        assert result.last_active == datetime(2023, 12, 31, 19, 0)


class TestAPublishedValueTheColumnCannotStore:
    """The other half of what this file recorded and D1340 repaired.

    It used to assert that a junk `published` reached `db.session.commit()` and
    raised `sqlalchemy.exc.DataError` (wrapping psycopg2's InvalidDatetimeFormat)
    out of the function, uncaught -- and that the Post row SURVIVED it, because
    `create_post` had committed before the enrichment ran. That is
    partially-applied ingest: a post in the database and a traceback for the
    activity that made it.

    Its docstring named both possible fixes: "parsing or validating `published`
    before the assignment, or moving the enrichment in front of the commit". D1340
    took the first. A value that does not read as a timestamp is now ignored, so
    the post keeps the time its creation gave it and nothing raises -- no
    exception, and therefore no partial ingest to describe.
    """

    @pytest.mark.parametrize('published', ['not a timestamp', '', 5, [], {},
                                           '2024-13-45T99:99:99Z', None])
    def test_a_junk_published_is_ignored_rather_than_raising(self, app, peer_author,
                                                            published):
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note() | {'published': published}, community)

        assert result is not None
        assert result.posted_at is not None
        assert result.posted_at != PUBLISHED_AS_DATETIME

    def test_the_post_is_created_once_and_completely(self, app, peer_author):
        """What replaces `test_the_post_is_left_behind_by_the_crash`: there is no
        crash to leave anything behind, so the assertion is that the row is
        whole."""
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note() | {'published': 'not a timestamp'}, community)

        assert Post.query.filter_by(ap_id=URI).count() == 1
        assert Post.query.filter_by(ap_id=URI).one().id == result.id
        assert result.last_active is not None

    @pytest.mark.parametrize('published', ['not a timestamp', 5, None])
    def test_a_junk_published_on_a_REPLY_is_ignored_too(self, app, peer_author,
                                                        published):
        """The reply branch has its own copy of the enrichment, and its own copy
        of the parse. A reply also writes `last_active` on its PARENT, so an
        unreadable value here used to poison the transaction that was updating a
        row the reply did not create.
        """
        community = make_community('news', host=PEER_OBJECT_HOST)
        post = parent_post(community, peer_author)
        before = post.last_active

        result = resolved(reply_note(published=published), community)

        assert isinstance(result, PostReply)
        assert result.posted_at is not None
        # `create_post_reply` bumps the parent's `last_active` to now by itself,
        # so the assertion is not that it did not move -- it did -- but that the
        # enrichment did not then move it again to an unreadable value.
        assert result.post.last_active >= before
        assert result.post.last_active != PUBLISHED_AS_DATETIME

    def test_a_replys_offset_is_converted_on_the_reply_and_the_parent(
            self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        parent_post(community, peer_author)

        result = resolved(reply_note(published='2024-01-01T00:00:00+05:00'), community)

        assert result.posted_at == datetime(2023, 12, 31, 19, 0)
        assert result.post.last_active == datetime(2023, 12, 31, 19, 0)


class TestTheHelpersReturningFalsy:
    """create_post and create_post_reply both refuse a non-public object --
    activitypub_visibility reads a document with neither 'to' nor 'cc' as
    'direct' -- and return None. The enrichment is inside `if post:` / `if
    post_reply:`, so it is skipped, and the function falls through to its
    final `return None`.

    Production change that fails these: hoisting the enrichment out of the
    truthiness guard, which would raise AttributeError on None rather than
    returning it.
    """

    def test_a_refused_post_returns_none_and_writes_nothing(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        document = note_document(attributed_to=AUTHOR_URI, uri=URI)

        assert resolved(document | {'published': PUBLISHED}, community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_refused_reply_returns_none_and_writes_nothing(self, app, peer_author, monkeypatch):
        """The reply half, and it needs the log to mean anything. Until D35
        was fixed the reply path returned None for TWO unrelated reasons --
        this refusal and the swallowed KeyError -- so asserting None alone
        passed under either, and this test could not tell which it had caught.
        The APLOG message is what separates them, and it is kept now that the
        second reason is gone: it is what pins the refusal itself.
        """
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        community = make_community('news', host=PEER_OBJECT_HOST)
        parent_post(community, peer_author)
        document = note_document(attributed_to=AUTHOR_URI, uri=URI)
        document['inReplyTo'] = PARENT_URI

        assert resolved(document | {'published': PUBLISHED}, community) is None
        assert PostReply.query.filter_by(ap_id=URI).count() == 0
        assert ActivityPubLog.query.one().exception_message == 'Non-public reply refused: direct'


class TestTheReplyCreatePathWorks:
    """D35, fixed 2026-08-29. This class replaces the characterisation that
    pinned it as broken.

    Both resolvers synthesise their activity as `{'id': ..., 'object': post_data}`
    with no 'type' key. `Post.new` reads that key defensively --
    `if 'type' in request_json and request_json['type'] == 'Update'` -- and
    `PostReply.new` did not: `if request_json and request_json['type'] == 'Update'`.
    So every remote reply raised `KeyError('type')` inside `PostReply.new`,
    `create_post_reply`'s `except Exception` swallowed it, and the resolver
    returned None. Silently, on three call paths: an Announce naming a reply
    URI, the microblog boost path, and the alpha API's resolve.

    The fix guards `PostReply.new`'s read exactly as `Post.new` guards its own.
    Deliberately NOT fixed by adding 'type' to the synthesised activity: that
    dict is built before `activity` may be flipped from 'update' to 'create'
    (see the dispatch above), so a 'type' set at build time would be wrong on
    the fallback path. The synthesised activity remains typeless, which is a
    separate and much smaller code-quality point.

    Production change that fails these: removing the `'type' in request_json`
    guard from `PostReply.new`.
    """

    def test_a_public_reply_is_created(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        post = parent_post(community, peer_author)

        result = resolved(reply_note(), community)

        assert result.id == PostReply.query.filter_by(ap_id=URI).one().id
        assert result.post_id == post.id
        assert result.user_id == peer_author.id

    def test_the_reply_carries_the_documents_body(self, app, peer_author):
        """Identity alone would not prove the document was used -- assert the
        content the peer sent actually landed on the row."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        parent_post(community, peer_author)
        document = reply_note()
        document['content'] = 'the reply body'

        result = resolved(document, community)

        assert 'the reply body' in result.body_html

    def test_the_post_path_survives_the_same_missing_key(self, app, peer_author):
        """The control that made the defect legible as an ASYMMETRY rather than
        as something about replies: the identical typeless activity down the
        post branch always worked, because `Post.new` guarded the read."""
        community = make_community('news', host=PEER_OBJECT_HOST)

        result = resolved(public_note(), community)

        assert result.id == Post.query.filter_by(ap_id=URI).one().id


class TestTheInboundCreateGate:
    """PERM-3, fixed (owner ruling). Fetched content is stored only if its author
    passes the checks an inbound Create does in `process_new_content`:
    `can_create_post` for a post and `can_create_post_reply` for a reply, which
    between them cover an instance ban or allowlist, a community ban, and the
    author's own bans. This function used to store the document with none of
    them. A refused object is not stored and the caller sees not-found (None).
    """

    def test_a_post_by_an_author_banned_from_the_community_is_not_stored(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        db.session.add(CommunityBan(community_id=community.id, user_id=peer_author.id))
        db.session.commit()

        assert resolved(public_note(), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_post_by_an_author_banned_from_posting_is_not_stored(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        peer_author.ban_posts = True
        db.session.commit()

        assert resolved(public_note(), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_post_from_a_banned_instance_is_not_stored(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        db.session.add(BannedInstances(domain=PEER_OBJECT_HOST))
        db.session.commit()

        assert resolved(public_note(), community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_reply_by_an_author_banned_from_commenting_is_not_stored(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        parent_post(community, peer_author)
        peer_author.ban_comments = True
        db.session.commit()

        assert resolved(reply_note(), community) is None
        assert PostReply.query.filter_by(ap_id=URI).count() == 0

    def test_an_update_by_an_author_banned_from_the_community_is_not_applied(self, app, peer_author):
        """The inbox gates an Update of existing content with the same check."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        post = make_post(community, peer_author, ap_id=URI)
        post.body = 'original'
        db.session.add(CommunityBan(community_id=community.id, user_id=peer_author.id))
        db.session.commit()
        document = public_note()
        document['updated'] = '2026-01-01T00:00:00Z'
        document['content'] = 'rewritten'

        assert resolved(document, community) is None
        assert db.session.get(Post, post.id).body == 'original'
