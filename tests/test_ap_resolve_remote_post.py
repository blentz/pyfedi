"""resolve_remote_post (app/activitypub/util.py) is the entry point the inbox
uses when the object of an Announce is just a URL. It gates on where the
announcing community lives, fetches the URI, and hands the parsed document to
create_resolved_object.

It has two `if` statements and four branch arcs. Derived, not counted by eye:

    podman-compose -f compose.test.yaml exec -T -w /app test-runner python -c "
    import ast
    src = open('app/activitypub/util.py').read()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == 'resolve_remote_post':
            print('If:', len([x for x in ast.walk(n) if isinstance(x, ast.If)]))
            for s in [x for x in ast.walk(n) if isinstance(x, ast.If)]:
                print('  ', ast.unparse(s.test)[:100])
    "

    If: 2
       announce_actor_domain != 'ovo.st' and (not nodebb) and (announce_actor_domain != uri_domain)
       not post_data

| guard | true arc | false arc |
|---|---|---|
| the three-part domain gate | returns None before any fetch | falls through to the fetch |
| `not post_data` | returns None after the fetch | falls through to create_resolved_object |

THE DOMAIN GATE is a conjunction of three operands, each of which can on its
own let the call through:

- `announce_actor_domain != 'ovo.st'` -- a hardcoded instance name. TestOvoSt
  below covers it. Registered as a defect distinct from the netloc comparison
  and left unfixed; a hostname literal in a resolver means one named peer, and
  only that peer, is exempt from the same-host rule, with nothing in the code
  saying why.
- `not nodebb` -- a caller-supplied bypass, passed True only by
  get_nodebb_replies_in_background and by resolve_remote_post_from_search's
  NodeBB branch. TestNodebbBypass covers it. Also registered as its own
  defect, and also left unfixed: it is a keyword-argument-shaped hole in the
  impersonation check, opened by a caller rather than by the data.
- `announce_actor_domain != uri_domain` -- the same-host rule proper, and the
  one registered as D21. Both sides are raw `urlparse(...).netloc`, so the
  comparison is case-sensitive, port-sensitive and userinfo-sensitive.
  TestRawNetlocComparison pins what that does TODAY; it is characterisation,
  not endorsement.

The three are covered separately and deliberately not folded together: they
fail in different ways and a fix to one would not touch the others. Each of
the three classes below drives a case where its own operand is the FALSE one
and the other two are true, so the operand under test is the only thing
letting the call through -- dropping it from the conjunction changes that
test's result and nothing else's.

THE `not post_data` GUARD sees two genuinely different falsy values, because
remote_object_to_json (same module) has two kinds of falsy return:

- None, from every failure path it has -- a non-200/non-401 status, a body
  that does not parse, or a transport error that survives its retry.
- A falsy value that is NOT None, from its success path: the 200 branch
  returns `object_request.json()` unexamined, so a peer serving `{}` (or
  `[]`, or `0`, or `false`) with a 200 and a JSON content type gets that value
  returned as-is. Established by reading the function, not assumed: its 200
  branch has no truthiness check of any kind between `.json()` and `return`.

Both are covered below, because they are different production events -- one is
"we could not get the document", the other is "the peer served us an empty
one" -- and `not post_data` is what collapses them into the same answer.

Helpers commit, so an assertion that a Post exists proves nothing about who
wrote it: create_post commits through Post.new, and find_actor_or_create
commits when it creates an actor. Every assertion below is therefore on the
CONTENTS of the row -- its ap_id, which comes from the fetched document -- or
on the identity of the object returned, never on a row's mere existence, and
never on a mock.
"""

import pytest

from app.activitypub.util import resolve_remote_post
from app.models import Post
from tests.factories import (AS_PUBLIC_URI, PEER_OBJECT_HOST, PEER_OBJECT_URI, make_community,
                             make_site, note_document, resolvable_remote_author,
                             seed_community_owner, serve_remote_object)

URI = PEER_OBJECT_URI
AUTHOR_URI = f'https://{PEER_OBJECT_HOST}/users/alice'

# remote_object_to_json sleeps 3 real seconds before each of its retries, and
# app.utils.get_request sleeps again inside its own; the transport-failure
# cases below would otherwise cost that in wall clock.
pytestmark = pytest.mark.usefixtures('no_real_sleeping')


@pytest.fixture
def peer_author(db_session):
    """The Instance and remote User the served document is attributed to.

    Created before every call below so find_actor_or_create takes its
    found-existing path: creating the actor instead would fetch the actor
    document, which no test here registers a route for.
    """
    make_site()
    instance = seed_community_owner(PEER_OBJECT_HOST)
    return resolvable_remote_author(instance, 'alice')


def public_note(attributed_to=AUTHOR_URI):
    """The served document, addressed to the public collection.

    Without 'to', activitypub_visibility classifies the object as 'direct' and
    create_post refuses it, so create_resolved_object returns None for reasons
    that have nothing to do with the gate under test here.
    """
    return note_document(attributed_to=attributed_to, uri=URI, fields={'to': [AS_PUBLIC_URI]})


def resolved_post(community, uri=URI, nodebb=False):
    return resolve_remote_post(uri, community, None, False, nodebb=nodebb)


class TestSameHostAnnounce:
    """`announce_actor_domain != uri_domain` is the operand doing the work:
    the community is not on ovo.st and nodebb is False, so the equality of
    the two netlocs is the only thing letting the call through.

    Production change that fails this: dropping the third operand from the
    conjunction, which would refuse a community announcing a post on its own
    host.
    """

    def test_a_post_on_the_announcing_communitys_own_host_is_created(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, public_note())

        result = resolved_post(community)

        assert result.ap_id == URI


class TestOvoSt:
    """`announce_actor_domain != 'ovo.st'` is the operand doing the work: the
    two netlocs disagree and nodebb is False, so the hardcoded hostname is the
    only thing letting a cross-host announce through.

    Production change that fails this: dropping the ovo.st operand, or
    changing the literal. Reported as a defect, not fixed -- a resolver that
    names one peer in its source and exempts it from the same-host rule is a
    trust decision with no stated reason and no way to revoke it short of an
    edit.
    """

    def test_an_ovo_st_community_may_announce_a_post_hosted_elsewhere(self, app, peer_author, http_mock):
        community = make_community('news', host='ovo.st')
        serve_remote_object(http_mock, URI, public_note())

        result = resolved_post(community)

        assert result.ap_id == URI


class TestNodebbBypass:
    """`not nodebb` is the operand doing the work: the community is not on
    ovo.st and the two netlocs disagree, so the caller's nodebb=True is the
    only thing letting the call through.

    Production change that fails this: dropping the `not nodebb` operand.
    Reported as a defect, not fixed -- the bypass is opened by a caller's
    keyword argument rather than by anything in the data, so the
    impersonation check the gate exists to perform can be waived from a call
    site that never sees the hosts involved.
    """

    def test_nodebb_true_lets_a_cross_host_post_through(self, app, peer_author, http_mock):
        community = make_community('news', host='forum.example')
        serve_remote_object(http_mock, URI, public_note())

        result = resolved_post(community, nodebb=True)

        assert result.ap_id == URI


class TestGateRefusal:
    """All three operands true: an ordinary community announcing a post on
    some other host. The gate returns None BEFORE the fetch.

    No route is registered, which is the assertion that the fetch did not
    happen -- expressed as a property of the harness rather than as a mock
    call count. block_outbound_http raises on any unmatched request, and it
    raises respx's AllMockedAssertionError, which is not an httpx.HTTPError,
    so remote_object_to_json's `except httpx.HTTPError` cannot swallow it and
    turn a leaked fetch back into the same None this test expects.

    Production change that fails this: replacing the conjunction with a
    disjunction, or deleting the guard entirely.
    """

    def test_a_cross_host_announce_returns_none_and_writes_nothing(self, app, peer_author):
        community = make_community('news', host='forum.example')

        assert resolved_post(community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestRawNetlocComparison:
    """D21, pinned as it behaves today: both sides of the comparison are raw
    `urlparse(...).netloc`, with no case folding, no default-port handling and
    no userinfo stripping. These two cases are the same authority written two
    ways, and both are refused.

    Characterisation only -- the campaign registered this and did not fix it,
    and these tests are what make a later fix verifiable. A fix would flip
    both of them to a created post, which is the point: today nothing would
    notice.

    Production change that fails these: none that is a fix. They fail if the
    comparison starts normalising, which is exactly what a reader wants to be
    told when someone lands D21's fix.
    """

    def test_a_case_difference_in_the_announce_host_refuses(self, app, peer_author):
        community = make_community('news', host=PEER_OBJECT_HOST.capitalize())

        assert resolved_post(community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_an_explicit_default_port_on_the_announce_host_refuses(self, app, peer_author):
        community = make_community('news', host=f'{PEER_OBJECT_HOST}:443')

        assert resolved_post(community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestFetchReturnsNone:
    """The `not post_data` guard's true arc, reached by remote_object_to_json
    returning None -- here from a 404, one of its three None paths.

    Production change that fails this: deleting the `if not post_data` guard,
    which would pass None into create_resolved_object and raise TypeError on
    `'attributedTo' in post_data`.
    """

    def test_a_404_at_the_post_uri_returns_none(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, {'error': 'gone'}, status=404)

        assert resolved_post(community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0

    def test_an_unparseable_body_at_the_post_uri_returns_none(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, content=b'not json')

        assert resolved_post(community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestFetchReturnsFalsyButNotNone:
    """The same guard's true arc reached by the OTHER kind of falsy value.

    remote_object_to_json's 200 branch returns `object_request.json()` with no
    truthiness check, so a peer serving an empty JSON object with a 200 gets
    `{}` returned -- falsy, but not None. `not post_data` catches it; an
    `is None` check would not, and would then hand `{}` to
    create_resolved_object, which walks straight past its `'attributedTo' in
    post_data` guard into `uri_domain != actor_domain` with actor_domain None
    and returns None anyway. So the difference is invisible in the return
    value and visible only here, in which guard fires.

    Production change that fails this: narrowing `if not post_data` to
    `if post_data is None` -- with `{}` that reaches find_actor_or_create(None)
    inside create_resolved_object, which raises AttributeError on
    `actor.strip()`, rather than returning None.
    """

    def test_an_empty_json_document_returns_none(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, {})

        assert resolved_post(community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0


class TestDelegation:
    """resolve_remote_post's last statement returns create_resolved_object's
    result unchanged -- both when that is a row and when it is None.

    The Post row alone would prove nothing about the delegation, because
    create_post commits it from inside create_resolved_object whether or not
    the return value survives the trip back. So the assertion is that the
    OBJECT returned by resolve_remote_post IS the row in the database, by
    primary key, and that the field create_resolved_object took from the
    fetched document arrived on it.

    Production change that fails these: replacing the tail call's `return`
    with a bare call, or with `return None`; and, for the second test,
    replacing it with anything that manufactures a value instead of passing
    the callee's through.
    """

    def test_the_object_returned_is_the_row_that_was_written(self, app, peer_author, http_mock):
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI, public_note())

        result = resolved_post(community)

        assert result.id == Post.query.filter_by(ap_id=URI).one().id
        assert result.community_id == community.id
        assert result.user_id == peer_author.id

    def test_a_none_from_create_resolved_object_is_returned_unchanged(self, app, peer_author, http_mock):
        """create_resolved_object refuses a document whose author is on a
        different host from the URI it was served at, and returns None.
        resolve_remote_post must hand that None back rather than substituting
        anything of its own. What create_resolved_object does with such a
        document is task 2's subject; what is pinned here is only that its
        answer is not altered on the way out.
        """
        community = make_community('news', host=PEER_OBJECT_HOST)
        serve_remote_object(http_mock, URI,
                            public_note('https://elsewhere.example/users/mallory'))

        assert resolved_post(community) is None
        assert Post.query.filter_by(ap_id=URI).count() == 0
