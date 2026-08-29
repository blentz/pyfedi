"""resolve_remote_post_from_search (app/activitypub/util.py) is the third
resolver: given a URI, it fetches, unwraps NodeBB's two container shapes, and
creates a Post. Its own comment says it is called "from UI, via 'search' option
in navbar"; that comment is stale, and the register's D24 says so -- the `Move`
activity handler in app/activitypub/routes.py also calls it, with a
peer-supplied string, which is what makes this function peer-reachable rather
than user-driven.

Derived from this function's own source. Task 2 derived create_resolved_object's
separately and deliberately, because the two are near-duplicates and the whole
point of the exercise is to find where they differ:

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast
    src = open('app/activitypub/util.py').read()
    n = next(x for x in ast.walk(ast.parse(src))
             if isinstance(x, ast.FunctionDef) and x.name == 'resolve_remote_post_from_search')
    print('span', n.lineno, n.end_lineno)
    print('If:', len([x for x in ast.walk(n) if isinstance(x, ast.If)]))
    print('For:', len([x for x in ast.walk(n) if isinstance(x, ast.For)]))
    print('Try:', len([x for x in ast.walk(n) if isinstance(x, ast.Try)]))
    print('Return:', len([x for x in ast.walk(n) if isinstance(x, ast.Return)]))
    "

    span 4282 4373
    If: 22
    For: 1
    Try: 0
    Return: 8

This file covers the PRE-FETCH CHAIN: everything from the entry short-circuit
down to the second existence check. The author walk, the domain gate and the
creation below them are Tasks 5 and 6.

**TWO CONJUNCT COUNTS IN THE PLAN ARE LOW, and both matter to what a test has
to cover.** The plan describes the Conversation refetch as guarded by
"`type == 'Conversation'` and `isinstance(post_data['posts'], str)`" -- two
conjuncts -- and the NodeBB branch by four. Read from the source, they are four
and six:

    Conversation:      'type' in post_data
                       post_data['type'] == 'Conversation'
                       'posts' in post_data
                       isinstance(post_data['posts'], str)

    OrderedCollection: 'type' in post_data
                       post_data['type'] == 'OrderedCollection'
                       'totalItems' in post_data
                       post_data['totalItems'] > 0
                       'orderedItems' in post_data
                       isinstance(post_data['orderedItems'], list)

The missing ones in both cases are the `'key' in post_data` membership tests
that make the reads beside them safe. They are not decoration: without them
these guards would raise KeyError on any document lacking the key, which is
exactly the defect shape this campaign keeps finding elsewhere in this file.
Counting them out of the guard is how someone later "simplifies" them away, so
each is covered below with a case where it is the conjunct doing the work.

THE FETCHES. This function calls remote_object_to_json up to three times -- the
URI itself, the Conversation's `posts` URL, and the collection's first item --
and every one of those URLs comes from the peer's own document. Each test
registers exactly the routes it expects to be fetched; an unregistered fetch
raises through block_outbound_http, and a registered route that goes unfetched
fails at teardown. So the route set IS the assertion about how many fetches
happen, expressed as harness state rather than as a mock's call count.

That harness property earned its keep immediately: the first draft of the guard
tests stored their row under the request URI, so the ENTRY check answered and
no fetch ever happened. Four routes registered, none called, and
assert_all_called failed the tests. Without it, four "this guard does not fire"
tests would have passed while exercising nothing at all. The declared id is
kept distinct from the fetch URI throughout this file for that reason.

THE THREE-WAY DRIFT REPORT (Task 5's deliverable).

The register's D23 describes the duplication as a pair: this function and
create_resolved_object. It is a triple. verify_object_from_source carries the
same walk and the same gate, and sub-project 2b already fixed that copy -- so
the family is three functions wide, one of them already correct, and the
correct one is the fix template for the other two.

Derived mechanically, not by eye:

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast, difflib
    src = open('app/activitypub/util.py').read()
    def walk_src(name):
        n = next(x for x in ast.walk(ast.parse(src))
                 if isinstance(x, ast.FunctionDef) and x.name == name)
        for node in ast.walk(n):
            if isinstance(node, ast.If) and 'attributedTo' in ast.unparse(node.test):
                return ast.unparse(node)
    print(walk_src('create_resolved_object') == walk_src('resolve_remote_post_from_search'))
    "

    True

**The two unfixed copies are byte-identical** once normalised through the AST.
Not "near-duplicates" -- the same code twice. Every finding Task 2 recorded
against create_resolved_object's walk therefore holds here verbatim, and the
tests below are what would catch a fix that lands in one copy only.

Against the FIXED copy, four differences, of which three carry behaviour:

| difference | unfixed pair | verify_object_from_source | behaviour? |
|---|---|---|---|
| host comparison | `urlparse(...).netloc` | `host_of(...)` | **yes** -- D22/D24 |
| a bare embedded object | no arm; falls through with actor_domain None | `elif isinstance(..., dict) and 'id' in ...` | **yes** -- refused here, accepted there |
| an unusable attributedTo type | silent fall-through | `else: return None, '<reason>'` | **yes** -- diagnosis |
| arm order | Person-dict arm first | string arm first | no -- one element can match only one arm |

The arm-order row is listed because a deduplicating engineer will see it and
must know it is safe to normalise; the other three are the fix.

The `else` row is worth stating plainly: the fixed copy REFUSES an unusable
attributedTo with a stated reason, while both unfixed copies fall out of the
walk with `actor_domain` still None and are then refused by the domain gate for
what looks like an impersonation attempt. Same outcome, different explanation,
and only one of the three can tell an operator which happened.

MUTATION. Thirteen mutants -- both existence checks, every conjunct of both
guards individually, and the uri_domain reassignment -- each reverted before the
next. All thirteen are killed:

| mutant | failed |
|---|---|
| the entry short-circuit never fires | 1 |
| the second existence check never fires | 15 |
| Conversation: `'type' in post_data` dropped | 2 |
| Conversation: `== 'Conversation'` dropped | 1 |
| Conversation: `'posts' in post_data` dropped | 1 |
| Conversation: `isinstance(..., str)` dropped | 1 |
| OrderedCollection: `'type' in post_data` dropped | 2 |
| OrderedCollection: `== 'OrderedCollection'` dropped | 1 |
| OrderedCollection: `'totalItems' in post_data` dropped | 1 |
| OrderedCollection: `totalItems > 0` dropped | 1 |
| OrderedCollection: `'orderedItems' in post_data` dropped | 1 |
| OrderedCollection: `isinstance(..., list)` dropped | 1 |
| the uri_domain reassignment deleted | 1 -- after the test was rewritten |

The two membership conjuncts fail 2 rather than 1 because dropping them turns
the neighbouring read into a KeyError, which takes down the all-conjuncts-true
case as well as the case built for that conjunct. Every other conjunct fails
exactly the one test written for it -- which is the evidence that the guards'
ten conjuncts are individually pinned rather than covered in bulk.

**The uri_domain mutant survived the first run.** Deleting
`uri_domain = parsed_url.netloc` while keeping `uri = ...` left all 21 tests
passing, because the test had pre-stored the post and the second existence
check answered before the domain gate read uri_domain. The test now creates the
post instead of finding it. That is the third time in this sub-project an
assertion has routed around the code it named, and all three had the same
shape: asserting on a row's identity rather than on the write.

The second existence check's mutant failing 15 tests is worth reading as a
caveat rather than a triumph -- most guard tests here observe through that
check, so they are not independent of it. What makes them still discriminating
is the per-conjunct column above.

MUTATION, TASK 5's REGION -- this copy's walk, its gate, and the fallback.
Seven mutants, six killed:

| mutant | failed |
|---|---|
| the `break` moved inside the `isinstance(actor, str)` guard | 1 |
| the bare-string list arm deleted | 2 |
| the string `attributedTo` arm deleted | 3 |
| the gate never fires | 7 |
| the gate always fires | 6 |
| the find_community fallback deleted | 1 |
| the fallback's `and nodebb` conjunct dropped | **0 -- SURVIVES** |

**The `and nodebb` conjunct is redundant, and the mutant proves it rather than
merely failing to kill it.** `topic_post_data` is assigned `post_data` at the
top and diverges from it in exactly one place -- the OrderedCollection branch,
which replaces `post_data` with the item and leaves `topic_post_data` holding
the collection. That branch is also the only place `nodebb` becomes True. So
whenever `nodebb` is False the two names hold the same dict, and the fallback
`find_community(topic_post_data)` would repeat a lookup that has already
returned None. Dropping the conjunct costs one redundant query and changes no
outcome.

Reported, not fixed, and not covered: a test that pinned it would have to
assert on the query count, which is a promise about how the function works
rather than what it does. Task 7 files it as a code-quality row, distinct from
the defects around it -- nothing is wrong, one operand is just doing no work.

MUTATION, TASK 6's REGION -- creation, enrichment, dispatch, return shape.
Ten mutants, nine killed:

| mutant | failed |
|---|---|
| the `inReplyTo` test made truthiness -- the drift | 1 |
| the `published` guard forced true | 14 |
| `posted_at` set to now | 1 |
| `last_active` set to now | 1 |
| the creation-result guard removed | 2 |
| the dispatch guard's `totalItems > 1` dropped | 3 |
| the dispatch guard's `nodebb` operand dropped | 10 |
| the DEBUG split inverted | 2 |
| the dispatch passed the whole collection, not its tail | 1 |
| `return object if not in_reply_to else object.post` → `return object` | **0 -- SURVIVES** |

**The drift mutant dies**, which is the point of the pair of tests: rewriting
this copy's `is not None` to the other copy's truthiness -- the obvious
deduplication -- changes behaviour, and the suite now says so from both sides.

**The return-shape mutant survives because its else arm is dead.** `object.post`
runs only for a reply, and no reply can be created. It is left surviving rather
than chased: the honest statement is that this function's most surprising
contract is untestable until the reply defect is fixed, and TestTheReturnShape
says so in place of pretending otherwise.

COVERAGE, whole function, Tasks 4-6 together, span 4282-4373:
**73 of 73 statements, 45 of 46 branch arcs.** The one missing arc is
`if not in_reply_to:` taking its False path -- the reply side of the
enrichment, unreachable for the same reason. Every other branch in this
function is exercised.

The exhaust arc of the author walk's `for` loop was missing until the
measurement pointed at it: every list case written before then broke out
early. `test_a_list_of_neither_shape_runs_off_the_end_and_refuses` closes it,
and is a reminder that a walk's exit-by-exhaustion is a distinct path from
each of its early exits.
"""

from datetime import datetime

import pytest

from app.activitypub.util import resolve_remote_post_from_search
from app.models import ActivityPubLog, Post, PostReply
from tests.factories import (AS_PUBLIC_URI, PEER_OBJECT_HOST, PEER_OBJECT_URI, make_community,
                             make_post, make_site, note_document, resolvable_remote_author,
                             seed_community_owner, serve_remote_object)

URI = PEER_OBJECT_URI
AUTHOR_URI = f'https://{PEER_OBJECT_HOST}/users/alice'
TOPIC_URI = f'https://{PEER_OBJECT_HOST}/topic/1'
# The id the wrapper documents DECLARE, kept distinct from the URI they are
# fetched from. A test that stored its row under URI itself would be answered
# by the ENTRY check and never fetch at all, which is how the first draft of
# the guard tests silently tested nothing -- respx's assert_all_called caught
# it, four routes registered and never called.
DOC_URI = f'https://{PEER_OBJECT_HOST}/objects/declared'
ITEM_URI = f'https://{PEER_OBJECT_HOST}/objects/item'
OTHER_HOST = 'other.example'
PARENT_URI = f'https://{PEER_OBJECT_HOST}/objects/parent'
REPLY_URI = f'https://{PEER_OBJECT_HOST}/objects/reply2'

# remote_object_to_json sleeps 3 real seconds before each retry, and
# app.utils.get_request sleeps inside its own; the failure cases below would
# otherwise cost that in wall clock.
pytestmark = pytest.mark.usefixtures('no_real_sleeping')


@pytest.fixture
def peer_author(db_session):
    """The Instance and remote User the served documents are attributed to,
    seeded so find_actor_or_create resolves without an actor fetch."""
    make_site()
    instance = seed_community_owner(PEER_OBJECT_HOST)
    return resolvable_remote_author(instance, 'alice')


def public_note(uri=URI, attributed_to=AUTHOR_URI):
    """A Note that can survive the whole function: public, so create_post does
    not refuse it, and attributed to an author on its own host."""
    return note_document(attributed_to=attributed_to, uri=uri,
                         fields={'to': [AS_PUBLIC_URI]})


def conversation(posts=TOPIC_URI):
    """NodeBB's outer wrapper: a Conversation whose `posts` names the topic."""
    return {'id': DOC_URI, 'type': 'Conversation', 'posts': posts}


def ordered_collection(items=None, total=1, **overrides):
    """NodeBB's topic: an OrderedCollection whose first item is the post."""
    document = {'id': DOC_URI, 'type': 'OrderedCollection', 'totalItems': total,
                'orderedItems': [ITEM_URI] if items is None else items}
    document.update(overrides)
    return document


class TestTheEntryShortCircuit:
    """The function opens with `Post.get_by_ap_id(uri)` and returns any existing
    row BEFORE fetching anything.

    No route is registered, which is the assertion that no fetch happened: an
    unmatched request raises respx's AllMockedAssertionError, which is not an
    httpx.HTTPError, so remote_object_to_json's handler cannot swallow it and
    disguise a leaked fetch as an ordinary None.

    Production change that fails this: deleting the early return, or moving it
    below the fetch.
    """

    def test_a_uri_already_in_the_database_returns_without_fetching(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST)
        existing = make_post(community, peer_author, ap_id=URI)

        assert resolve_remote_post_from_search(URI).id == existing.id


class TestTheFetchFailing:
    """`if not post_data: return None` after the first fetch, reached by both
    kinds of falsy return -- None from a failure, and a falsy-but-not-None
    value from a 200 with an empty body.

    Production change that fails these: narrowing the guard to
    `if post_data is None`, which would carry `{}` into the Conversation test
    and then into the unguarded `post_data['id']` read below.
    """

    def test_a_404_returns_none(self, app, peer_author, http_mock):
        serve_remote_object(http_mock, URI, {'error': 'gone'}, status=404)

        assert resolve_remote_post_from_search(URI) is None

    def test_an_empty_json_document_returns_none(self, app, peer_author, http_mock):
        serve_remote_object(http_mock, URI, {})

        assert resolve_remote_post_from_search(URI) is None


class TestTheSecondExistenceCheck:
    """After the fetch chain the function looks the post up AGAIN, this time by
    the id the DOCUMENT states rather than the URI it was asked for. That is
    what catches "different but equivalent URLs" -- the case the first check
    cannot see.

    Production change that fails the first test: deleting the second check, or
    looking up `uri` again instead of `post_data['id']`.
    """

    def test_a_document_whose_id_is_already_stored_returns_that_row(self, app, peer_author, http_mock):
        """Fetched at one URL, already stored under the id it declares."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=ITEM_URI)
        serve_remote_object(http_mock, URI, public_note(uri=ITEM_URI))

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_a_document_with_no_id_raises_keyerror(self, app, peer_author, http_mock):
        """**An unguarded read on a peer document, pinned as a finding.**
        `post_data['id']` has no membership test in front of it, unlike the
        `'type' in post_data` tests just above it, so a document without an
        'id' raises KeyError out of the function.

        A peer chooses this document: on the Move path it chooses the URI too.
        The consequence is availability -- a failed task and a traceback, not a
        wrong row -- but it is the same unguarded-read shape this campaign has
        registered repeatedly, and the guards it sits between show the author
        knew the idiom.

        Task 7 should file it. Production change that fails this: a membership
        test or a .get() on that read, which is the fix.
        """
        document = public_note()
        del document['id']
        serve_remote_object(http_mock, URI, document)

        with pytest.raises(KeyError):
            resolve_remote_post_from_search(URI)


class TestTheConversationRefetch:
    """Four conjuncts, each covered with a case where it is the one that fails.
    When they all hold, the function fetches a SECOND document -- the URL the
    peer put in `posts` -- and continues with that.

    Production change that fails the first test: deleting the refetch.
    Production change that fails each of the others: dropping that conjunct
    from the guard, which would send a document that is not a NodeBB
    conversation off to fetch whatever `posts` happens to hold.
    """

    def test_all_four_conjuncts_true_refetches_and_uses_the_second_document(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=ITEM_URI)
        serve_remote_object(http_mock, URI, conversation())
        serve_remote_object(http_mock, TOPIC_URI, public_note(uri=ITEM_URI))

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_no_type_key_does_not_refetch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        document = conversation()
        del document['type']
        serve_remote_object(http_mock, URI, document)

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_a_different_type_does_not_refetch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        serve_remote_object(http_mock, URI, conversation() | {'type': 'Note'})

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_no_posts_key_does_not_refetch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        document = conversation()
        del document['posts']
        serve_remote_object(http_mock, URI, document)

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_a_posts_value_that_is_not_a_string_does_not_refetch(self, app, peer_author, http_mock):
        """A list here is the realistic wrong shape -- `posts` as an embedded
        collection rather than a URL -- and the isinstance test is the only
        thing standing between it and remote_object_to_json(a list)."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        serve_remote_object(http_mock, URI, conversation(posts=[TOPIC_URI]))

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_a_refetch_that_fails_returns_none(self, app, peer_author, http_mock):
        serve_remote_object(http_mock, URI, conversation())
        serve_remote_object(http_mock, TOPIC_URI, {'error': 'gone'}, status=404)

        assert resolve_remote_post_from_search(URI) is None


class TestTheOrderedCollectionBranch:
    """Six conjuncts, and the branch that makes a topic resolve to its first
    post. Each conjunct gets a case where it is the one that fails.

    Production change that fails the first test: deleting the branch.
    Production change that fails each of the others: dropping that conjunct.
    """

    def test_all_six_conjuncts_true_resolves_the_first_item(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=ITEM_URI)
        serve_remote_object(http_mock, URI, ordered_collection())
        serve_remote_object(http_mock, ITEM_URI, public_note(uri=ITEM_URI))

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_no_type_key_does_not_take_the_branch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        document = ordered_collection()
        del document['type']
        serve_remote_object(http_mock, URI, document)

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_a_different_type_does_not_take_the_branch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        serve_remote_object(http_mock, URI, ordered_collection() | {'type': 'Collection'})

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_no_total_items_key_does_not_take_the_branch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        document = ordered_collection()
        del document['totalItems']
        serve_remote_object(http_mock, URI, document)

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_zero_total_items_does_not_take_the_branch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        serve_remote_object(http_mock, URI, ordered_collection(total=0))

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_no_ordered_items_key_does_not_take_the_branch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        document = ordered_collection()
        del document['orderedItems']
        serve_remote_object(http_mock, URI, document)

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_ordered_items_that_is_not_a_list_does_not_take_the_branch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=DOC_URI)
        serve_remote_object(http_mock, URI, ordered_collection(items=ITEM_URI))

        assert resolve_remote_post_from_search(URI).id == stored.id

    def test_an_item_fetch_that_fails_returns_none(self, app, peer_author, http_mock):
        serve_remote_object(http_mock, URI, ordered_collection())
        serve_remote_object(http_mock, ITEM_URI, {'error': 'gone'}, status=404)

        assert resolve_remote_post_from_search(URI) is None


class TestBothWrappersTogether:
    """The shape the branches were written for: a Conversation naming a topic,
    the topic an OrderedCollection, the collection's first entry the post.
    Three fetches, and only the third document survives as `post_data`.

    Production change that fails this: making either unwrap exclusive of the
    other, or reordering them.
    """

    def test_a_conversation_wrapping_a_collection_resolves_the_first_item(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        stored = make_post(community, peer_author, ap_id=ITEM_URI)
        serve_remote_object(http_mock, URI, conversation())
        serve_remote_object(http_mock, TOPIC_URI, ordered_collection())
        serve_remote_object(http_mock, ITEM_URI, public_note(uri=ITEM_URI))

        assert resolve_remote_post_from_search(URI).id == stored.id


class TestTheUriAndUriDomainReassignment:
    """The NodeBB branch replaces BOTH `uri` and `uri_domain` from
    `orderedItems[0]`, so a collection may point at a different host than the
    collection itself -- and the domain gate further down then compares the
    author against THAT host, not the one the caller asked about.

    The register calls this out under D24's adjacent findings: a host asked for
    one URI can redirect the resolution to a different host, and the comparison
    that was supposed to police impersonation ends up comparing the new host
    against itself and passing.

    Pinned here as behaviour, from the pre-fetch side: the post that gets
    created is the one on the OTHER host, authored by an actor on that other
    host, reached from a URI on the first host.

    Production change that fails this: not reassigning `uri_domain` alongside
    `uri`, which would make the gate compare the author against the original
    host and refuse.
    """

    def test_a_collection_may_redirect_the_resolution_to_another_host(self, app, peer_author, http_mock):
        """**The first version of this test did not pin the reassignment**, and
        the mutation run said so: deleting `uri_domain = parsed_url.netloc`
        while keeping `uri = ...` left all 21 tests passing. The test had
        pre-stored the post, so the SECOND EXISTENCE CHECK answered before the
        domain gate ever read uri_domain. Third time in this sub-project that an
        assertion routed around the code it named -- assert on what the path
        WROTE, not on a row that was already there.

        So nothing is pre-stored here: the post has to be created, which means
        the gate has to pass, which means uri_domain has to have been moved to
        the item's host. With the reassignment deleted the gate compares the
        original host against the item author's and refuses.
        """
        other_uri = f'https://{OTHER_HOST}/objects/elsewhere'
        other_author = f'https://{OTHER_HOST}/users/mallory'
        other_instance = seed_community_owner(OTHER_HOST)
        resolvable_remote_author(other_instance, 'mallory')
        community = make_community('news', host=PEER_OBJECT_HOST)
        document = public_note(uri=other_uri, attributed_to=other_author)
        document['audience'] = community.ap_profile_id
        serve_remote_object(http_mock, URI, ordered_collection(items=[other_uri]))
        serve_remote_object(http_mock, other_uri, document)

        result = resolve_remote_post_from_search(URI)

        assert result.ap_id == other_uri
        assert result.author.ap_profile_id == other_author
        assert Post.query.filter_by(ap_id=other_uri).count() == 1


# ---------------------------------------------------------------------------
# Task 5: this function's own copy of the attributedTo walk, its domain gate,
# and the find_community fallback. The drift report the walk produced is in the
# module docstring above, under THE THREE-WAY DRIFT REPORT.
# ---------------------------------------------------------------------------

def resolvable(document, community, **extra):
    """A served document that can reach creation: public, addressed to a
    community find_community can resolve, and attributed to a seeded actor.

    find_community reads 'audience', 'cc', 'to' and 'target', but only when the
    value is a STRING -- public_note's `to` is a list and is therefore invisible
    to it, so the audience is what actually locates the community here.
    """
    document = dict(document)
    document['audience'] = community.ap_profile_id
    document.update(extra)
    return document


class TestThisCopysAttributedToWalk:
    """The same six list/string shapes Task 2 covered in create_resolved_object,
    exercised through THIS function because the two copies are separate code.

    Derived independently, and the derivation is the finding: the two walks are
    byte-identical (see the drift report above). So these tests are not
    redundant with Task 2's -- they are the evidence that the duplication is
    exact, and they are what would catch a fix landing in one copy only.

    Production change that fails these: any edit to this copy's walk that
    Task 2's tests would catch in the other one.
    """

    def test_a_string_author_on_the_uris_host_is_used(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, resolvable(public_note(), community))

        assert resolve_remote_post_from_search(URI).author.id == peer_author.id

    def test_a_person_dict_in_a_list_is_used(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        document = resolvable(public_note(attributed_to=[{'type': 'Person', 'id': AUTHOR_URI}]), community)
        serve_remote_object(http_mock, URI, document)

        assert resolve_remote_post_from_search(URI).author.id == peer_author.id

    def test_a_bare_string_in_a_list_is_used(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, resolvable(public_note(attributed_to=[AUTHOR_URI]), community))

        assert resolve_remote_post_from_search(URI).author.id == peer_author.id

    def test_a_non_person_dict_does_not_end_the_search(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        attributed_to = [{'type': 'Service', 'id': f'https://{OTHER_HOST}/users/svc'}, AUTHOR_URI]
        serve_remote_object(http_mock, URI, resolvable(public_note(attributed_to=attributed_to), community))

        assert resolve_remote_post_from_search(URI).author.id == peer_author.id

    def test_a_person_dict_ends_the_search_even_when_it_yields_nothing(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        attributed_to = [{'type': 'Person', 'id': {'nested': 'not a string'}}, AUTHOR_URI]
        serve_remote_object(http_mock, URI, resolvable(public_note(attributed_to=attributed_to), community))

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_person_dict_on_another_host_ends_the_search_too(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        attributed_to = [{'type': 'Person', 'id': f'https://{OTHER_HOST}/users/mallory'}, AUTHOR_URI]
        serve_remote_object(http_mock, URI, resolvable(public_note(attributed_to=attributed_to), community))

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_bare_embedded_object_is_refused_here_but_accepted_by_the_fixed_copy(self, app, peer_author, http_mock):
        """The drift row with the sharpest consequence. This copy has a string
        arm and a list arm and nothing else, so a single embedded Person object
        -- ordinary ActivityStreams -- matches neither and the author is never
        found. verify_object_from_source, the copy 2b fixed, grew a dict arm
        (`elif isinstance(..., dict) and 'id' in ...`) that handles exactly
        this.

        So the shape is already fixed once in this file. Two copies still
        refuse it. Pinned as today's behaviour.
        """
        community = make_community('news', host=PEER_OBJECT_HOST)
        attributed_to = {'type': 'Person', 'id': AUTHOR_URI}
        serve_remote_object(http_mock, URI, resolvable(public_note(attributed_to=attributed_to), community))

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_list_of_neither_shape_runs_off_the_end_and_refuses(self, app, peer_author, http_mock):
        """The loop's exhaust arc: no element matches either arm, so it ends
        without ever breaking and actor_domain is still None at the gate.

        Added after the branch-arc measurement showed `for a in attributed_to:`
        had no exit-by-exhaustion arc -- every list case written before it
        broke out early.
        """
        community = make_community('news', host=PEER_OBJECT_HOST)
        attributed_to = [{'type': 'Service', 'id': f'https://{OTHER_HOST}/users/svc'}, 42, None]
        serve_remote_object(http_mock, URI, resolvable(public_note(attributed_to=attributed_to), community))

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_no_attributed_to_key_refuses(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        document = resolvable(public_note(), community)
        del document['attributedTo']
        serve_remote_object(http_mock, URI, document)

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestThisCopysDomainGate:
    """D24's surface: `uri_domain != actor_domain`, both sides raw
    `urlparse(...).netloc`. Unlike create_resolved_object's, BOTH operands here
    are derived inside this function from raw strings, which is why the
    register rates this one inconsistency-dependent rather than systematic --
    a peer whose authority is spelled the same way everywhere passes.

    These pin what it does today. A fix to D24 flips the last two to a created
    post.
    """

    def test_an_author_on_another_host_is_refused(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        other_author = f'https://{OTHER_HOST}/users/mallory'
        serve_remote_object(http_mock, URI, resolvable(public_note(attributed_to=other_author), community))

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_case_difference_in_the_author_host_refuses(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        mixed = f'https://{PEER_OBJECT_HOST.capitalize()}/users/alice'
        serve_remote_object(http_mock, URI, resolvable(public_note(attributed_to=mixed), community))

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_an_explicit_default_port_on_the_author_refuses(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        ported = f'https://{PEER_OBJECT_HOST}:443/users/alice'
        serve_remote_object(http_mock, URI, resolvable(public_note(attributed_to=ported), community))

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestTheFindCommunityFallback:
    """`if not community and nodebb: community = find_community(topic_post_data)`
    -- the "use 'audience' from the topic when the post itself does not say
    where it went" path.

    It can only matter on the NodeBB branch, because `topic_post_data` diverges
    from `post_data` in exactly one place: the OrderedCollection branch replaces
    post_data with the item and leaves topic_post_data holding the collection.
    Everywhere else the two names hold the same dict, so the fallback would be
    a second identical lookup. That is why the `and nodebb` conjunct is
    reported below rather than covered -- see TestTheNodebbConjunctIsRedundant.

    Production change that fails the first test: deleting the fallback.
    """

    def test_the_topics_audience_is_used_when_the_item_has_none(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        collection = ordered_collection() | {'audience': community.ap_profile_id}
        serve_remote_object(http_mock, URI, collection)
        serve_remote_object(http_mock, ITEM_URI, public_note(uri=ITEM_URI))

        result = resolve_remote_post_from_search(URI)

        assert result.ap_id == ITEM_URI
        assert result.community_id == community.id

    def test_no_audience_anywhere_returns_none(self, app, peer_author, http_mock):
        make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, ordered_collection())
        serve_remote_object(http_mock, ITEM_URI, public_note(uri=ITEM_URI))

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=ITEM_URI).count() == 0


# ---------------------------------------------------------------------------
# Task 6: creation, enrichment, the NodeBB background dispatch, return shape.
# ---------------------------------------------------------------------------

class TestThisCopysInReplyToSplit:
    """`'inReplyTo' in post_data and post_data['inReplyTo'] is not None`.

    **The confirmed divergence from create_resolved_object**, which tests the
    same key for TRUTHINESS. A present-but-empty-string inReplyTo therefore
    takes the REPLY branch here and the POST branch there: one peer document,
    two outcomes, decided only by which resolver received it. Task 3 pinned the
    other side; this is this side, and together they are the drift report's
    strongest row because both behaviours are now nailed down.

    Production change that fails the empty-string test: changing this copy to
    truthiness -- which is the deduplication a reader would reach for, and it
    silently changes behaviour.
    """

    def test_an_empty_string_in_reply_to_takes_the_reply_branch(self, app, peer_author, http_mock):
        """`'' is not None` is True, so this document is treated as a reply --
        and the reply path cannot create anything (see below), so the answer is
        None. create_resolved_object, given the identical document, creates a
        Post."""
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, resolvable(public_note(), community, inReplyTo=''))

        assert resolve_remote_post_from_search(URI) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_a_null_in_reply_to_takes_the_post_branch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, resolvable(public_note(), community, inReplyTo=None))

        assert resolve_remote_post_from_search(URI).ap_id == URI

    def test_no_in_reply_to_key_takes_the_post_branch(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, resolvable(public_note(), community))

        assert resolve_remote_post_from_search(URI).ap_id == URI


class TestTheReplyPathIsBrokenHereToo:
    """The defect Task 3 found in create_resolved_object, present in this copy
    for the same reason: the synthesised activity is
    `{'id': ..., 'object': post_data}` with no 'type' key, PostReply.new reads
    `request_json['type']` unguarded, create_post_reply swallows the KeyError.

    Task 3 predicted this would hold here, from the source alone. It does --
    verified, not assumed. So BOTH unfixed resolvers silently fail every remote
    reply, and the register should say so about the pair rather than about one
    of them.

    Production change that fails these: adding 'type' to either synthesised
    activity, or guarding PostReply.new's read.
    """

    def test_a_well_formed_public_reply_creates_nothing(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        make_post(community, peer_author, ap_id=PARENT_URI)
        serve_remote_object(http_mock, URI, resolvable(public_note(), community, inReplyTo=PARENT_URI))

        assert resolve_remote_post_from_search(URI) is None
        assert PostReply.query.count() == 0

    def test_the_swallowed_exception_is_the_missing_type_key(self, app, peer_author, http_mock, monkeypatch):
        monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
        community = make_community('news', host=PEER_OBJECT_HOST)
        make_post(community, peer_author, ap_id=PARENT_URI)
        serve_remote_object(http_mock, URI, resolvable(public_note(), community, inReplyTo=PARENT_URI))

        resolve_remote_post_from_search(URI)

        assert ActivityPubLog.query.one().exception_message == "'type'"


class TestTheEnrichment:
    """`object.posted_at` is set whenever the document carries 'published', and
    `object.last_active` only when the object is not a reply.

    The `not in_reply_to` distinction cannot be exercised today: no reply is
    ever created, so the reply side of it is unreachable for the same reason
    Task 3's reply enrichment was. Pinned as far as it goes, which is the post
    side.

    Production change that fails these: deleting the `'published' in post_data`
    guard, or setting either column to utcnow() instead of the peer's value.
    """

    def test_published_lands_on_posted_at_and_last_active(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        document = resolvable(public_note(), community, published='2024-01-01T00:00:00Z')
        serve_remote_object(http_mock, URI, document)

        result = resolve_remote_post_from_search(URI)

        assert result.posted_at == datetime(2024, 1, 1, 0, 0)
        assert result.last_active == datetime(2024, 1, 1, 0, 0)

    def test_without_published_the_row_keeps_its_creation_time(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, resolvable(public_note(), community))

        result = resolve_remote_post_from_search(URI)

        assert result.posted_at is not None
        assert result.posted_at != datetime(2024, 1, 1, 0, 0)


class TestTheReturnShape:
    """`return object if not in_reply_to else object.post` -- a reply is meant
    to resolve to its PARENT post, not to the reply. That contract is genuinely
    surprising and the plan asked for it to be pinned explicitly.

    It cannot be: the else arm is unreachable while the reply path cannot
    create a reply. What is pinned here is the reachable half plus the fact
    that the other half is dead today, so that a fix to the reply defect is
    known to also make this contract testable for the first time.
    """

    def test_a_post_resolves_to_itself(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, resolvable(public_note(), community))

        result = resolve_remote_post_from_search(URI)

        assert isinstance(result, Post)
        assert result.ap_id == URI


class Recorder:
    """Stands in for get_nodebb_replies_in_background and records HOW it was
    called -- inline, or through .delay.

    A mock is used here deliberately and only here. The dispatch mode is not
    observable any other way: under this suite's eager Celery, `.delay()` runs
    the task INLINE and lets its exceptions propagate, exactly as the direct
    call does. That was measured, not assumed -- with the reply URI's route
    left unregistered, both DEBUG settings raised the identical
    AllMockedAssertionError from inside the task.

    So the `if current_app.debug` branch is behaviourally inert in the suite,
    and a test that tried to tell the modes apart by their effects would be
    pinning eager Celery rather than this function. Recording the call is the
    honest alternative, and the docstring is the caveat that goes with it.
    """

    def __init__(self):
        self.inline = []
        self.delayed = []

    def __call__(self, *args):
        self.inline.append(args)

    def delay(self, *args):
        self.delayed.append(args)


class TestTheNodebbBackgroundDispatch:
    """Guarded by `nodebb and topic_post_data['totalItems'] > 1`, and split on
    `current_app.debug`.

    Production change that fails these: deleting either arm of the DEBUG split,
    dropping the `totalItems > 1` test, or passing something other than the
    tail of orderedItems.
    """

    def test_debug_true_calls_the_task_inline(self, app, peer_author, http_mock, monkeypatch):
        recorder = Recorder()
        monkeypatch.setattr('app.activitypub.util.get_nodebb_replies_in_background', recorder)
        monkeypatch.setitem(app.config, 'DEBUG', True)
        community = make_community('news', host=PEER_OBJECT_HOST)
        collection = ordered_collection(items=[ITEM_URI, REPLY_URI], total=2)
        collection['audience'] = community.ap_profile_id
        serve_remote_object(http_mock, URI, collection)
        serve_remote_object(http_mock, ITEM_URI, public_note(uri=ITEM_URI))

        resolve_remote_post_from_search(URI)

        assert recorder.inline == [([REPLY_URI], community.id)]
        assert recorder.delayed == []

    def test_debug_false_calls_the_task_through_delay(self, app, peer_author, http_mock, monkeypatch):
        recorder = Recorder()
        monkeypatch.setattr('app.activitypub.util.get_nodebb_replies_in_background', recorder)
        monkeypatch.setitem(app.config, 'DEBUG', False)
        community = make_community('news', host=PEER_OBJECT_HOST)
        collection = ordered_collection(items=[ITEM_URI, REPLY_URI], total=2)
        collection['audience'] = community.ap_profile_id
        serve_remote_object(http_mock, URI, collection)
        serve_remote_object(http_mock, ITEM_URI, public_note(uri=ITEM_URI))

        resolve_remote_post_from_search(URI)

        assert recorder.delayed == [([REPLY_URI], community.id)]
        assert recorder.inline == []

    def test_a_single_item_topic_dispatches_nothing(self, app, peer_author, http_mock, monkeypatch):
        recorder = Recorder()
        monkeypatch.setattr('app.activitypub.util.get_nodebb_replies_in_background', recorder)
        community = make_community('news', host=PEER_OBJECT_HOST)
        collection = ordered_collection(total=1)
        collection['audience'] = community.ap_profile_id
        serve_remote_object(http_mock, URI, collection)
        serve_remote_object(http_mock, ITEM_URI, public_note(uri=ITEM_URI))

        resolve_remote_post_from_search(URI)

        assert recorder.inline == [] and recorder.delayed == []

    def test_a_document_that_is_not_a_topic_dispatches_nothing(self, app, peer_author, http_mock, monkeypatch):
        recorder = Recorder()
        monkeypatch.setattr('app.activitypub.util.get_nodebb_replies_in_background', recorder)
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, resolvable(public_note(), community))

        resolve_remote_post_from_search(URI)

        assert recorder.inline == [] and recorder.delayed == []
