"""remote_object_to_json (app/activitypub/util.py) fetches a URI and returns
the parsed JSON object (a dict), or None. It is fetch_object_capped with a
retry_delay of 3:

- unsigned 200 whose body is a JSON object -> the dict.
- 200 whose body is not JSON, or is JSON but not an object -> None.
- 401 -> a SIGNED fetch (signed_get_capped) using Site 1's private_key; its
  200 is read as above, and any other signed status -> None, its body never
  parsed. No Site row -> None.
- anything else (including a 3xx, which is not followed) -> None.

Every attempt is capped (get_request_capped): at most OBJECT_MAX_BYTES of
body and a wall-clock deadline, so a hostile origin's huge or slow-drip body
returns None without being buffered (TestCappedFetch).

Both fetches keep their transport-failure retry: on httpx.HTTPError the
function sleeps 3 seconds and tries that fetch once more before returning
None. get_request_capped has no internal retry (get_request had one), so one
failure per attempt is what a test needs. The shared `no_real_sleeping`
fixture, opted into by pytestmark, patches both `time.sleep` and
`app.utils.sleep`; retry BEHAVIOUR is asserted, not timing.

The 401 path signs with Site.private_key, so every 401-path test goes through
`seed_signing_site` (tests/factories.py), which puts a real keypair on the
Site row.
"""
import time
from types import SimpleNamespace

import httpx
import pytest

from app.activitypub.util import OBJECT_MAX_BYTES, remote_object_to_json
from tests.factories import PEER_OBJECT_URI, seed_signing_site

URI = PEER_OBJECT_URI

# Both sleep call sites reachable from this function are neutralised by the
# shared no_real_sleeping fixture -- see the module docstring.
pytestmark = pytest.mark.usefixtures('no_real_sleeping')


class TestSuccessfulPlainFetch:
    """The unsigned 200 whose body is a JSON object comes back parsed."""

    def test_a_200_response_with_valid_json_returns_the_parsed_dict(self, app, db_session, http_mock):
        http_mock.get(URI).respond(200, json={'type': 'Note', 'id': URI})
        assert remote_object_to_json(URI) == {'type': 'Note', 'id': URI}


class TestUnparseableBody:
    """A 200 whose body does not parse returns None rather than raising."""

    def test_a_200_response_with_an_unparseable_body_returns_none(self, app, db_session, http_mock):
        http_mock.get(URI).respond(200, content=b'not json', headers={'content-type': 'application/json'})
        assert remote_object_to_json(URI) is None


class TestOtherStatusCodes:
    """No Site row exists in this class, and only ONE route is ever
    registered -- both are deliberate. A mutation sending this 404 down the
    401 branch returns None at the missing Site row without the single route
    being called twice, so the body below (a dict) would only come back if the
    status check itself were broken; a spurious extra fetch is caught by
    block_outbound_http raising on anything unmatched.
    """

    def test_a_404_response_returns_none(self, app, db_session, http_mock):
        http_mock.get(URI).respond(404, json={'error': 'not found', 'id': URI})
        assert remote_object_to_json(URI) is None


class TestUnsignedTransportFailureRetry:
    """remote_object_to_json's own retry: on httpx.HTTPError it sleeps and tries once more. The fetch is
    get_request_capped, which (unlike get_request) has no internal retry, so one failure per attempt."""

    def test_a_transport_failure_then_a_successful_retry_returns_the_parsed_dict(self, app, db_session, http_mock):
        """Mutation that fails this: deleting remote_object_to_json's outer
        `except httpx.HTTPError:` retry (the raised error would propagate
        instead of being caught)."""
        http_mock.get(URI).mock(side_effect=[
            httpx.ConnectError('boom'),
            httpx.Response(200, json={'type': 'Note', 'id': URI}),
        ])
        assert remote_object_to_json(URI) == {'type': 'Note', 'id': URI}

    def test_two_transport_failures_returns_none(self, app, db_session, http_mock):
        """Mutation that fails this: the inner `except httpx.HTTPError:
        return None` being replaced with something that re-raises or
        returns a non-None value."""
        route = http_mock.get(URI).mock(side_effect=[
            httpx.ConnectError('boom'),
            httpx.ConnectError('boom'),
        ])
        assert remote_object_to_json(URI) is None
        assert route.call_count == 2


class TestSignedRetryOnUnauthorized:
    """The 401 branch: a plain fetch returning 401 triggers a SIGNED
    re-fetch of the same URI. respx matches by (method, url), so the two
    fetches share one route with an ordered side_effect list -- the first
    entry answers the plain fetch, later entries answer the signed fetch(es).
    """

    def test_a_401_then_a_successful_signed_fetch_returns_the_parsed_dict(self, app, db_session, http_mock):
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={'type': 'Note', 'id': URI, 'via': 'signed'}),
        ])
        assert remote_object_to_json(URI) == {'type': 'Note', 'id': URI, 'via': 'signed'}

    def test_a_401_then_an_unparseable_signed_body_returns_none(self, app, db_session, http_mock):
        """The signed 200's unparseable body returns None rather than raising."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401),
            httpx.Response(200, content=b'not json', headers={'content-type': 'application/json'}),
        ])
        assert remote_object_to_json(URI) is None

    def test_signed_fetch_transport_failure_then_a_successful_retry_returns_the_parsed_dict(self, app, db_session, http_mock):
        """The signed fetch's own transport-failure retry, distinct from
        TestUnsignedTransportFailureRetry."""
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


def _endless(pulled):
    while True:
        pulled.append(1)
        yield b' ' * 65536


class TestCappedFetch:
    """Every attempt -- unsigned, signed, and each retry -- goes through get_request_capped (the signed one via
    signed_get_capped): a hostile origin's huge or slow body is cut off, not buffered, and None comes back."""

    def test_a_huge_unsigned_body_returns_none_unbuffered(self, app, db_session, http_mock):
        pulled = []
        http_mock.get(URI).respond(200, content=_endless(pulled))

        assert remote_object_to_json(URI) is None
        assert len(pulled) * 65536 <= OBJECT_MAX_BYTES + 65536

    def test_a_huge_body_on_the_unsigned_retry_returns_none_unbuffered(self, app, db_session, http_mock):
        pulled = []
        http_mock.get(URI).mock(side_effect=[httpx.ConnectError('boom'), httpx.Response(200, content=_endless(pulled))])

        assert remote_object_to_json(URI) is None
        assert len(pulled) * 65536 <= OBJECT_MAX_BYTES + 65536

    def test_a_huge_signed_body_returns_none_unbuffered(self, app, db_session, http_mock):
        seed_signing_site()
        pulled = []
        http_mock.get(URI).mock(side_effect=[httpx.Response(401), httpx.Response(200, content=_endless(pulled))])

        assert remote_object_to_json(URI) is None
        assert len(pulled) * 65536 <= OBJECT_MAX_BYTES + 65536

    def test_a_slow_unsigned_body_is_cut_at_the_wall_clock(self, app, db_session, http_mock, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr('app.utils.time', SimpleNamespace(**{**vars(time), 'monotonic': lambda: now[0]}))
        pulled = []

        def trickle():
            for _ in range(1000):
                pulled.append(1)
                now[0] += 5   # five fake seconds per byte
                yield b' '

        http_mock.get(URI).respond(200, content=trickle())

        assert remote_object_to_json(URI) is None
        assert len(pulled) <= 5

    def test_a_slow_signed_body_is_cut_at_the_wall_clock(self, app, db_session, http_mock, monkeypatch):
        seed_signing_site()
        now = [1000.0]
        monkeypatch.setattr('app.utils.time', SimpleNamespace(**{**vars(time), 'monotonic': lambda: now[0]}))
        pulled = []

        def trickle():
            for _ in range(1000):
                pulled.append(1)
                now[0] += 5
                yield b' '

        http_mock.get(URI).mock(side_effect=[httpx.Response(401), httpx.Response(200, content=trickle())])

        assert remote_object_to_json(URI) is None
        assert len(pulled) <= 5

    def test_a_redirect_is_not_followed(self, app, db_session, http_mock):
        """No route serves the Location, so following it would error the test."""
        route = http_mock.get(URI).respond(302, headers={'Location': 'https://elsewhere.example/x'})

        assert remote_object_to_json(URI) is None
        assert route.call_count == 1

    def test_a_signed_redirect_is_not_followed(self, app, db_session, http_mock):
        seed_signing_site()
        route = http_mock.get(URI).mock(side_effect=[
            httpx.Response(401), httpx.Response(302, headers={'Location': 'https://elsewhere.example/x'})])

        assert remote_object_to_json(URI) is None
        assert route.call_count == 2

    def test_a_signed_404_with_a_json_body_returns_none(self, app, db_session, http_mock):
        """The signed body used to be parsed whatever the status, so a 404 carrying a Note was returned."""
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[
            httpx.Response(401), httpx.Response(404, json={'type': 'Note', 'id': URI})])

        assert remote_object_to_json(URI) is None

    def test_a_peer_refusing_both_signature_formats_gets_exactly_two_signed_requests(self, app, db_session,
                                                                                     http_mock):
        seed_signing_site()
        route = http_mock.get(URI).respond(401)

        assert remote_object_to_json(URI) is None
        assert ['signature' in call.request.headers for call in route.calls] == [False, True, True]

    @pytest.mark.parametrize('body', [b'[1, 2]', b'"a string"', b'null', b'3'])
    def test_json_that_is_not_an_object_returns_none(self, app, db_session, http_mock, body):
        http_mock.get(URI).respond(200, content=body)

        assert remote_object_to_json(URI) is None

    def test_signed_json_that_is_not_an_object_returns_none(self, app, db_session, http_mock):
        seed_signing_site()
        http_mock.get(URI).mock(side_effect=[httpx.Response(401), httpx.Response(200, content=b'[1]')])

        assert remote_object_to_json(URI) is None

    def test_a_deeply_nested_body_returns_none_rather_than_raising(self, app, db_session, http_mock):
        http_mock.get(URI).respond(200, content=b'[' * 100000 + b']' * 100000)

        assert remote_object_to_json(URI) is None

    def test_a_401_without_a_site_row_returns_none(self, app, db_session, http_mock):
        route = http_mock.get(URI).respond(401)

        assert remote_object_to_json(URI) is None
        assert route.call_count == 1


