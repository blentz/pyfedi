"""S5 outbound: signing in RFC 9421, and one retry in the other format on a 401."""
import time
from types import SimpleNamespace

import httpx
import pytest

from app import db
from app.activitypub import signature_rfc9421 as rfc9421
from app.activitypub.signature import HttpSignature, double_knock, post_request, signed_get_capped, \
    signed_get_request
from app.models import ActivityPubLog, Instance, SendQueue
from tests.factories import a_keypair, make_instance
from tests.test_ap_security_rfc9421 import GET_COMPONENTS, POST_COMPONENTS

INBOX = 'https://peer.example/inbox'
KEY_ID = 'https://test.piefed.local/u/alice#main-key'


def _body():
    return {'id': 'https://test.piefed.local/activities/create/1', 'type': 'Create', 'actor': KEY_ID.split('#')[0]}


class TestSignedRequest:

    def test_rfc9421_headers_replace_cavage(self, app):
        private_key, public_key = a_keypair()
        uri, headers, body = HttpSignature.signed_request(INBOX, _body(), private_key, KEY_ID, send_via_async=True,
                                                          signature_format='rfc9421')
        assert 'Signature' in headers and 'Signature-Input' in headers and 'Content-Digest' in headers
        assert 'Digest' not in headers and '(request-target)' not in headers
        assert rfc9421.verify('POST', INBOX, headers, body, public_key, POST_COMPONENTS)

    def test_rfc9421_signs_the_target_uri_as_sent(self, app):
        """The query is part of the target URI; a fragment never goes on the wire, so it is not signed."""
        private_key, public_key = a_keypair()
        uri, headers, body = HttpSignature.signed_request(INBOX + '?x=1#frag', _body(), private_key, KEY_ID,
                                                          send_via_async=True, signature_format='rfc9421')
        assert rfc9421.verify('POST', INBOX + '?x=1', headers, body, public_key, POST_COMPONENTS)

    def test_cavage_is_the_default(self, app):
        _uri, headers, _body_bytes = HttpSignature.signed_request(INBOX, _body(), a_keypair()[0], KEY_ID,
                                                                  send_via_async=True)
        assert 'Digest' in headers and 'Signature-Input' not in headers

    def test_an_unknown_format_is_refused(self, app):
        with pytest.raises(ValueError, match='Unknown signature format'):
            HttpSignature.signed_request(INBOX, _body(), a_keypair()[0], KEY_ID, send_via_async=True,
                                         signature_format='other')


class TestDoubleKnock:

    def _send(self, statuses):
        sent = []

        def send(signature_format):
            sent.append(signature_format)
            return httpx.Response(statuses[len(sent) - 1])
        return send, sent

    def test_no_401_sends_once_in_the_stored_format(self, app, db_session):
        make_instance('peer.example').signature_format = 'rfc9421'
        db.session.commit()
        send, sent = self._send([202])
        assert double_knock(INBOX, send).status_code == 202
        assert sent == ['rfc9421']

    def test_an_unknown_instance_is_cavage_and_nothing_is_stored(self, app, db_session):
        send, sent = self._send([401, 202])
        assert double_knock(INBOX, send).status_code == 202
        assert sent == ['cavage', 'rfc9421'] and Instance.query.count() == 0

    @pytest.mark.parametrize('stored, alternate', [('cavage', 'rfc9421'), ('rfc9421', 'cavage')])
    def test_a_401_then_success_remembers_the_other_format(self, app, db_session, stored, alternate):
        instance = make_instance('peer.example')
        instance.signature_format = stored
        db.session.commit()
        send, sent = self._send([401, 200])
        double_knock(INBOX, send)
        assert sent == [stored, alternate]
        db.session.refresh(instance)
        assert instance.signature_format == alternate

    def test_a_peer_refusing_both_keeps_the_format_after_exactly_two_attempts(self, app, db_session):
        """Review Focus 5: a 401 that is not about the format (e.g. a domain block)."""
        instance = make_instance('peer.example')
        send, sent = self._send([401, 401])
        assert double_knock(INBOX, send).status_code == 401
        assert sent == ['cavage', 'rfc9421']
        db.session.refresh(instance)
        assert instance.signature_format == 'cavage'

    def test_a_stored_value_that_is_not_a_format_is_cavage(self, app, db_session):
        make_instance('peer.example').signature_format = 'bogus'
        db.session.commit()
        send, sent = self._send([202])
        double_knock(INBOX, send)
        assert sent == ['cavage']


class TestPostRequest:

    def test_a_401_then_202_delivers_with_a_real_rfc9421_signature(self, app, db_session, http_mock):
        private_key, public_key = a_keypair()
        instance = make_instance('peer.example')
        route = http_mock.post(INBOX).mock(side_effect=[httpx.Response(401), httpx.Response(202)])
        post_request(uri=INBOX, body=_body(), private_key=private_key, key_id=KEY_ID)
        first, second = (call.request for call in route.calls)
        assert 'signature-input' not in first.headers and 'signature' in first.headers
        assert rfc9421.verify('POST', INBOX, second.headers, second.content, public_key)
        db.session.refresh(instance)
        assert instance.signature_format == 'rfc9421'
        assert ActivityPubLog.query.one().result == 'success'

    def test_a_peer_refusing_both_is_a_logged_failure_and_not_queued(self, app, db_session, http_mock):
        """Spec S5's 'SendQueue retry' does not match post_request, which never queues a 4xx (see Plan clarification 6)."""
        make_instance('peer.example')
        route = http_mock.post(INBOX).mock(return_value=httpx.Response(401, text='blocked'))
        post_request(uri=INBOX, body=_body(), private_key=a_keypair()[0], key_id=KEY_ID)
        assert route.call_count == 2
        log = ActivityPubLog.query.one()
        assert log.result == 'failure' and log.exception_message.startswith('401: blocked')
        assert SendQueue.query.count() == 0


class TestSignedGet:

    def test_a_401_then_200_remembers_rfc9421(self, app, db_session, http_mock):
        private_key, public_key = a_keypair()
        instance = make_instance('peer.example')
        route = http_mock.get('https://peer.example/u/bob').mock(
            side_effect=[httpx.Response(401), httpx.Response(200, json={'id': 'https://peer.example/u/bob'})])
        response = signed_get_request('https://peer.example/u/bob', private_key, KEY_ID)
        assert response.status_code == 200 and route.call_count == 2
        second = route.calls[1].request
        assert rfc9421.verify('GET', 'https://peer.example/u/bob', second.headers, b'', public_key,
                              GET_COMPONENTS)
        db.session.refresh(instance)
        assert instance.signature_format == 'rfc9421'


OBJECT = 'https://peer.example/post/1'


class TestSignedGetCapped:
    """signed_get_capped: a signed GET under get_request_capped's byte cap, wall clock and no-redirect rule."""

    def test_a_401_then_200_remembers_rfc9421_in_exactly_two_signed_requests(self, app, db_session, http_mock):
        private_key, public_key = a_keypair()
        instance = make_instance('peer.example')
        route = http_mock.get(OBJECT).mock(side_effect=[httpx.Response(401), httpx.Response(200, content=b'{}')])
        assert signed_get_capped(OBJECT, private_key, KEY_ID, 1000) == (200, b'{}')
        assert route.call_count == 2
        first, second = (call.request for call in route.calls)
        assert 'signature' in first.headers and 'signature-input' not in first.headers
        assert rfc9421.verify('GET', OBJECT, second.headers, b'', public_key, GET_COMPONENTS)
        db.session.refresh(instance)
        assert instance.signature_format == 'rfc9421'

    def test_a_peer_refusing_both_formats_gets_no_third_request(self, app, db_session, http_mock):
        instance = make_instance('peer.example')
        route = http_mock.get(OBJECT).mock(return_value=httpx.Response(401))
        assert signed_get_capped(OBJECT, a_keypair()[0], KEY_ID, 1000) == (401, b'')
        assert route.call_count == 2
        db.session.refresh(instance)
        assert instance.signature_format == 'cavage'

    def test_a_non_401_answer_is_final_after_one_request(self, app, db_session, http_mock):
        route = http_mock.get(OBJECT).mock(return_value=httpx.Response(404, content=b'gone'))
        assert signed_get_capped(OBJECT, a_keypair()[0], KEY_ID, 1000) == (404, b'gone')
        assert route.call_count == 1

    def test_a_huge_body_is_refused_unbuffered(self, app, db_session, http_mock):
        pulled = []

        def endless():
            while True:
                pulled.append(1)
                yield b'x' * 1024

        http_mock.get(OBJECT).respond(200, content=endless())
        assert signed_get_capped(OBJECT, a_keypair()[0], KEY_ID, 4096) == (200, None)
        assert len(pulled) <= 5

    def test_a_slow_drip_is_cut_at_the_wall_clock(self, app, db_session, http_mock, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr('app.utils.time', SimpleNamespace(**{**vars(time), 'monotonic': lambda: now[0]}))
        pulled = []

        def trickle():
            for _ in range(1000):
                pulled.append(1)
                now[0] += 5   # five fake seconds per byte
                yield b'x'

        http_mock.get(OBJECT).respond(200, content=trickle())
        assert signed_get_capped(OBJECT, a_keypair()[0], KEY_ID, 1_000_000, max_seconds=15) == (200, None)
        assert len(pulled) <= 5

    def test_a_redirect_is_not_followed(self, app, db_session, http_mock):
        """No route serves the Location: following it would raise out of respx and error the test."""
        route = http_mock.get(OBJECT).respond(302, headers={'Location': 'https://elsewhere.example/post/1'})
        assert signed_get_capped(OBJECT, a_keypair()[0], KEY_ID, 1000)[0] == 302
        assert route.call_count == 1

    def test_status_only_reads_no_body(self, app, db_session, http_mock):
        http_mock.get(OBJECT).respond(200, content=b'{"type": "Note"}')
        assert signed_get_capped(OBJECT, a_keypair()[0], KEY_ID, 0) == (200, None)
