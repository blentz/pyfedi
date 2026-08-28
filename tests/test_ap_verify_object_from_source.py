"""verify_object_from_source (app/activitypub/util.py) is the impersonation
defence for Announce-wrapped objects. A peer sends an activity naming an
`object` URI; the function decides whether to believe that object really
belongs to the peer named in `actor`, and on success REPLACES
request_json['object'] with the document it fetched, returning the same
(mutated) request_json. On every refusal it returns None and leaves
request_json['object'] as the bare URI string.

TWO SEPARATE GUARDS, not one. The function compares domains twice, around
the fetch:

  guard 1 (pre-fetch)  the object URI's domain vs the actor's domain
  guard 2 (post-fetch) the object URI's domain vs the fetched document's
                       attributedTo domain

A test that fails guard 1 returns before the fetch and so never reaches
guard 2, which means one "mismatched domain" test cannot prove both work.
Each guard gets its own refusal case AND its own acceptance case below, in
TestPreFetchDomainComparison and TestPostFetchDomainComparison
respectively; TestSuccessfulVerification is the shared acceptance case that
both guards must pass through.

PROVING THE PRE-FETCH REFUSALS DO NOT FETCH. The guard-1 refusal tests do
not request the `http_mock` fixture at all, and register no route anywhere.
The session-scoped autouse `block_outbound_http` router (tests/conftest.py)
is still active, and it registers zero routes, so any outbound httpx
request raises respx's AllMockedAssertionError. That error is neither
httpx.HTTPError nor any of the five exception types app.utils.get_request
handles -- its clauses are httpx.InvalidURL, ValueError, httpx.ReadError,
httpx.HTTPError and httpx.StreamError -- so it is normalised
by nothing in app.utils.get_request and caught by nothing in
verify_object_from_source -- the function's only bare `except:` wraps
`object_request.json()`, not the network call -- so it propagates and the
test ERRORS. "No fetch happened" is therefore proved by the absence of a
route plus a passing test, not by interrogating a mock.

THE netloc-VERSUS-hostname BEHAVIOUR. Both guards compare
`urlparse(...).netloc`, never `.hostname`. TestNetlocRatherThanHostname
pins the current behaviour and records it as a suspected defect (it is not
fixed here). `hostname` is a pure function of `netloc`'s own string, so two
EQUAL netlocs can never yield different hostnames: userinfo
(https://peer.example@attacker.example/x) cannot make two different real
hosts compare equal, and this is not an impersonation hole.

Does a peer control both strings? YES -- a peer authors `actor`, `object`
and the fetched document's `attributedTo` alike, so it supplies every input
to both comparisons. That is what makes the question worth asking, and
`hostname` being a pure function of `netloc` is what makes the answer
harmless: controlling both strings still cannot make two different real
hosts compare equal.

The genuine divergence is the PORT: https://peer.example:8443/x and
https://peer.example/x are the same host by `hostname` but different
strings by `netloc`, so a peer that is INCONSISTENT about the port between
`actor`, `object` and `attributedTo` is falsely REFUSED. A peer that
carries the same port on all three compares equal and passes -- the
false reject needs the inconsistency, which is what the tests below
construct. That is an availability defect, and it bites at both guards
independently -- so both are pinned.

TIMING. app/utils.py carries both `import time` and `from time import
sleep`, and get_request runs its OWN retry through that second binding,
nested inside this function's `time.sleep(3)` retry. Patching only
`time.sleep` would leave the inner one sleeping for real, so
the shared `no_real_sleeping` fixture in conftest patches both,
function-scoped, and this module opts into it by pytestmark. Neither patch
touches get_request or verify_object_from_source, so no test here asserts
retry TIMING -- only that a second attempt happens and that its outcome is
what comes back.

THE Site ROW. Nothing seeds one automatically: `db_session` truncates, and a
Site row exists in a test only if that test calls make_site()
(tests/factories.py) itself. Two consequences pull in opposite directions
and are both deliberate here.

The 401 branch reads Site.query.get(1).private_key, so a test that reaches
it must create the row -- and make_site()'s row carries private_key=None,
which fails as a PEM key before any network call. Hence
`seed_signing_site` (tests/factories.py), which calls make_site() AND
assigns a real keypair. tests/test_ap_remote_object_to_json.py covers the
identical fetch-and-retry structure in a sibling function and needs exactly
the same row, which is why that helper is shared rather than restated here.

test_a_404_returns_none goes the other way and calls neither, so no Site row
exists in it at all. That is the point: a `status_code == 401` mutation
misrouting its 404 into the signed branch crashes on None.private_key
instead of passing quietly. "No Site row exists" and "the Site row carries
private_key=None" are statements about different tests, not a contradiction.

REPORTED, NOT FIXED. The `.json()` calls in both the 200 and the 401 branch
are wrapped in a bare `except:`. Structurally it encloses only the parse,
not the fetch, so it cannot swallow respx's AllMockedAssertionError the way
sub-project 1c's fixup_url hazard did -- but it is still a bare except and
is left in place per this task's "report defects; do not fix them"
instruction. Both copies are covered separately below, because a
duplicated construct with only one copy tested lets a mutation on the
untested copy survive silently.
"""
import httpx
import pytest

from app.activitypub.util import verify_object_from_source
from tests.factories import (PEER_ACTOR_URI, PEER_OBJECT_HOST, PEER_OBJECT_URI,
                             announce_activity, note_document, seed_signing_site)

HOST = PEER_OBJECT_HOST
ACTOR = PEER_ACTOR_URI
URI = PEER_OBJECT_URI
OTHER_HOST = 'attacker.example'

# Both sleep call sites reachable from this function are neutralised by the
# shared no_real_sleeping fixture -- see the module docstring's TIMING
# paragraph for why both are needed.
pytestmark = pytest.mark.usefixtures('no_real_sleeping')


class TestSuccessfulVerification:
    """The acceptance case both guards must let through. This is guard 1's
    presence case AND guard 2's presence case: inverting either comparison
    fails this test.
    """

    def test_a_same_host_object_attributed_to_that_host_replaces_the_object(self, app, db_session, http_mock):
        """Production change that fails this: inverting either `!=` to `==`,
        or dropping the `request_json['object'] = object` assignment."""
        http_mock.get(URI).respond(200, json=note_document())
        request_json = announce_activity()

        result = verify_object_from_source(request_json)

        assert result is request_json
        assert result['object'] == note_document()
        assert result['actor'] == ACTOR


class TestPreFetchDomainComparison:
    """Guard 1, which runs BEFORE the fetch. Neither test requests
    `http_mock` or registers any route, so a fetch would raise out of
    get_request and error the test -- see the module docstring's PROVING THE
    PRE-FETCH REFUSALS DO NOT FETCH paragraph.
    """

    def test_an_object_uri_with_no_domain_returns_none_without_fetching(self, app, db_session):
        """`urlparse('objects/1').netloc` is '', which the `if not
        uri_domain` guard refuses outright. Production change that fails
        this: deleting that guard (the function would then fetch a
        schemeless URI) or inverting it to `if uri_domain`."""
        request_json = announce_activity(object_uri='objects/1')

        assert verify_object_from_source(request_json) is None
        assert request_json['object'] == 'objects/1'

    def test_an_actor_on_a_different_host_returns_none_without_fetching(self, app, db_session):
        """Production change that fails this: deleting `if create_domain !=
        uri_domain: return None`, or weakening it to compare something both
        sides share. The presence half of this pair is
        TestSuccessfulVerification, which the inverted (`==`) mutation
        fails."""
        request_json = announce_activity(actor=f'https://{OTHER_HOST}/u/mallory')

        assert verify_object_from_source(request_json) is None
        assert request_json['object'] == URI


class TestPostFetchDomainComparison:
    """Guard 2, which runs AFTER the fetch and compares the object URI's
    domain against the fetched document's attributedTo. Reaching it at all
    requires guard 1 to pass, which is why these tests keep actor and
    object on the same host and vary only the FETCHED document.
    """

    def test_a_document_attributed_to_a_different_host_returns_none(self, app, db_session, http_mock):
        """The impersonation case this guard exists for: the peer names an
        object URI on its own host, but the document that URI actually
        serves claims to belong to someone else. Production change that
        fails this: deleting `if uri_domain != actor_domain: return None`.
        Its presence half is TestSuccessfulVerification, which the inverted
        (`==`) mutation fails."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=f'https://{OTHER_HOST}/u/mallory'))
        request_json = announce_activity()

        assert verify_object_from_source(request_json) is None
        assert request_json['object'] == URI

    def test_a_document_with_an_empty_attributed_to_string_returns_none(self, app, db_session, http_mock):
        """`urlparse('').netloc` is '', which cannot equal a real
        uri_domain. Distinct from the case above because it exercises the
        comparison against the '' that `actor_domain` is initialised to,
        rather than against a rival host."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=''))
        request_json = announce_activity()

        assert verify_object_from_source(request_json) is None


class TestRequiredKeys:
    """`if not 'id' in object or not 'type' in object or not 'attributedTo'
    in object` -- three operands, one test each, so dropping any single
    operand is caught.
    """

    def test_a_document_without_id_returns_none(self, app, db_session, http_mock):
        document = note_document()
        del document['id']
        http_mock.get(URI).respond(200, json=document)
        request_json = announce_activity()

        assert verify_object_from_source(request_json) is None
        assert request_json['object'] == URI

    def test_a_document_without_type_returns_none(self, app, db_session, http_mock):
        document = note_document()
        del document['type']
        http_mock.get(URI).respond(200, json=document)

        assert verify_object_from_source(announce_activity()) is None

    def test_a_document_without_attributed_to_returns_none(self, app, db_session, http_mock):
        document = note_document()
        del document['attributedTo']
        http_mock.get(URI).respond(200, json=document)

        assert verify_object_from_source(announce_activity()) is None


class TestAttributedToShapes:
    """The four shapes attributedTo is destructured into before guard 2
    compares it: a string, a dict carrying 'id', a list (first string, or
    first dict whose type is 'Person'), and anything else -- which falls to
    the chain's `else: return None`.
    """

    def test_a_dict_with_an_id_matches_on_that_ids_host(self, app, db_session, http_mock):
        """Production change that fails this: dropping the `isinstance(...,
        dict) and 'id' in ...` branch, which would send this input to the
        `else: return None`."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to={'type': 'Person', 'id': ACTOR}))

        assert verify_object_from_source(announce_activity()) is not None

    def test_a_dict_with_an_id_on_another_host_returns_none(self, app, db_session, http_mock):
        """The dict branch's refusal half: the branch must read the host out
        of 'id', not merely accept any dict."""
        http_mock.get(URI).respond(
            200, json=note_document(attributed_to={'type': 'Person', 'id': f'https://{OTHER_HOST}/u/mallory'}))

        assert verify_object_from_source(announce_activity()) is None

    def test_a_dict_without_an_id_returns_none(self, app, db_session, http_mock):
        """No 'id' key means the dict branch's second operand is False, so
        the chain falls through the list branch to `else: return None`.
        Production change that fails this: dropping the `'id' in ...`
        operand, which would then raise KeyError rather than refuse."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to={'type': 'Person', 'name': 'alice'}))

        assert verify_object_from_source(announce_activity()) is None

    def test_a_list_matches_on_the_first_string(self, app, db_session, http_mock):
        """The loop breaks on the first string, so a later entry on a rival
        host is never consulted. Production change that fails this:
        removing the `break` after the string branch, which would let the
        last entry win instead of the first."""
        http_mock.get(URI).respond(
            200, json=note_document(attributed_to=[ACTOR, f'https://{OTHER_HOST}/u/mallory']))

        assert verify_object_from_source(announce_activity()) is not None

    def test_a_list_whose_first_string_is_on_another_host_returns_none(self, app, db_session, http_mock):
        """The mirror of the test above, and the reason it is worth having
        both: first-wins is only proved by showing that a matching entry
        LATER in the list does not rescue a mismatching first one."""
        http_mock.get(URI).respond(
            200, json=note_document(attributed_to=[f'https://{OTHER_HOST}/u/mallory', ACTOR]))

        assert verify_object_from_source(announce_activity()) is None

    def test_a_list_matches_on_the_first_person_dicts_id(self, app, db_session, http_mock):
        """A non-Person dict matches neither loop branch, so iteration
        continues past it to the Person dict. Production change that fails
        this: dropping the `a.get('type') == 'Person'` test, which would
        take the Group's id (a rival host) instead."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=[
            {'type': 'Group', 'id': f'https://{OTHER_HOST}/c/news'},
            {'type': 'Person', 'id': ACTOR},
        ]))

        assert verify_object_from_source(announce_activity()) is not None

    def test_a_person_dict_whose_id_is_not_a_string_returns_none(self, app, db_session, http_mock):
        """The loop breaks on the first Person dict whether or not its 'id'
        is usable, leaving actor_domain at '' -- so a Person entry with a
        non-string id refuses, and does NOT fall through to the usable
        entry behind it."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=[
            {'type': 'Person', 'id': 12345},
            ACTOR,
        ]))

        assert verify_object_from_source(announce_activity()) is None

    def test_an_empty_list_returns_none(self, app, db_session, http_mock):
        """The loop body never runs, so actor_domain stays '' and guard 2
        refuses. Production change that fails this: initialising
        actor_domain to uri_domain rather than ''."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=[]))

        assert verify_object_from_source(announce_activity()) is None

    def test_an_integer_attributed_to_returns_none(self, app, db_session, http_mock):
        """The fourth shape -- anything that is not a str, an id-carrying
        dict, or a list reaches the if/elif chain's `else: return None`.
        Production change that fails this: deleting that `else`, which
        would let actor_domain stay '' and reach guard 2 instead (still
        None here, but by a different route -- which is why the shapes above
        that DO produce '' are tested separately)."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=12345))

        assert verify_object_from_source(announce_activity()) is None


class TestNetlocRatherThanHostname:
    """Both guards compare `urlparse(...).netloc`, not `.hostname`. These
    tests PIN CURRENT BEHAVIOUR and record a SUSPECTED DEFECT; they do not
    endorse it and nothing is fixed here.

    `hostname` is derived purely from `netloc`'s own string (userinfo and
    port stripped, then lowercased), so two inputs with an equal netloc can
    never have different hostnames -- userinfo cannot make two DIFFERENT
    real hosts compare equal, and this is not an impersonation hole. The
    real divergence is the PORT, and it runs the other way: the same host on
    a non-default port is a different netloc STRING, so a legitimate peer is
    falsely refused. That is availability, not impersonation, and because
    this function gates whether a fetched object is believed at all, the
    refusal drops the announced content rather than merely declining one
    activity.
    """

    def test_a_port_on_the_object_uri_makes_the_pre_fetch_guard_refuse(self, app, db_session):
        """actor netloc 'remote.example', object netloc
        'remote.example:8443' -- the SAME host by `hostname`, refused by
        `netloc`. No route is registered, so this also shows the refusal
        happens before any fetch."""
        request_json = announce_activity(object_uri=f'https://{HOST}:8443/objects/1')

        assert verify_object_from_source(request_json) is None

    def test_a_port_only_on_attributed_to_makes_the_post_fetch_guard_refuse(self, app, db_session, http_mock):
        """The same suspected defect at guard 2, which needs its own case
        because guard 1 returns before guard 2 is reached. actor and object
        agree on netloc 'remote.example:8443'; the document is attributed to
        the same host without the port, so `hostname` would accept and
        `netloc` refuses."""
        uri = f'https://{HOST}:8443/objects/1'
        http_mock.get(uri).respond(200, json=note_document(attributed_to=ACTOR, uri=uri))
        request_json = announce_activity(object_uri=uri, actor=f'https://{HOST}:8443/u/alice')

        assert verify_object_from_source(request_json) is None

    def test_userinfo_naming_the_peer_does_not_make_a_rival_host_pass(self, app, db_session):
        """`https://remote.example@attacker.example/objects/1` has netloc
        'remote.example@attacker.example' and hostname 'attacker.example'.
        Both a netloc comparison and a hostname comparison refuse it against
        actor netloc 'remote.example' -- recorded to show that the userinfo
        form, which LOOKS like an impersonation vector, is not one here."""
        request_json = announce_activity(object_uri=f'https://{HOST}@{OTHER_HOST}/objects/1')

        assert verify_object_from_source(request_json) is None


class TestFetchOutcomes:
    """The fetch itself, between the two guards. Every one of these is a
    refusal, so each also confirms request_json['object'] is left as the
    bare URI -- an unfetched object is never treated as verified.
    """

    def test_an_unparseable_200_body_returns_none(self, app, db_session, http_mock):
        """The 200 branch's bare `except:` around `.json()`. Production
        change that fails this: removing that handler, which would raise
        instead of returning None. Reported, not narrowed."""
        http_mock.get(URI).respond(200, content=b'not json', headers={'content-type': 'application/json'})
        request_json = announce_activity()

        assert verify_object_from_source(request_json) is None
        assert request_json['object'] == URI

    def test_a_404_returns_none(self, app, db_session, http_mock):
        """No Site row exists in this test, so a `status_code == 401`
        mutation that routed this response into the signed branch would
        crash on Site.query.get(1) being None rather than pass quietly."""
        http_mock.get(URI).respond(404, json=note_document())

        assert verify_object_from_source(announce_activity()) is None

    def test_a_401_retries_with_a_signed_request_and_uses_its_body(self, app, db_session, http_mock):
        """respx matches by (method, url), so both fetches share one route
        with an ordered side_effect list: the first entry answers the plain
        get_request, the second the signed retry. The signed body carries a
        marker absent from anything else, so the returned object proves the
        SIGNED response is what was used."""
        seed_signing_site()
        signed_document = dict(note_document(), content='signed only')
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=signed_document),
        ])
        request_json = announce_activity()

        assert verify_object_from_source(request_json) is request_json
        assert request_json['object'] == signed_document

    def test_an_unparseable_signed_body_returns_none(self, app, db_session, http_mock):
        """The 401 branch's OWN bare `except:` around `.json()` -- a second
        copy of the construct covered by
        test_an_unparseable_200_body_returns_none, tested separately so a
        mutation on either copy is caught."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.Response(200, content=b'not json', headers={'content-type': 'application/json'}),
        ])

        assert verify_object_from_source(announce_activity()) is None

    def test_a_signed_fetch_failure_then_a_successful_retry_returns_the_fetched_object(self, app, db_session,
                                                                                       http_mock):
        """The 401 branch carries its OWN transport-failure retry, separate
        from the plain path's. signed_get_request has no internal retry of
        its own, so one failure per attempt is enough here where the plain
        path needs two."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.ConnectError('boom'),
            httpx.Response(200, json=note_document()),
        ])
        request_json = announce_activity()

        assert verify_object_from_source(request_json) is request_json
        assert request_json['object'] == note_document()

    def test_both_signed_fetch_attempts_failing_returns_none(self, app, db_session, http_mock):
        """Production change that fails this: the 401 branch's inner `except
        httpx.HTTPError: return None` re-raising or returning non-None -- a
        second copy of the handler covered by
        test_four_transport_failures_returns_none, tested separately."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
        ])
        request_json = announce_activity()

        assert verify_object_from_source(request_json) is None
        assert request_json['object'] == URI

    def test_two_transport_failures_then_a_success_returns_the_fetched_object(self, app, db_session, http_mock):
        """get_request has its own internal retry, so one
        verify_object_from_source attempt costs TWO httpx attempts before
        its `except httpx.HTTPError` sees anything -- hence two failures
        here, not one. Production change that fails this: deleting the outer
        retry, which would let the error propagate."""
        http_mock.get(URI).mock(side_effect=[
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
            httpx.Response(200, json=note_document()),
        ])
        request_json = announce_activity()

        assert verify_object_from_source(request_json) is request_json
        assert request_json['object'] == note_document()

    def test_four_transport_failures_returns_none(self, app, db_session, http_mock):
        """Production change that fails this: the inner `except
        httpx.HTTPError: return None` re-raising or returning non-None."""
        http_mock.get(URI).mock(side_effect=[httpx.ConnectError('boom')] * 4)
        request_json = announce_activity()

        assert verify_object_from_source(request_json) is None
        assert request_json['object'] == URI
