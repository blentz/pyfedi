"""Web Push crypto (app/webpush.py): RFC 8291's worked example reproduced to the byte, the VAPID header verifying
against the key it names, the key made once, and the endpoints a subscription may send to."""
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import db, webpush
from app.models import SiteDomain
from app.storefront import clear_domain_cache

# RFC 8291 Appendix A
PLAINTEXT = webpush.unb64('V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24')
AS_PRIVATE = 'yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw'
UA_PUBLIC = 'BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4'
UA_PRIVATE = 'q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94'
SALT = 'DGv6ra1nlYgDCS1FRnbzlw'
AUTH = 'BTBZMqHH6r4Tts7J_aSIgg'
HEADER = ('DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml'
          'mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8')
CIPHERTEXT = '8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ'


def _private(raw: str):
    return ec.derive_private_key(int.from_bytes(webpush.unb64(raw), 'big'), ec.SECP256R1())


def test_rfc_8291s_example_is_reproduced_to_the_byte():
    body = webpush.encrypt(PLAINTEXT, UA_PUBLIC, AUTH, as_private=_private(AS_PRIVATE), salt=webpush.unb64(SALT))
    assert body == webpush.unb64(HEADER) + webpush.unb64(CIPHERTEXT)


def test_a_fresh_message_differs_each_time_and_the_receiver_reads_it():
    first, second = (webpush.encrypt(b'hello', UA_PUBLIC, AUTH) for _ in range(2))
    assert first != second and first[16:20] == b'\x00\x00\x10\x00'


def test_the_vapid_key_is_made_once_and_kept(app, db_session):
    first = webpush.server_key()
    assert webpush.server_key() == first and len(webpush.unb64(first)) == 65


def test_the_vapid_header_verifies_against_the_key_it_names(app, db_session):
    header = webpush.vapid_header('https://push.example/send/abc?x=1', 'https://piefed.example')
    token = header.split('t=')[1].split(',')[0]
    key = header.split('k=')[1]
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.unb64(key))
    claims = jwt.decode(token, public, algorithms=['ES256'], audience='https://push.example')
    assert claims['sub'] == 'https://piefed.example' and key == webpush.server_key()


@pytest.mark.parametrize('endpoint, acceptable', [
    ('https://fcm.googleapis.com/fcm/send/abc', True),
    ('http://fcm.googleapis.com/fcm/send/abc', False),
    ('https://push.abcdefghijklmnop.onion/x', False),
    ('https://127.0.0.1/x', False),
    ('https://[::1]/x', False),
    ('https://10.0.0.5/x', False),
    ('https://test.piefed.local/x', False),
    ('https:///nohost', False),
    ('https://[x', False),
    ('https://push.example/' + 'a' * 2100, False),
    (None, False),
], ids=['public', 'http', 'onion', 'loopback', 'ipv6-loopback', 'private', 'local', 'no-host', 'broken', 'long',
        'none'])
def test_an_endpoint_must_be_https_public_and_not_ours(app, db_session, monkeypatch, endpoint, acceptable):
    monkeypatch.setattr(webpush, 'is_invalid_get_request_uri',
                        lambda uri: not uri.startswith('https://fcm.googleapis.com/'))   # no DNS in a test
    assert webpush.acceptable_endpoint(endpoint) is acceptable


def test_one_of_our_own_domains_is_never_an_endpoint(app, db_session, monkeypatch):
    db.session.add(SiteDomain(domain='shop.example', scheme='https'))
    db.session.commit()
    clear_domain_cache()
    monkeypatch.setattr(webpush, 'is_invalid_get_request_uri', lambda uri: False)
    assert not webpush.acceptable_endpoint('https://shop.example/push')


def test_sending_posts_the_encrypted_body_with_vapid(app, db_session, monkeypatch, http_mock):
    monkeypatch.setattr(webpush, 'is_invalid_get_request_uri', lambda uri: False)
    route = http_mock.post('https://push.example/send/abc').respond(201)
    status = webpush.send('https://push.example/send/abc', UA_PUBLIC, AUTH, b'{"title":"hi"}', 'https://p.example')
    assert status == 201
    request = route.calls.last.request
    assert request.headers['Content-Encoding'] == 'aes128gcm' and request.headers['TTL'] == str(webpush.TTL_SECONDS)
    assert request.headers['Authorization'].startswith('vapid t=')
    # the subscriber's own key reads it back
    body = request.content
    shared = _private(UA_PRIVATE).exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), body[21:86]))
    ikm = webpush._expand(webpush._extract(webpush.unb64(AUTH), shared),
                          b'WebPush: info\x00' + webpush.unb64(UA_PUBLIC) + body[21:86], 32)
    prk = webpush._extract(body[:16], ikm)
    plain = AESGCM(webpush._expand(prk, b'Content-Encoding: aes128gcm\x00', 16)).decrypt(
        webpush._expand(prk, b'Content-Encoding: nonce\x00', 12), body[86:], None)
    assert plain == b'{"title":"hi"}\x02'


def test_an_endpoint_no_longer_acceptable_is_not_sent_to(app, db_session, monkeypatch):
    monkeypatch.setattr(webpush, 'is_invalid_get_request_uri', lambda uri: True)
    assert webpush.send('https://push.example/x', UA_PUBLIC, AUTH, b'x', 'https://p.example') == 410
