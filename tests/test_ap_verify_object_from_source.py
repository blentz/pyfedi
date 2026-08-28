"""verify_object_from_source (app/activitypub/util.py) is the impersonation
defence for Announce-wrapped objects. A peer sends an activity naming an
`object` URI; the function decides whether to believe that object really
belongs to the peer named in `actor`, and on success REPLACES
request_json['object'] with the document it fetched.

IT RETURNS A PAIR: `(request_json, None)` on success, `(None, reason)` on
every refusal. The reason is a short string naming which check refused, and
it exists because there are ten distinct refusal paths and the single caller
(app/activitypub/routes.py) used to log the same sentence for all of them --
an operator reading the incoming-activity log could not tell a malformed URI
from an impersonation attempt from a peer that was simply unreachable. Every
refusal test below therefore asserts WHICH reason came back, not merely that
something was refused; a reason no test observes is a refusal path no test
covers.

TWO SEPARATE GUARDS, not one. The function compares hosts twice, around
the fetch:

  guard 1 (pre-fetch)  the object URI's host vs the actor's host
  guard 2 (post-fetch) the object URI's host vs the fetched document's
                       attributedTo host

A test that fails guard 1 returns before the fetch and so never reaches
guard 2, which means one "mismatched host" test cannot prove both work.
Each guard gets its own refusal case AND its own acceptance case below, in
TestPreFetchHostComparison and TestPostFetchHostComparison
respectively; TestSuccessfulVerification is the shared acceptance case that
both guards must pass through. The two guards now also return DIFFERENT
reasons, which is what lets a test say which of them refused.

PROVING THE PRE-FETCH REFUSALS DO NOT FETCH. The guard-1 refusal tests do
not request the `http_mock` fixture at all, and register no route anywhere.
The session-scoped autouse `block_outbound_http` router (tests/conftest.py)
is still active, and it registers zero routes, so any outbound httpx
request raises respx's AllMockedAssertionError. That error is neither
httpx.HTTPError nor any of the five exception types app.utils.get_request
handles -- its clauses are httpx.InvalidURL, ValueError, httpx.ReadError,
httpx.HTTPError and httpx.StreamError -- so it is normalised
by nothing in app.utils.get_request and caught by nothing in
verify_object_from_source -- the function's two `except JSONDecodeError`
clauses wrap `object_request.json()`, not the network call -- so it
propagates and the test ERRORS. "No fetch happened" is therefore proved by
the absence of a route plus a passing test, not by interrogating a mock.

HOSTS RATHER THAN AUTHORITIES. Both guards go through `host_of`, which
reads `urlparse(...).hostname`: userinfo and port stripped, the remainder
lowercased. The authority string they used to compare instead
(`urlparse(...).netloc`) carries both, and the port is where that bit: a
peer inconsistent about the port between `actor`, `object` and
`attributedTo` names the same host every time and was falsely REFUSED, which
in this function means the announced content was dropped rather than merely
one activity declined. TestHostRatherThanAuthority is where that acceptance
is now pinned, at each guard separately. Userinfo never was a hole in the
other direction -- `hostname` is a pure function of `netloc`'s own string,
so two EQUAL authorities can never yield different hosts -- and the test
that records this keeps refusing under either reading.

THE EMPTY HOST. `host_of` returns '' for a URL with no host and for one
urlparse refuses outright, and '' == '' is true, so an empty host must never
be allowed to satisfy a comparison. It cannot here: the `not uri_domain`
guard returns before either comparison runs, so the object URI's host is
non-empty at both of them and an empty host on the other side can only ever
compare unequal. That is a claim about behaviour, so it is tested at both
guards -- see the two "will not parse" tests. Under the previous authority
comparison those two inputs raised ValueError out of the function instead.

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

test_a_404_refuses goes the other way and calls neither, so no Site row
exists in it at all. That is the point: a `status_code == 401` mutation
misrouting its 404 into the signed branch crashes on None.private_key
instead of passing quietly. "No Site row exists" and "the Site row carries
private_key=None" are statements about different tests, not a contradiction.

THE TWO JSON PARSES. The `.json()` calls in the 200 branch and the 401
branch each have their own `except JSONDecodeError`. Both copies are
covered separately below, because a duplicated construct with only one copy
tested lets a mutation on the untested copy survive silently -- and because
the two now return different reasons, each test can say which copy ran.
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

# A URL urlparse refuses: an opening IPv6 bracket with no closing one makes
# urlsplit raise ValueError, which host_of turns into ''.
UNPARSEABLE = 'https://[oops/u/mallory'

NO_HOST = 'the object URI has no host'
ACTOR_MISMATCH = 'the announcing actor is on a different host than the object URI'
FETCH_FAILED = 'the object could not be fetched'
NOT_JSON = 'the object response was not JSON'
SIGNED_FETCH_FAILED = 'the object could not be fetched with a signed request'
SIGNED_NOT_JSON = 'the signed object response was not JSON'
MISSING_KEYS = 'the fetched object has no id, type or attributedTo'
UNUSABLE_ATTRIBUTED_TO = 'the fetched object has an attributedTo of an unusable type'
ATTRIBUTED_ELSEWHERE = 'the fetched object is attributed to a different host than its URI'

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
        dropping the `request_json['object'] = object` assignment, or
        returning a reason alongside a successful object."""
        http_mock.get(URI).respond(200, json=note_document())
        request_json = announce_activity()

        result, reason = verify_object_from_source(request_json)

        assert result is request_json
        assert reason is None
        assert result['object'] == note_document()
        assert result['actor'] == ACTOR


class TestPreFetchHostComparison:
    """Guard 1, which runs BEFORE the fetch. No test here requests
    `http_mock` or registers any route, so a fetch would raise out of
    get_request and error the test -- see the module docstring's PROVING THE
    PRE-FETCH REFUSALS DO NOT FETCH paragraph.
    """

    def test_an_object_uri_with_no_host_refuses_without_fetching(self, app, db_session):
        """`host_of('objects/1')` is '', which the `if not uri_domain` guard
        refuses outright. Production change that fails this: deleting that
        guard (the function would then fetch a schemeless URI) or inverting
        it to `if uri_domain`."""
        request_json = announce_activity(object_uri='objects/1')

        assert verify_object_from_source(request_json) == (None, NO_HOST)
        assert request_json['object'] == 'objects/1'

    def test_an_object_uri_that_will_not_parse_refuses_without_fetching(self, app, db_session):
        """host_of degrades urlparse's ValueError to '', so an object URI
        urlparse refuses reaches the same guard as one with no host at all,
        rather than raising out of the function as it did when the guard read
        `netloc` directly."""
        request_json = announce_activity(object_uri=UNPARSEABLE)

        assert verify_object_from_source(request_json) == (None, NO_HOST)
        assert request_json['object'] == UNPARSEABLE

    def test_an_actor_on_a_different_host_refuses_without_fetching(self, app, db_session):
        """Production change that fails this: deleting `if create_domain !=
        uri_domain`, or weakening it to compare something both sides share.
        The presence half of this pair is TestSuccessfulVerification, which
        the inverted (`==`) mutation fails."""
        request_json = announce_activity(actor=f'https://{OTHER_HOST}/u/mallory')

        assert verify_object_from_source(request_json) == (None, ACTOR_MISMATCH)
        assert request_json['object'] == URI

    def test_an_actor_that_will_not_parse_refuses_without_fetching(self, app, db_session):
        """The empty-host obligation at guard 1: host_of returns '' for an
        actor urlparse refuses, and '' must not be allowed to satisfy the
        comparison. It cannot, because the guard above has already proved the
        object URI's host non-empty -- this test is what would fail if that
        ordering were ever undone, and it also pins that the ValueError is
        absorbed rather than propagated."""
        request_json = announce_activity(actor=UNPARSEABLE)

        assert verify_object_from_source(request_json) == (None, ACTOR_MISMATCH)
        assert request_json['object'] == URI


class TestPostFetchHostComparison:
    """Guard 2, which runs AFTER the fetch and compares the object URI's
    host against the fetched document's attributedTo. Reaching it at all
    requires guard 1 to pass, which is why these tests keep actor and
    object on the same host and vary only the FETCHED document.
    """

    def test_a_document_attributed_to_a_different_host_refuses(self, app, db_session, http_mock):
        """The impersonation case this guard exists for: the peer names an
        object URI on its own host, but the document that URI actually
        serves claims to belong to someone else. Production change that
        fails this: deleting `if uri_domain != actor_domain`. Its presence
        half is TestSuccessfulVerification, which the inverted (`==`)
        mutation fails."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=f'https://{OTHER_HOST}/u/mallory'))
        request_json = announce_activity()

        assert verify_object_from_source(request_json) == (None, ATTRIBUTED_ELSEWHERE)
        assert request_json['object'] == URI

    def test_a_document_with_an_empty_attributed_to_string_refuses(self, app, db_session, http_mock):
        """`host_of('')` is '', which cannot equal a real uri_domain.
        Distinct from the case above because it exercises the comparison
        against '' rather than against a rival host."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=''))
        request_json = announce_activity()

        assert verify_object_from_source(request_json) == (None, ATTRIBUTED_ELSEWHERE)

    def test_a_document_whose_attributed_to_will_not_parse_refuses(self, app, db_session, http_mock):
        """The empty-host obligation at guard 2, and the mirror of
        test_an_actor_that_will_not_parse_refuses_without_fetching: host_of
        absorbs urlparse's ValueError into '', which the comparison must
        refuse rather than let match another ''."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=UNPARSEABLE))
        request_json = announce_activity()

        assert verify_object_from_source(request_json) == (None, ATTRIBUTED_ELSEWHERE)


class TestRequiredKeys:
    """`if not 'id' in object or not 'type' in object or not 'attributedTo'
    in object` -- three operands, one test each, so dropping any single
    operand is caught. All three share one reason, because an operator's
    next step is the same whichever key is missing.
    """

    def test_a_document_without_id_refuses(self, app, db_session, http_mock):
        document = note_document()
        del document['id']
        http_mock.get(URI).respond(200, json=document)
        request_json = announce_activity()

        assert verify_object_from_source(request_json) == (None, MISSING_KEYS)
        assert request_json['object'] == URI

    def test_a_document_without_type_refuses(self, app, db_session, http_mock):
        document = note_document()
        del document['type']
        http_mock.get(URI).respond(200, json=document)

        assert verify_object_from_source(announce_activity()) == (None, MISSING_KEYS)

    def test_a_document_without_attributed_to_refuses(self, app, db_session, http_mock):
        document = note_document()
        del document['attributedTo']
        http_mock.get(URI).respond(200, json=document)

        assert verify_object_from_source(announce_activity()) == (None, MISSING_KEYS)


class TestAttributedToShapes:
    """The four shapes attributedTo is destructured into before guard 2
    compares it: a string, a dict carrying 'id', a list (first string, or
    first dict whose type is 'Person'), and anything else -- which falls to
    the chain's `else`, the only refusal here that is not guard 2's.
    """

    def test_a_dict_with_an_id_matches_on_that_ids_host(self, app, db_session, http_mock):
        """Production change that fails this: dropping the `isinstance(...,
        dict) and 'id' in ...` branch, which would send this input to the
        `else`."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to={'type': 'Person', 'id': ACTOR}))
        request_json = announce_activity()

        result, reason = verify_object_from_source(request_json)

        assert result is request_json
        assert reason is None

    def test_a_dict_with_an_id_on_another_host_refuses(self, app, db_session, http_mock):
        """The dict branch's refusal half: the branch must read the host out
        of 'id', not merely accept any dict. The reason proves it reached
        guard 2 rather than the shape chain's `else`."""
        http_mock.get(URI).respond(
            200, json=note_document(attributed_to={'type': 'Person', 'id': f'https://{OTHER_HOST}/u/mallory'}))

        assert verify_object_from_source(announce_activity()) == (None, ATTRIBUTED_ELSEWHERE)

    def test_a_dict_without_an_id_refuses_as_an_unusable_shape(self, app, db_session, http_mock):
        """No 'id' key means the dict branch's second operand is False, so
        the chain falls through the list branch to the `else` -- which the
        reason distinguishes from a guard-2 refusal. Production change that
        fails this: dropping the `'id' in ...` operand, which would then
        raise KeyError rather than refuse."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to={'type': 'Person', 'name': 'alice'}))

        assert verify_object_from_source(announce_activity()) == (None, UNUSABLE_ATTRIBUTED_TO)

    def test_a_list_matches_on_the_first_string(self, app, db_session, http_mock):
        """The loop breaks on the first string, so a later entry on a rival
        host is never consulted. Production change that fails this:
        removing the `break` after the string branch, which would let the
        last entry win instead of the first."""
        http_mock.get(URI).respond(
            200, json=note_document(attributed_to=[ACTOR, f'https://{OTHER_HOST}/u/mallory']))
        request_json = announce_activity()

        result, reason = verify_object_from_source(request_json)

        assert result is request_json
        assert reason is None

    def test_a_list_whose_first_string_is_on_another_host_refuses(self, app, db_session, http_mock):
        """The mirror of the test above, and the reason it is worth having
        both: first-wins is only proved by showing that a matching entry
        LATER in the list does not rescue a mismatching first one."""
        http_mock.get(URI).respond(
            200, json=note_document(attributed_to=[f'https://{OTHER_HOST}/u/mallory', ACTOR]))

        assert verify_object_from_source(announce_activity()) == (None, ATTRIBUTED_ELSEWHERE)

    def test_a_list_matches_on_the_first_person_dicts_id(self, app, db_session, http_mock):
        """A non-Person dict matches neither loop branch, so iteration
        continues past it to the Person dict. Production change that fails
        this: dropping the `a.get('type') == 'Person'` test, which would
        take the Group's id (a rival host) instead."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=[
            {'type': 'Group', 'id': f'https://{OTHER_HOST}/c/news'},
            {'type': 'Person', 'id': ACTOR},
        ]))
        request_json = announce_activity()

        result, reason = verify_object_from_source(request_json)

        assert result is request_json
        assert reason is None

    def test_a_person_dict_whose_id_is_not_a_string_refuses(self, app, db_session, http_mock):
        """The loop breaks on the first Person dict whether or not its 'id'
        is usable, leaving actor_domain at '' -- so a Person entry with a
        non-string id refuses at guard 2, and does NOT fall through to the
        usable entry behind it."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=[
            {'type': 'Person', 'id': 12345},
            ACTOR,
        ]))

        assert verify_object_from_source(announce_activity()) == (None, ATTRIBUTED_ELSEWHERE)

    def test_an_empty_list_refuses(self, app, db_session, http_mock):
        """The loop body never runs, so actor_domain stays '' and guard 2
        refuses. Production change that fails this: initialising
        actor_domain to uri_domain rather than ''."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=[]))

        assert verify_object_from_source(announce_activity()) == (None, ATTRIBUTED_ELSEWHERE)

    def test_an_integer_attributed_to_refuses_as_an_unusable_shape(self, app, db_session, http_mock):
        """The fourth shape -- anything that is not a str, an id-carrying
        dict, or a list reaches the if/elif chain's `else`. The reason is
        what separates it from the shapes above that produce '' and refuse
        at guard 2 instead; deleting the `else` would let this input reach
        guard 2 and come back with the other reason."""
        http_mock.get(URI).respond(200, json=note_document(attributed_to=12345))

        assert verify_object_from_source(announce_activity()) == (None, UNUSABLE_ATTRIBUTED_TO)


class TestHostRatherThanAuthority:
    """Both guards compare `host_of(...)`, which is `urlparse(...).hostname`,
    rather than the authority string `urlparse(...).netloc` they compared
    before. The authority carries userinfo and a port; neither identifies the
    host.

    The port is where that bit. https://remote.example:8443/x and
    https://remote.example/x are the same host but different authority
    STRINGS, so a peer INCONSISTENT about the port between `actor`, `object`
    and `attributedTo` was falsely REFUSED -- and because this function gates
    whether a fetched object is believed at all, the refusal dropped the
    announced content rather than merely declining one activity. Both guards
    had the defect independently, so both acceptances are pinned.

    Userinfo never ran the other way: `hostname` is a pure function of
    `netloc`'s own string, so two EQUAL authorities can never yield different
    hosts, and controlling both strings still cannot make two different real
    hosts compare equal. The last test records that, and refuses under either
    reading.
    """

    def test_a_port_only_on_the_object_uri_is_accepted(self, app, db_session, http_mock):
        """actor host 'remote.example', object authority
        'remote.example:8443' -- the same host, and now accepted. Under the
        authority comparison this refused before any fetch, which is why it
        needed no route; it needs one now, and that route being HIT is the
        behaviour change."""
        uri = f'https://{HOST}:8443/objects/1'
        http_mock.get(uri).respond(200, json=note_document(uri=uri))
        request_json = announce_activity(object_uri=uri)

        result, reason = verify_object_from_source(request_json)

        assert result is request_json
        assert reason is None
        assert request_json['object'] == note_document(uri=uri)

    def test_a_port_absent_only_from_attributed_to_is_accepted(self, app, db_session, http_mock):
        """The same fix at guard 2, which needs its own case because guard 1
        returns before guard 2 is reached. actor and object agree on
        authority 'remote.example:8443'; the document is attributed to the
        same host without the port, so the authority comparison refused and
        the host comparison accepts."""
        uri = f'https://{HOST}:8443/objects/1'
        http_mock.get(uri).respond(200, json=note_document(attributed_to=ACTOR, uri=uri))
        request_json = announce_activity(object_uri=uri, actor=f'https://{HOST}:8443/u/alice')

        result, reason = verify_object_from_source(request_json)

        assert result is request_json
        assert reason is None
        assert request_json['object'] == note_document(attributed_to=ACTOR, uri=uri)

    def test_userinfo_naming_the_peer_does_not_make_a_rival_host_pass(self, app, db_session):
        """`https://remote.example@attacker.example/objects/1` has authority
        'remote.example@attacker.example' and host 'attacker.example'. Both
        readings refuse it against actor host 'remote.example' -- recorded to
        show that the userinfo form, which LOOKS like an impersonation
        vector, is not one, and that reading the host rather than the
        authority did not open one."""
        request_json = announce_activity(object_uri=f'https://{HOST}@{OTHER_HOST}/objects/1')

        assert verify_object_from_source(request_json) == (None, ACTOR_MISMATCH)


class TestFetchOutcomes:
    """The fetch itself, between the two guards. Every one of these is a
    refusal, so each also confirms request_json['object'] is left as the
    bare URI -- an unfetched object is never treated as verified -- and each
    names a different reason, which is what tells an operator whether the
    peer was unreachable, answered with something that was not JSON, or
    answered with a status this function does not handle.
    """

    def test_an_unparseable_200_body_refuses(self, app, db_session, http_mock):
        """The 200 branch's `except JSONDecodeError` around `.json()`.
        Production change that fails this: removing that handler, which
        would raise instead of refusing, or narrowing it to an exception
        httpx's json() does not raise."""
        http_mock.get(URI).respond(200, content=b'not json', headers={'content-type': 'application/json'})
        request_json = announce_activity()

        assert verify_object_from_source(request_json) == (None, NOT_JSON)
        assert request_json['object'] == URI

    def test_a_404_refuses_and_names_the_status(self, app, db_session, http_mock):
        """No Site row exists in this test, so a `status_code == 401`
        mutation that routed this response into the signed branch would
        crash on Site.query.get(1) being None rather than pass quietly. The
        status is carried in the reason because it is the one thing an
        operator needs and the response itself is not kept."""
        http_mock.get(URI).respond(404, json=note_document())

        assert verify_object_from_source(announce_activity()) == (None, 'the object fetch returned HTTP 404')

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

        result, reason = verify_object_from_source(request_json)

        assert result is request_json
        assert reason is None
        assert request_json['object'] == signed_document

    def test_an_unparseable_signed_body_refuses(self, app, db_session, http_mock):
        """The 401 branch's OWN `except JSONDecodeError` around `.json()` --
        a second copy of the construct covered by
        test_an_unparseable_200_body_refuses, tested separately so a mutation
        on either copy is caught. The two reasons differ, so this test also
        proves which copy ran."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.Response(200, content=b'not json', headers={'content-type': 'application/json'}),
        ])

        assert verify_object_from_source(announce_activity()) == (None, SIGNED_NOT_JSON)

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

        result, reason = verify_object_from_source(request_json)

        assert result is request_json
        assert reason is None
        assert request_json['object'] == note_document()

    def test_both_signed_fetch_attempts_failing_refuses(self, app, db_session, http_mock):
        """Production change that fails this: the 401 branch's inner `except
        httpx.HTTPError` re-raising or refusing with the plain path's reason
        -- a second copy of the handler covered by
        test_four_transport_failures_refuses, tested separately, and the two
        reasons are what keep the copies distinguishable."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
        ])
        request_json = announce_activity()

        assert verify_object_from_source(request_json) == (None, SIGNED_FETCH_FAILED)
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

        result, reason = verify_object_from_source(request_json)

        assert result is request_json
        assert reason is None
        assert request_json['object'] == note_document()

    def test_four_transport_failures_refuses(self, app, db_session, http_mock):
        """Production change that fails this: the inner `except
        httpx.HTTPError` re-raising or refusing with the signed path's
        reason."""
        http_mock.get(URI).mock(side_effect=[httpx.ConnectError('boom')] * 4)
        request_json = announce_activity()

        assert verify_object_from_source(request_json) == (None, FETCH_FAILED)
        assert request_json['object'] == URI
