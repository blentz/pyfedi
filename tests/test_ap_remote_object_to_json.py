"""remote_object_to_json (app/activitypub/util.py) fetches a URI and returns
parsed JSON, or None. Four outcomes on the plain (unsigned) fetch:

- 200 with a body that parses as JSON -> the parsed dict.
- 200 with a body that does not parse -> None (via a bare `except:` -- see
  "The bare except" below).
- 401 -> retried with a SIGNED request (app.activitypub.signature.
  signed_get_request), using Site.query.get(1)'s private_key. Whatever that
  signed response's body parses to (or fails to) is returned the same way.
- anything else -> None.

Both fetch paths (the plain `get_request` call and the signed retry inside
the 401 branch) also carry their own transport-failure retry: on
httpx.HTTPError the function sleeps 3 seconds and tries once more before
giving up and returning None.

Timing decision (the brief's hazard #1): `time.sleep(3)` fires on every one
of those retries, and app.utils.get_request has ITS OWN internal retry (a
`random.randint(3, 10)`-second sleep, bound via `from time import sleep` in
app/utils.py) nested inside the plain fetch path -- a single "transport
error twice" case can involve two get_request calls, each of which can make
two real httpx attempts before raising, so up to four real sleeps stack
without patching. Both sleeps are patched here: `time.sleep` (module-level,
covers app.activitypub.util's `time.sleep(3)`, since `time` is a shared
module object and every `import time; time.sleep(...)` caller sees the same
patched attribute) and `app.utils.sleep` (the name `from time import sleep`
bound directly into app.utils's namespace, which `time.sleep`-patching does
NOT reach). Neither patch touches get_request or remote_object_to_json
themselves -- both are standard-library sleep, not a mock of the code under
test -- so retry TIMING is not asserted anywhere below, only retry
BEHAVIOUR (that a second attempt happens and its outcome is what is
returned).

The bare except: both the 200 branch and the 401 branch wrap `.json()` in a
bare `except:`. Sub-project 1c's tests/test_fixup_url.py documents exactly
this class of hazard in a sibling function (fixup_url's peertube branch, since
narrowed) -- a bare except swallows respx's own AllMockedAssertionError
exactly as readily as a real parse failure, so "the code took a branch it
should not have" can look identical to "the code behaved correctly", if the
wrong branch's fetch also happens to leave an unparseable body. Every test
below that exercises a wrong-branch mutation is built so the WRONG branch's
`.json()` would succeed and return an observably different dict, not raise --
see TestMutationStatus200 and TestMutationStatus401's classes for the
mutation results this produced. Reported, not narrowed, per the task's
"report defects; do not fix them" instruction.

Site's row in this suite (via make_site(), tests/factories.py) carries
private_key=None -- nothing before this task needed a real key on it. The
401 path calls HttpSignature.signed_request, which loads that column as a
PEM private key (cryptography.hazmat's load_pem_private_key) to sign the
request; None fails there before any network call happens. Every 401-path
test in this file therefore goes through `seed_signing_site`
(tests/factories.py), which assigns a real keypair from
app.activitypub.signature.RsaKeys.generate_keypair() onto the Site row after
make_site() and commits, rather than relying on make_site()'s default.
"""
import httpx
import pytest

from app.activitypub.util import remote_object_to_json
from tests.factories import PEER_OBJECT_URI, seed_signing_site

URI = PEER_OBJECT_URI

# Both sleep call sites reachable from this function are neutralised by the
# shared no_real_sleeping fixture -- see the module docstring's Timing
# decision paragraph for why both are needed.
pytestmark = pytest.mark.usefixtures('no_real_sleeping')


class TestSuccessfulPlainFetch:
    """Mutation that fails this: mutating `object_request.json()`'s call or
    the return statement inside the `status_code == 200` branch."""

    def test_a_200_response_with_valid_json_returns_the_parsed_dict(self, app, db_session, http_mock):
        http_mock.get(URI).respond(200, json={'type': 'Note', 'id': URI})
        assert remote_object_to_json(URI) == {'type': 'Note', 'id': URI}


class TestUnparseableBody:
    """Mutation that fails this: removing the bare `except:` around
    `object_request.json()` in the 200 branch (it would then raise instead
    of returning None) -- reported, not narrowed; see the module docstring."""

    def test_a_200_response_with_an_unparseable_body_returns_none(self, app, db_session, http_mock):
        http_mock.get(URI).respond(200, content=b'not json', headers={'content-type': 'application/json'})
        assert remote_object_to_json(URI) is None


class TestOtherStatusCodes:
    """No Site row exists in this class, and only ONE route is ever
    registered -- both are deliberate. If a `status_code == 200` or
    `status_code == 401` mutation makes this input take either of those
    branches instead of the `else`, the 401 branch crashes immediately on
    `Site.query.get(1)` being None (AttributeError on `.private_key`) before
    any network call, and the 200 branch would return the body below's dict
    instead of None -- either way the mutation is caught, and a spurious
    extra fetch is caught by http_mock's assert_all_called=True /
    block_outbound_http raising on anything unmatched.
    """

    def test_a_404_response_returns_none(self, app, db_session, http_mock):
        http_mock.get(URI).respond(404, json={'error': 'not found', 'id': URI})
        assert remote_object_to_json(URI) is None


class TestUnsignedTransportFailureRetry:
    """The plain get_request path's own retry, one level up from
    get_request's internal ReadError/HTTPError retry -- see the module
    docstring's Timing decision paragraph for why each of these two tests
    involves the httpx transport failing twice per remote_object_to_json
    attempt (once for get_request's own first try, once for its internal
    retry) before remote_object_to_json's outer except even sees it.
    """

    def test_two_transport_failures_then_a_successful_retry_returns_the_parsed_dict(self, app, db_session, http_mock):
        """Mutation that fails this: deleting remote_object_to_json's outer
        `except httpx.HTTPError:` retry (the raised error would propagate
        instead of being caught)."""
        http_mock.get(URI).mock(side_effect=[
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
            httpx.Response(200, json={'type': 'Note', 'id': URI}),
        ])
        assert remote_object_to_json(URI) == {'type': 'Note', 'id': URI}

    def test_four_transport_failures_returns_none(self, app, db_session, http_mock):
        """Mutation that fails this: the inner `except httpx.HTTPError:
        return None` being replaced with something that re-raises or
        returns a non-None value."""
        http_mock.get(URI).mock(side_effect=[
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
        ])
        assert remote_object_to_json(URI) is None


class TestSignedRetryOnUnauthorized:
    """The 401 branch: a plain fetch returning 401 triggers a SIGNED
    re-fetch of the same URI. respx matches by (method, url), so the two
    fetches share one route with an ordered side_effect list -- the first
    entry answers the plain get_request call, later entries answer the
    signed_get_request call(s).
    """

    def test_a_401_then_a_successful_signed_fetch_returns_the_parsed_dict(self, app, db_session, http_mock):
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={'type': 'Note', 'id': URI, 'via': 'signed'}),
        ])
        assert remote_object_to_json(URI) == {'type': 'Note', 'id': URI, 'via': 'signed'}

    def test_a_401_then_an_unparseable_signed_body_returns_none(self, app, db_session, http_mock):
        """Mutation that fails this: removing the bare `except:` around
        `object_request.json()` in the 401 branch specifically -- a
        SEPARATE copy of the same pattern covered for the 200 branch above,
        per the task's note that a guard/handler duplicated across two
        locations needs its own test per location."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.Response(200, content=b'not json', headers={'content-type': 'application/json'}),
        ])
        assert remote_object_to_json(URI) is None

    def test_signed_fetch_transport_failure_then_a_successful_retry_returns_the_parsed_dict(self, app, db_session, http_mock):
        """The signed path's own transport-failure retry -- distinct from
        TestUnsignedTransportFailureRetry because signed_get_request (unlike
        get_request) has no internal retry of its own, so only ONE failure
        per attempt is needed here rather than two."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.ConnectError('boom'),
            httpx.Response(200, json={'type': 'Note', 'id': URI, 'via': 'signed-retry'}),
        ])
        assert remote_object_to_json(URI) == {'type': 'Note', 'id': URI, 'via': 'signed-retry'}

    def test_a_401_then_the_signed_request_also_failing_returns_none(self, app, db_session, http_mock):
        """Mutation that fails this: the 401 branch's inner `except
        httpx.HTTPError: return None` being replaced with something that
        re-raises or returns a non-None value."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
        ])
        assert remote_object_to_json(URI) is None


