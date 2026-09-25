"""What goes on the wire when this instance signs and sends an activity.

Sub-project 114, and the first of the warning burn-down. `HttpSignature.
signed_request` passed its body to httpx as `data=body_bytes`. httpx takes raw
bytes through `content=` and keeps `data=` for form encoding, so every outbound
federation request raised

    DeprecationWarning: Use 'content=<...>' to upload raw bytes/text content.

-- 199 of the suite's 455 warnings, one per federated send -- and the spelling
stops working when httpx drops it, which would take every outbound activity with
it.

The swap is safe because it is the same bytes: measured with both spellings
against the same route, the request carried the same body, the same Content-Type
and the same Content-Length. That equality is what the signature depends on --
the Digest header is computed over those bytes, and the signature over the
Digest -- so these tests assert the wire form rather than the call.
"""
import json
import warnings

import httpx
import pytest
import respx

from app.activitypub.signature import HttpSignature, RsaKeys


@pytest.fixture
def keypair():
    private_key, public_key = RsaKeys.generate_keypair()
    return private_key, public_key


@pytest.fixture
def activity():
    return {'id': 'https://test.piefed.local/activities/1', 'type': 'Create',
            'actor': 'https://test.piefed.local/u/alice',
            'object': {'id': 'https://test.piefed.local/post/1',
                       'type': 'Note', 'content': 'hello'}}


def send(app, keypair, activity, **overrides):
    private_key, _public_key = keypair
    with app.app_context():
        return HttpSignature.signed_request(
            uri=overrides.get('uri', 'https://peer.test/inbox'),
            body=activity, private_key=private_key,
            key_id='https://test.piefed.local/u/alice#main-key',
            **{name: value for name, value in overrides.items() if name != 'uri'})


class TestTheRequestItself:
    def test_the_body_is_the_activity(self, app, keypair, activity):
        with respx.mock as mock:
            route = mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            send(app, keypair, activity)
        sent = route.calls[0].request
        assert json.loads(sent.content) == activity

    def test_the_bytes_are_sent_exactly_as_signed(self, app, keypair, activity):
        """The Digest is over these bytes, so anything that re-encodes them --
        form encoding, a different separator, a re-serialisation -- breaks the
        signature at the far end rather than here."""
        with respx.mock as mock:
            route = mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            send(app, keypair, activity)
        sent = route.calls[0].request
        assert sent.content == json.dumps(activity).encode('utf-8')

    def test_the_content_type_is_the_one_asked_for(self, app, keypair, activity):
        with respx.mock as mock:
            route = mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            send(app, keypair, activity)
        assert route.calls[0].request.headers['content-type'] == \
            'application/activity+json'

    def test_a_different_content_type_is_carried_through(self, app, keypair,
                                                         activity):
        with respx.mock as mock:
            route = mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            send(app, keypair, activity, content_type='application/ld+json')
        assert route.calls[0].request.headers['content-type'] == \
            'application/ld+json'

    def test_the_content_length_matches_the_body(self, app, keypair, activity):
        with respx.mock as mock:
            route = mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            send(app, keypair, activity)
        sent = route.calls[0].request
        assert int(sent.headers['content-length']) == len(sent.content)

    def test_nothing_is_form_encoded(self, app, keypair, activity):
        """`data=` with a mapping would send `application/x-www-form-urlencoded`
        and a body of `id=...&type=...`. The body is JSON and the header says
        so."""
        with respx.mock as mock:
            route = mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            send(app, keypair, activity)
        sent = route.calls[0].request
        assert b'&' not in sent.content
        assert 'urlencoded' not in sent.headers['content-type']

    def test_the_signature_headers_are_there(self, app, keypair, activity):
        with respx.mock as mock:
            route = mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            send(app, keypair, activity)
        headers = route.calls[0].request.headers
        assert 'signature' in headers
        assert 'digest' in headers
        assert headers['digest'].startswith('SHA-256=')

    def test_the_digest_is_over_the_bytes_that_were_sent(self, app, keypair,
                                                         activity):
        """The property the encoding must not break: a peer recomputes this
        from the body it received, and refuses the activity if it differs."""
        import base64
        import hashlib

        with respx.mock as mock:
            route = mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            send(app, keypair, activity)
        sent = route.calls[0].request
        expected = 'SHA-256=' + base64.b64encode(
            hashlib.sha256(sent.content).digest()).decode('utf-8')
        assert sent.headers['digest'] == expected

    def test_the_user_agent_names_this_instance(self, app, keypair, activity):
        with respx.mock as mock:
            route = mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            send(app, keypair, activity)
        assert 'PieFed/' in route.calls[0].request.headers['user-agent']


class TestItRaisesNoDeprecation:
    """The burn-down's own assertion. 199 of the suite's 455 warnings came from
    this one call."""

    def test_a_send_warns_about_nothing(self, app, keypair, activity):
        with respx.mock as mock:
            mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                send(app, keypair, activity)
        assert [str(warning.message) for warning in caught] == []

    def test_a_get_warns_about_nothing_either(self, app, keypair, activity):
        with respx.mock as mock:
            mock.get('https://peer.test/actor').mock(
                return_value=httpx.Response(200, json={}))
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                send(app, keypair, activity, uri='https://peer.test/actor',
                     method='get')
        assert [str(warning.message) for warning in caught] == []

    def test_the_deprecated_spelling_is_gone_from_the_source(self):
        """The property, so the next `data=<bytes>` is caught when it is
        written. `app/translation.py` and `app/shared/tasks/users.py` pass
        MAPPINGS as `data=`, which is form encoding and is what that argument is
        for; only raw bytes belong in `content=`."""
        from pathlib import Path

        offenders = []
        for path in sorted(Path('app').rglob('*.py')):
            lines = path.read_text(encoding='utf8').splitlines()
            for number, line in enumerate(lines, start=1):
                if 'data=body' in line or 'data=payload' in line.strip():
                    offenders.append(f'{path}:{number}: {line.strip()}')
        assert offenders == []


class TestWhatTheCallerGetsBack:
    def test_the_response_comes_back(self, app, keypair, activity):
        with respx.mock as mock:
            mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202, text='thanks'))
            response = send(app, keypair, activity)
        assert response.status_code == 202
        assert response.text == 'thanks'

    @pytest.mark.parametrize('status', [400, 403, 404, 410, 418, 422])
    def test_a_refusal_comes_back_as_a_response(self, app, keypair, activity,
                                                status):
        """D1322. A branch here raised ValueError for a 4xx POST, and had never
        run once: `method` is `Literal["get", "post"]` and every caller passes
        it lowercase, while the comparison was against "POST".

        It is GONE rather than corrected, because `post_request` reads these
        responses: 410 and 418 mark the peer gone forever and empty its send
        queue, `community_has_no_followers` repairs the membership, and
        `person_is_banned_from_site` processes the ban. A raise lands in that
        function's `except Exception` instead, which does none of them."""
        with respx.mock as mock:
            mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(status, text='no'))
            response = send(app, keypair, activity)
        assert response.status_code == status

    def test_a_server_error_comes_back_too(self, app, keypair, activity):
        """The retry path reads the status off the response as well."""
        with respx.mock as mock:
            mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(503))
            assert send(app, keypair, activity).status_code == 503

    def test_nothing_in_the_source_raises_on_a_status_any_more(self):
        from pathlib import Path
        source = Path('app/activitypub/signature.py').read_text(encoding='utf8')
        assert 'raise ValueError(' not in source or \
            'POST error to' not in source

    def test_async_sending_returns_the_pieces_instead_of_sending(self, app,
                                                                 keypair,
                                                                 activity):
        """`send_via_async=True` hands the signed request to another service, so
        nothing goes out here -- and the bytes it hands over are the ones the
        signature was made over."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post('https://peer.test/inbox').mock(
                return_value=httpx.Response(202))
            uri, headers, body_bytes = send(app, keypair, activity,
                                            send_via_async=True)
            assert len(mock.calls) == 0
        assert uri == 'https://peer.test/inbox'
        assert body_bytes == json.dumps(activity).encode('utf-8')
        assert headers['Digest'].startswith('SHA-256=')
