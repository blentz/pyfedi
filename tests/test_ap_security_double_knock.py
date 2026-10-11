"""S5 outbound: signing in RFC 9421, and one retry in the other format on a 401."""
import httpx
import pytest

from app import db
from app.activitypub import signature_rfc9421 as rfc9421
from app.activitypub.signature import HttpSignature, double_knock, post_request, signed_get_request
from app.models import ActivityPubLog, Instance, SendQueue
from tests.factories import a_keypair, make_instance

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
        assert rfc9421.verify('POST', INBOX, headers, body, public_key, rfc9421.RFC9421_POST_REQUIRED)

    def test_rfc9421_signs_the_target_uri_as_sent(self, app):
        """The query is part of the target URI; a fragment never goes on the wire, so it is not signed."""
        private_key, public_key = a_keypair()
        uri, headers, body = HttpSignature.signed_request(INBOX + '?x=1#frag', _body(), private_key, KEY_ID,
                                                          send_via_async=True, signature_format='rfc9421')
        assert rfc9421.verify('POST', INBOX + '?x=1', headers, body, public_key, rfc9421.RFC9421_POST_REQUIRED)

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
                              rfc9421.RFC9421_GET_REQUIRED)
        db.session.refresh(instance)
        assert instance.signature_format == 'rfc9421'
