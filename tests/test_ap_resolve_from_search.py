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
"""

import pytest

from app.activitypub.util import resolve_remote_post_from_search
from app.models import Post
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
