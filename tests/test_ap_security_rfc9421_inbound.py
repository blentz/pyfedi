"""S5 inbound: RFC 9421 requests through precheck, verify_request, the inbox and signed GET identification."""
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from flask import request

from app import db
from app.activitypub import signature_rfc9421 as rfc9421
from app.activitypub.entitlement import signed_requester_actor
from app.activitypub.signature import GET_REQUIRED, POST_REQUIRED, HttpSignature, VerificationError, \
    VerificationFormatError, http_date, request_key_id
from app.models import ActivityPubLog, Site
from tests.factories import a_keypair, cavage_signed_headers, inbox_activity, make_site, rfc9421_inbox_post

pytestmark = pytest.mark.usefixtures('redis_double')

HOST = 'test.piefed.local'
STALE = 4000   # seconds: past rfc9421.CREATED_MAX_AGE_SECONDS


@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append(a))
    return calls


def _context(app, headers, method='POST', path='/inbox', body=b''):
    return app.test_request_context(path, method=method, headers=dict(headers, Host=HOST), data=body)


def _custom(covered, private_key, key_id, *, method='GET', url=f'https://{HOST}/post/1', created=None):
    """RFC 9421 headers covering exactly `covered`, which production's sign() cannot be asked to do."""
    created = int(time.time()) if created is None else created
    params = '(' + ' '.join(f'"{c}"' for c in covered) + f');created={created};keyid="{key_id}"'
    base = rfc9421.signature_base(covered, params, method, url, {})
    key = serialization.load_pem_private_key(private_key.encode(), password=None)
    signature = key.sign(base.encode(), padding.PKCS1v15(), hashes.SHA256())
    return {'Signature-Input': f'sig1={params}', 'Signature': f'sig1=:{base64.b64encode(signature).decode()}:'}


def _two_labels(first, second):
    """`first` and `second` (sign() output, both labelled sig1) as one request's labels sig1 and sig2."""
    return {'Signature-Input': first['Signature-Input'] + ', ' + second['Signature-Input'].replace('sig1=', 'sig2=', 1),
            'Signature': first['Signature'] + ', ' + second['Signature'].replace('sig1=', 'sig2=', 1)}


class TestInbox:

    @pytest.mark.parametrize('date', [False, True])
    def test_a_genuine_rfc9421_activity_is_dispatched(self, app, signing_peer, dispatched, date):
        with app.test_client() as client:
            response = rfc9421_inbox_post(client, inbox_activity(signing_peer), signing_peer, date=date)
        assert response.status_code == 200 and len(dispatched) == 1

    def test_the_wrong_key_is_refused(self, app, signing_peer, dispatched):
        signing_peer.public_key = next(p for _, p in (a_keypair() for _ in range(16)) if p != signing_peer.public_key)
        db.session.commit()
        with app.test_client() as client:
            response = rfc9421_inbox_post(client, inbox_activity(signing_peer), signing_peer)
        assert response.status_code == 400 and dispatched == []
        assert ActivityPubLog.query.one().exception_message.startswith('Could not verify HTTP signature')

    def test_a_tampered_body_fails_precheck(self, app, signing_peer, dispatched):
        activity = inbox_activity(signing_peer)
        headers = rfc9421.sign('post', f'https://{HOST}/inbox', json.dumps(activity).encode(), signing_peer.private_key,
                               f'{signing_peer.ap_profile_id}#main-key')
        with app.test_client() as client:
            response = client.post('/inbox', data=json.dumps(dict(activity, type='Dislike')).encode(),
                                   headers=dict(headers, Host=HOST), content_type='application/activity+json')
        assert response.status_code == 400
        assert ActivityPubLog.query.one().exception_message == 'Precheck failed: Content-Digest is incorrect'

    def test_a_stale_created_time_fails_precheck(self, app, signing_peer, dispatched):
        body = json.dumps(inbox_activity(signing_peer)).encode()
        headers = rfc9421.sign('post', f'https://{HOST}/inbox', body, signing_peer.private_key,
                               f'{signing_peer.ap_profile_id}#main-key', created=int(time.time()) - STALE)
        with app.test_client() as client:
            response = client.post('/inbox', data=body, headers=dict(headers, Host=HOST),
                                   content_type='application/activity+json')
        assert response.status_code == 400 and dispatched == []
        assert ActivityPubLog.query.one().exception_message == 'Precheck failed: Signature is too old'


class TestPrecheck:

    def test_no_digest_of_either_kind_fails(self, app):
        with _context(app, {'Date': 'x'}):
            with pytest.raises(VerificationFormatError, match='No digest header present'):
                HttpSignature.precheck(request)

    def test_a_cavage_request_still_needs_a_date(self, app):
        body = b'{}'
        with _context(app, {'Digest': HttpSignature.calculate_digest(body)}, body=body):
            with pytest.raises(VerificationFormatError, match='No date header present'):
                HttpSignature.precheck(request)

    def test_both_digests_are_checked(self, app):
        body = b'{}'
        headers = {'Digest': HttpSignature.calculate_digest(body), 'Content-Digest': rfc9421.content_digest(b'[]'),
                   'Signature-Input': 'sig1=()'}
        with _context(app, headers, body=body):
            with pytest.raises(VerificationFormatError, match='Content-Digest is incorrect'):
                HttpSignature.precheck(request)

    def test_a_wrong_digest_fails_beside_a_content_digest(self, app):
        body = b'{}'
        headers = {'Digest': HttpSignature.calculate_digest(b'[]'), 'Content-Digest': rfc9421.content_digest(body),
                   'Signature-Input': f'sig1=();created={int(time.time())}'}
        with _context(app, headers, body=body):
            with pytest.raises(VerificationFormatError, match='Digest is incorrect'):
                HttpSignature.precheck(request)

    @pytest.mark.parametrize('digest', ['Digest', 'Content-Digest'])
    def test_an_rfc9421_request_needs_no_date(self, app, digest):
        body = b'{}'
        value = HttpSignature.calculate_digest(body) if digest == 'Digest' else rfc9421.content_digest(body)
        headers = {digest: value, 'Signature-Input': f'sig1=();created={int(time.time())}'}
        with _context(app, headers, body=body):
            HttpSignature.precheck(request)

    def test_an_rfc9421_request_is_fresh_by_created_not_by_date(self, app):
        body = b'{}'
        headers = {'Content-Digest': rfc9421.content_digest(body), 'Date': http_date(time.time() - STALE),
                   'Signature-Input': f'sig1=();created={int(time.time())}'}
        with _context(app, headers, body=body):
            HttpSignature.precheck(request)

    @pytest.mark.parametrize('signature_input, message', [
        ('sig1=()', 'Signature-Input has no created time'),
        (f'sig1=();created={int(time.time()) - STALE}', 'Signature is too old'),
        (f'sig1=();created={int(time.time()) + STALE}', 'Signature is from the future'),
        ('', 'No Signature-Input header present'),
    ])
    def test_an_rfc9421_request_without_a_fresh_created_time_fails(self, app, signature_input, message):
        body = b'{}'
        headers = {'Content-Digest': rfc9421.content_digest(body), 'Signature-Input': signature_input}
        with _context(app, headers, body=body):
            with pytest.raises(VerificationFormatError, match=message):
                HttpSignature.precheck(request)


class TestVerifyRequest:

    def _signed(self, method, body):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign(method, f'https://{HOST}/inbox', body, private_key, 'https://peer.example/u/a#k')
        return headers, public_key

    def test_a_post_meets_the_cavage_required_set(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
        headers, public_key = self._signed('post', b'{}')
        with _context(app, headers, body=b'{}'):
            assert HttpSignature.verify_request(request, public_key, POST_REQUIRED) is True

    def test_a_get_meets_the_get_set(self, app, monkeypatch):
        monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', True)
        headers, public_key = self._signed('get', None)
        with _context(app, headers, method='GET'):
            assert HttpSignature.verify_request(request, public_key, GET_REQUIRED) is True

    def test_a_query_string_is_part_of_the_target_uri(self, app):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('get', f'https://{HOST}/post/1?page=2', None, private_key, 'k')
        with app.test_request_context('/post/1?page=2', method='GET', headers=dict(headers, Host=HOST)):
            assert HttpSignature.verify_request(request, public_key) is True

    @pytest.mark.parametrize('protocol, scheme', [('https', 'https'), ('http', 'http'), ('mixed', 'https')])
    def test_the_target_uri_scheme_is_the_federation_scheme(self, app, monkeypatch, protocol, scheme):
        """'mixed' serves the web UI over http but federates over https, as SERVER_URL says."""
        monkeypatch.setitem(app.config, 'HTTP_PROTOCOL', protocol)
        monkeypatch.setitem(app.config, 'SERVER_URL', f'{scheme}://{HOST}')
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('get', f'{scheme}://{HOST}/inbox', None, private_key, 'k')
        with _context(app, headers, method='GET'):
            assert HttpSignature.verify_request(request, public_key) is True

    @pytest.mark.parametrize('enforce', [False, True])
    def test_a_post_without_a_covered_digest(self, app, caplog, monkeypatch, enforce):
        monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', enforce)
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('post', f'https://{HOST}/inbox', None, private_key, 'https://peer.example/u/a#k')
        with _context(app, headers, body=b''):
            if enforce:
                with pytest.raises(VerificationError, match='unsigned required header: content-digest'):
                    HttpSignature.verify_request(request, public_key, POST_REQUIRED)
            else:
                with caplog.at_level('WARNING', logger=app.logger.name):
                    assert HttpSignature.verify_request(request, public_key, POST_REQUIRED) is True
                assert 'Signature from peer.example does not sign: content-digest' in caplog.text

    def test_request_key_id_reads_rfc9421(self, app):
        headers, _ = self._signed('get', None)
        with _context(app, headers, method='GET'):
            assert request_key_id(request) == 'https://peer.example/u/a#k'

    def test_a_label_naming_another_key_is_not_satisfied_by_the_resolved_key(self, app):
        """The keyId the caller resolved the key from is the first label's; a later label signed by that key but
        naming another keyId must not verify the request."""
        private_key, public_key = a_keypair()
        other_private, _ = a_keypair()
        url = f'https://{HOST}/inbox'
        first = rfc9421.sign('get', url, None, other_private, 'https://peer.example/u/b#k')
        second = rfc9421.sign('get', url, None, private_key, 'https://peer.example/u/a#k')
        with _context(app, _two_labels(first, second), method='GET'):
            assert request_key_id(request) == 'https://peer.example/u/b#k'
            with pytest.raises(VerificationError, match='Label sig2 names another key'):
                HttpSignature.verify_request(request, public_key)

    def test_a_later_label_naming_the_resolved_key_verifies(self, app):
        private_key, public_key = a_keypair()
        url = f'https://{HOST}/inbox'
        first = {'Signature-Input': 'sig1=("@method");created=1;keyid="https://peer.example/u/a#k"',
                 'Signature': 'sig1=:YWJj:'}
        second = rfc9421.sign('get', url, None, private_key, 'https://peer.example/u/a#k')
        with _context(app, _two_labels(first, second), method='GET'):
            assert HttpSignature.verify_request(request, public_key) is True

    def test_a_signature_without_a_keyid_fails(self, app):
        private_key, public_key = a_keypair()
        headers = _custom(['@method'], private_key, 'k', url=f'https://{HOST}/inbox')
        headers['Signature-Input'] = headers['Signature-Input'].replace(';keyid="k"', '')
        with _context(app, headers, method='GET'):
            with pytest.raises(VerificationFormatError, match='Signature-Input names no keyid'):
                HttpSignature.verify_request(request, public_key)


class TestSignedGet:

    def test_signed_requester_actor_identifies_an_rfc9421_signer(self, app, signing_peer):
        headers = rfc9421.sign('get', f'https://{HOST}/post/1', None, signing_peer.private_key,
                               f'{signing_peer.ap_profile_id}#main-key')
        assert 'Date' not in headers
        with app.test_request_context('/post/1', method='GET', headers=dict(headers, Host=HOST)):
            assert signed_requester_actor() == signing_peer

    def test_a_stale_created_time_identifies_nobody(self, app, signing_peer):
        headers = rfc9421.sign('get', f'https://{HOST}/post/1', None, signing_peer.private_key,
                               f'{signing_peer.ap_profile_id}#main-key', created=int(time.time()) - STALE)
        with app.test_request_context('/post/1', method='GET', headers=dict(headers, Host=HOST)):
            assert signed_requester_actor() is None

    def test_the_get_set_is_enforced_whatever_the_flag_says(self, app, monkeypatch, signing_peer):
        monkeypatch.setitem(app.config, 'SIG_REQUIRED_HEADERS_ENFORCE', False)
        headers = _custom(['@method'], signing_peer.private_key, f'{signing_peer.ap_profile_id}#main-key')
        with app.test_request_context('/post/1', method='GET', headers=dict(headers, Host=HOST)):
            assert signed_requester_actor() is None


class TestHostPinning:
    """R-HOSTPIN: a request signed for another instance and replayed here with that instance's Host is refused."""

    EVIL = 'evil.example'

    def _cavage_post(self, app, sender, host):
        body = json.dumps(inbox_activity(sender)).encode()
        headers = cavage_signed_headers(sender.private_key, f'{sender.ap_profile_id}#main-key', body=body, host=host)
        with app.test_client() as client:
            return client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')

    def _rfc9421_post(self, app, sender, host):
        body = json.dumps(inbox_activity(sender)).encode()
        headers = rfc9421.sign('post', f'https://{host}/inbox', body, sender.private_key,
                               f'{sender.ap_profile_id}#main-key')
        with app.test_client() as client:
            return client.post('/inbox', data=body, headers=dict(headers, Host=host),
                               content_type='application/activity+json')

    @pytest.mark.parametrize('post', ['_cavage_post', '_rfc9421_post'])
    def test_an_inbox_post_replayed_with_another_host_is_refused(self, app, signing_peer, dispatched, post):
        response = getattr(self, post)(app, signing_peer, self.EVIL)
        assert response.status_code == 400 and dispatched == []
        assert ActivityPubLog.query.one().exception_message.startswith('Could not verify HTTP signature')

    @pytest.mark.parametrize('post', ['_cavage_post', '_rfc9421_post'])
    def test_the_same_post_with_this_host_is_dispatched(self, app, signing_peer, dispatched, post):
        response = getattr(self, post)(app, signing_peer, HOST)
        assert response.status_code == 200 and len(dispatched) == 1

    def _cavage_get(self, sender, host):
        return cavage_signed_headers(sender.private_key, f'{sender.ap_profile_id}#main-key', method='get',
                                     path='/post/1', signed=('(request-target)', 'host', 'date'), host=host)

    def _rfc9421_get(self, sender, host):
        return dict(rfc9421.sign('get', f'https://{host}/post/1', None, sender.private_key,
                                 f'{sender.ap_profile_id}#main-key'), Host=host)

    @pytest.mark.parametrize('sign', ['_cavage_get', '_rfc9421_get'])
    @pytest.mark.parametrize('host, identified', [(EVIL, False), (HOST, True)])
    def test_a_signed_get_identifies_its_signer_only_for_this_host(self, app, signing_peer, sign, host, identified):
        with app.test_request_context('/post/1', method='GET', headers=getattr(self, sign)(signing_peer, host)):
            assert signed_requester_actor() == (signing_peer if identified else None)

    def test_a_cavage_replay_names_the_host_in_its_error(self, app):
        private_key, public_key = a_keypair()
        headers = cavage_signed_headers(private_key, 'https://peer.example/u/a#k', host=self.EVIL)
        with app.test_request_context('/inbox', method='POST', headers=headers, data=b''):
            with pytest.raises(VerificationError, match='Signed host evil.example is not this instance'):
                HttpSignature.verify_request(request, public_key)

    @pytest.mark.parametrize('host', ['TEST.piefed.local', 'test.piefed.local:443', 'test.piefed.local:80'])
    def test_case_and_a_default_port_are_the_same_host(self, app, host):
        private_key, public_key = a_keypair()
        headers = cavage_signed_headers(private_key, 'https://peer.example/u/a#k', host=host)
        with app.test_request_context('/inbox', method='POST', headers=headers, data=b''):
            assert HttpSignature.verify_request(request, public_key) is True

    @pytest.mark.parametrize('host, accepted', [('test.piefed.local:8443', True), ('test.piefed.local', False)])
    def test_a_server_name_with_a_port_must_match_it(self, app, monkeypatch, host, accepted):
        monkeypatch.setitem(app.config, 'SERVER_NAME', 'test.piefed.local:8443')
        monkeypatch.setitem(app.config, 'SERVER_URL', 'https://test.piefed.local:8443')
        private_key, public_key = a_keypair()
        cavage = cavage_signed_headers(private_key, 'k', host=host)
        signed = dict(rfc9421.sign('post', f'https://{host}/inbox', b'', private_key, 'k'), Host=host)
        for headers in (cavage, signed):
            with app.test_request_context('/inbox', method='POST', headers=headers, data=b''):
                if accepted:
                    assert HttpSignature.verify_request(request, public_key) is True
                else:
                    with pytest.raises(VerificationError):
                        HttpSignature.verify_request(request, public_key)

    def test_an_unsigned_host_is_not_checked(self, app):
        """Leaving host unsigned is _check_required's business (a warning, or a refusal under enforcement)."""
        private_key, public_key = a_keypair()
        headers = cavage_signed_headers(private_key, 'https://peer.example/u/a#k', host=self.EVIL,
                                        signed=('(request-target)', 'date', 'digest'))
        with app.test_request_context('/inbox', method='POST', headers=headers, data=b''):
            assert HttpSignature.verify_request(request, public_key) is True


class TestInboxPaths:
    """An RFC 9421 POST through the key refetch and the keyId-host refusal, as cavage ones go."""

    def test_a_rotated_key_is_accepted_after_one_refetch(self, app, signing_peer, http_mock, dispatched):
        site = db.session.get(Site, 1) or make_site()
        site.private_key, _ = a_keypair()
        private_key, public_key = signing_peer.private_key, signing_peer.public_key
        signing_peer.public_key = next(p for _, p in (a_keypair() for _ in range(16)) if p != public_key)
        db.session.commit()
        route = http_mock.get(signing_peer.ap_profile_id).respond(
            200, json={'id': signing_peer.ap_profile_id, 'publicKey': {'publicKeyPem': public_key}})
        with app.test_client() as client:
            response = rfc9421_inbox_post(client, inbox_activity(signing_peer), signing_peer)
        assert response.status_code == 200 and len(dispatched) == 1 and route.call_count == 1
        db.session.refresh(signing_peer)
        assert signing_peer.public_key == public_key

    def test_a_key_id_on_another_host_is_refused(self, app, signing_peer, dispatched):
        with app.test_client() as client:
            response = rfc9421_inbox_post(client, inbox_activity(signing_peer), signing_peer,
                                          key_id='https://other.example/actor#main-key')
        assert response.status_code == 400 and dispatched == []
        assert ActivityPubLog.query.one().exception_message == 'Signature keyId host does not match actor'
