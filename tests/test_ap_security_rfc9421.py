"""RFC 9421 / RFC 9530 (S5): the standalone module, against the RFC's own vectors and round trips."""
import base64
import hashlib
import json
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding

from app.activitypub import signature_rfc9421 as rfc9421
from app.activitypub.signature import VerificationError as ReExportedError
from app.activitypub.signature_errors import VerificationError, VerificationFormatError
from tests.factories import a_keypair

VECTORS = json.loads((Path(__file__).parent / 'fixtures' / 'rfc9421' / 'appendix_b.json').read_text())
URL = 'https://peer.example/inbox'
BODY = b'{"type": "Create"}'
NOW = 1_800_000_000


def _pem(public_key) -> str:
    return public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _rsa_private(pem):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _custom(covered, private_sign, *, extra='', headers=None, created=NOW, label='sig1', url=URL, method='POST'):
    """Signature headers over `covered`, signed by `private_sign(base_bytes) -> bytes`."""
    headers = dict(headers or {})
    params = '(' + ' '.join(f'"{c}"' for c in covered) + f');created={created};keyid="k"{extra}'
    base = rfc9421.signature_base(covered, params, method, url, headers)
    headers['Signature-Input'] = f'{label}={params}'
    headers['Signature'] = f'{label}=:{base64.b64encode(private_sign(base.encode())).decode()}:'
    return headers


def _v15(private_pem):
    key = _rsa_private(private_pem)
    return lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256())


class TestErrorsModule:

    def test_signature_re_exports_the_same_class(self):
        assert ReExportedError is VerificationError
        assert issubclass(VerificationFormatError, VerificationError)


class TestRfcVectors:

    @pytest.mark.parametrize('case', VECTORS['cases'], ids=lambda c: c['name'])
    def test_appendix_b_verifies(self, case):
        request = VECTORS['request']
        headers = dict(request['headers'], **{'Signature-Input': case['signature_input'],
                                              'Signature': case['signature']})
        try:
            covered = rfc9421.verify(request['method'], request['url'], headers, request['body'].encode(),
                                     VECTORS['keys'][case['key']], now=VECTORS['created'])
        except VerificationFormatError as e:
            if case['name'] == 'B.2.1' and 'usable PEM' in str(e):
                pytest.skip('this cryptography build cannot load an id-RSASSA-PSS SPKI key; B.2.6 still runs')
            raise
        assert isinstance(covered, frozenset)

    def test_a_tampered_vector_fails(self):
        case = next(c for c in VECTORS['cases'] if c['name'] == 'B.2.6')
        request = VECTORS['request']
        headers = dict(request['headers'], **{'Signature-Input': case['signature_input'],
                                              'Signature': case['signature'], 'Content-Type': 'text/plain'})
        with pytest.raises(VerificationError, match='Signature mismatch'):
            rfc9421.verify(request['method'], request['url'], headers, request['body'].encode(),
                           VECTORS['keys'][case['key']], now=VECTORS['created'])


class TestRoundTrip:

    def test_a_post_with_a_body(self):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('post', URL, BODY, private_key, 'https://test.piefed.local/actor#main-key', created=NOW)
        assert headers['Content-Digest'] == rfc9421.content_digest(BODY)
        assert headers['Signature-Input'].startswith('sig1=("@method" "@target-uri" "@authority" "content-digest")')
        assert 'alg="rsa-v1_5-sha256"' in headers['Signature-Input']
        covered = rfc9421.verify('POST', URL, headers, BODY, public_key, rfc9421.RFC9421_POST_REQUIRED, now=NOW)
        assert covered == frozenset({'@method', '@target-uri', '@authority', 'content-digest'})

    def test_a_get_without_a_body(self):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('get', URL, None, private_key, 'k', created=NOW)
        assert 'Content-Digest' not in headers
        assert rfc9421.verify('GET', URL, headers, b'', public_key, rfc9421.RFC9421_GET_REQUIRED, now=NOW) == \
            frozenset({'@method', '@target-uri', '@authority'})

    def test_created_defaults_to_now(self):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('get', URL, None, private_key, 'k')
        assert rfc9421.verify('GET', URL, headers, b'', public_key)

    def test_a_key_id_is_escaped(self):
        private_key, public_key = a_keypair()
        headers = rfc9421.sign('get', URL, None, private_key, 'a"b\\c', created=NOW)
        assert rfc9421.parse_key_id(headers) == 'a"b\\c'
        assert rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    def test_a_non_ascii_key_id_cannot_be_signed(self):
        with pytest.raises(ValueError):
            rfc9421.sign('get', URL, None, a_keypair()[0], 'clé')


class TestTampering:

    @pytest.fixture
    def signed(self):
        private_key, public_key = a_keypair()
        return rfc9421.sign('post', URL, BODY, private_key, 'k', created=NOW), public_key

    @pytest.mark.parametrize('method, url', [('POST', 'https://other.example/inbox'), ('PUT', URL),
                                             ('POST', 'https://peer.example/other')])
    def test_a_different_request_line_fails(self, signed, method, url):
        headers, public_key = signed
        with pytest.raises(VerificationError, match='Signature mismatch'):
            rfc9421.verify(method, url, headers, BODY, public_key, now=NOW)

    def test_a_different_body_fails_its_digest(self, signed):
        headers, public_key = signed
        with pytest.raises(VerificationFormatError, match='Content-Digest is incorrect'):
            rfc9421.verify('POST', URL, headers, b'{}', public_key, now=NOW)

    def test_a_replaced_digest_header_fails_the_signature(self, signed):
        headers, public_key = signed
        headers = dict(headers, **{'Content-Digest': rfc9421.content_digest(b'{}')})
        with pytest.raises(VerificationError):
            rfc9421.verify('POST', URL, headers, b'{}', public_key, now=NOW)

    @pytest.mark.parametrize('now, message', [(NOW + 3601, 'Signature is too old'),
                                              (NOW - 301, 'Signature is from the future')])
    def test_created_outside_the_window_fails(self, signed, now, message):
        headers, public_key = signed
        with pytest.raises(VerificationError, match=message):
            rfc9421.verify('POST', URL, headers, BODY, public_key, now=now)

    @pytest.mark.parametrize('now', [NOW + 3600, NOW - 300])
    def test_created_at_the_window_edges_passes(self, signed, now):
        headers, public_key = signed
        assert rfc9421.verify('POST', URL, headers, BODY, public_key, now=now)

    def test_a_missing_required_component_fails(self):
        private_key, public_key = a_keypair()
        headers = _custom(['@method', '@target-uri'], _v15(private_key))
        with pytest.raises(VerificationError, match='unsigned required component: content-digest'):
            rfc9421.verify('POST', URL, headers, b'', public_key, rfc9421.RFC9421_POST_REQUIRED, now=NOW)

    def test_authority_and_path_count_as_the_target_uri(self):
        private_key, public_key = a_keypair()
        headers = _custom(['@method', '@authority', '@path'], _v15(private_key), method='GET')
        assert rfc9421.verify('GET', URL, headers, b'', public_key, rfc9421.RFC9421_GET_REQUIRED, now=NOW) == \
            frozenset({'@method', '@authority', '@path'})


class TestLabels:

    def test_the_first_label_that_verifies_wins(self):
        private_key, public_key = a_keypair()
        good = _custom(['@method'], _v15(private_key), label='good', method='GET')
        headers = {'Signature-Input': f'bad=("@method");created={NOW};keyid="k", ' + good['Signature-Input'],
                   'Signature': 'bad=:YWJj:, ' + good['Signature']}
        assert rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW) == frozenset({'@method'})

    def test_when_none_verifies_the_last_error_is_raised(self):
        _, public_key = a_keypair()
        headers = {'Signature-Input': f'a=("@method");created={NOW}, b=("@method")',
                   'Signature': 'a=:YWJj:, b=:YWJj:'}
        with pytest.raises(VerificationFormatError, match='no created time'):
            rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    @pytest.mark.parametrize('signature', ['', 'other=:YWJj:', 'sig1="text"'])
    def test_a_label_without_signature_bytes_fails(self, signature):
        _, public_key = a_keypair()
        headers = {'Signature-Input': f'sig1=("@method");created={NOW}', 'Signature': signature}
        with pytest.raises(VerificationFormatError, match='No signature for label sig1'):
            rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    def test_no_signature_input_fails(self):
        with pytest.raises(VerificationFormatError, match='No Signature-Input header present'):
            rfc9421.verify('GET', URL, {}, b'', a_keypair()[1], now=NOW)


class TestCoveredComponents:

    @pytest.mark.parametrize('signature_input, message', [
        ('sig1=:YWJj:', 'not an inner list'),
        ('sig1=(1)', 'Covered component is not a string'),
        ('sig1=("content-digest";sf)', 'Component parameters are not supported'),
        ('sig1=("@method" "@method")', 'Duplicate component @method'),
    ])
    def test_malformed_component_lists_fail(self, signature_input, message):
        headers = {'Signature-Input': f'{signature_input};created={NOW}', 'Signature': 'sig1=:YWJj:'}
        with pytest.raises(VerificationFormatError, match=message):
            rfc9421.verify('GET', URL, headers, b'', a_keypair()[1], now=NOW)

    def test_an_unsupported_derived_component_fails(self):
        with pytest.raises(VerificationFormatError, match='Unsupported derived component @query'):
            rfc9421.signature_base(['@query'], '()', 'GET', URL, {})

    def test_a_missing_covered_header_fails(self):
        with pytest.raises(VerificationError, match='Covered header date is missing'):
            rfc9421.signature_base(['date'], '()', 'GET', URL, {})

    def test_the_base_for_each_component(self):
        base = rfc9421.signature_base(['@method', '@target-uri', '@authority', '@path', 'content-type'], '(x)',
                                      'post', 'https://Peer.Example:8443?q=1', {'CONTENT-TYPE': ' a/b '})
        assert base == ('"@method": POST\n"@target-uri": https://Peer.Example:8443?q=1\n'
                        '"@authority": peer.example:8443\n"@path": /\n"content-type": a/b\n'
                        '"@signature-params": (x)')

    @pytest.mark.parametrize('url', ['https://peer.example:443/x', 'http://peer.example:80/x'])
    def test_a_default_port_is_dropped_from_the_authority(self, url):
        assert rfc9421.signature_base(['@authority'], '()', 'GET', url, {}).startswith('"@authority": peer.example\n')

    def test_normalise_covered(self):
        assert rfc9421.normalise_covered(['@authority', '@path']) == frozenset({'@authority', '@path', '@target-uri'})
        assert rfc9421.normalise_covered(['@authority']) == frozenset({'@authority'})


class TestKeysAndAlgorithms:

    def test_rsa_without_alg_accepts_pkcs1(self):
        private_key, public_key = a_keypair()
        headers = _custom(['@method'], _v15(private_key), method='GET')
        assert rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    @pytest.mark.parametrize('extra', ['', ';alg="rsa-pss-sha512"'])
    def test_rsa_pss_sha512(self, extra):
        private_key, public_key = a_keypair()
        key = _rsa_private(private_key)
        pss = lambda data: key.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64),
                                    hashes.SHA512())
        headers = _custom(['@method'], pss, extra=extra, method='GET')
        assert rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    def test_an_rsa_signature_matching_neither_scheme_fails(self):
        _, public_key = a_keypair()
        headers = {'Signature-Input': f'sig1=("@method");created={NOW}', 'Signature': 'sig1=:YWJj:'}
        with pytest.raises(VerificationError, match='Signature mismatch'):
            rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    @pytest.mark.parametrize('extra', ['', ';alg="ed25519"'])
    def test_ed25519(self, extra):
        key = ed25519.Ed25519PrivateKey.generate()
        headers = _custom(['@method'], key.sign, extra=extra, method='GET')
        assert rfc9421.verify('GET', URL, headers, b'', _pem(key.public_key()), now=NOW)

    def test_ed25519_mismatch(self):
        key, other = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
        headers = _custom(['@method'], key.sign, method='GET')
        with pytest.raises(VerificationError, match='Signature mismatch'):
            rfc9421.verify('GET', URL, headers, b'', _pem(other.public_key()), now=NOW)

    def test_ed25519_with_an_rsa_alg_fails(self):
        key = ed25519.Ed25519PrivateKey.generate()
        headers = _custom(['@method'], key.sign, extra=';alg="rsa-v1_5-sha256"', method='GET')
        with pytest.raises(VerificationFormatError, match='does not match an ed25519 key'):
            rfc9421.verify('GET', URL, headers, b'', _pem(key.public_key()), now=NOW)

    def test_an_unsupported_alg_fails(self):
        private_key, public_key = a_keypair()
        headers = _custom(['@method'], _v15(private_key), extra=';alg="hmac-sha256"', method='GET')
        with pytest.raises(VerificationFormatError, match='Unsupported algorithm hmac-sha256'):
            rfc9421.verify('GET', URL, headers, b'', public_key, now=NOW)

    def test_an_unsupported_key_type_fails(self):
        key = ec.generate_private_key(ec.SECP256R1())
        headers = {'Signature-Input': f'sig1=("@method");created={NOW}', 'Signature': 'sig1=:YWJj:'}
        with pytest.raises(VerificationFormatError, match='Unsupported key type'):
            rfc9421.verify('GET', URL, headers, b'', _pem(key.public_key()), now=NOW)

    @pytest.mark.parametrize('pem', ['junk', None])
    def test_an_unusable_public_key_fails(self, pem):
        headers = {'Signature-Input': f'sig1=("@method");created={NOW}', 'Signature': 'sig1=:YWJj:'}
        with pytest.raises(VerificationFormatError, match='Public key is not a usable PEM'):
            rfc9421.verify('GET', URL, headers, b'', pem, now=NOW)


class TestContentDigest:

    def test_the_header_format(self):
        expected = base64.b64encode(hashlib.sha256(BODY).digest()).decode()
        assert rfc9421.content_digest(BODY) == f'sha-256=:{expected}:'

    def test_an_absent_header_passes(self):
        assert rfc9421.check_content_digest({}, BODY) is None

    def test_sha512_and_unknown_algorithms_alongside(self):
        sha512 = base64.b64encode(hashlib.sha512(BODY).digest()).decode()
        assert rfc9421.check_content_digest({'content-digest': f'md5=:YWJj:, sha-512=:{sha512}:'}, BODY) is None

    @pytest.mark.parametrize('value, message', [('md5=:YWJj:', 'no supported algorithm'),
                                                ('sha-256="text"', 'Content-Digest is incorrect'),
                                                ('sha-256=:YWJj:', 'Content-Digest is incorrect')])
    def test_bad_digests(self, value, message):
        with pytest.raises(VerificationFormatError, match=message):
            rfc9421.check_content_digest({'Content-Digest': value}, BODY)


class TestParseKeyId:

    @pytest.mark.parametrize('headers, expected', [
        ({'Signature-Input': 'sig1=();keyid="https://a.example/u#k"'}, 'https://a.example/u#k'),
        ({}, None),
        ({'Signature-Input': 'Bad'}, None),
        ({'Signature-Input': 'sig1=()'}, None),
        ({'Signature-Input': 'sig1=();keyid=1'}, None),
    ])
    def test_parse_key_id(self, headers, expected):
        assert rfc9421.parse_key_id(headers) == expected


class TestStructuredFieldParser:

    def test_a_well_formed_dictionary(self):
        parsed = rfc9421.parse_dictionary(' a=("x" "y\\"z");p=1;q=tok, b=:YWJj:;r="s" , c=-5')
        assert parsed['a'] == ([('x', {}), ('y"z', {})], {'p': 1, 'q': 'tok'}, '("x" "y\\"z");p=1;q=tok')
        assert parsed['b'] == (b'abc', {'r': 's'}, ':YWJj:;r="s"')
        assert parsed['c'][0] == -5
        assert rfc9421.parse_dictionary('') == {}

    @pytest.mark.parametrize('text, message', [
        ('Sig1=("@method")', 'key'),
        ('sig1', 'expected "="'),
        ('sig1=:YQ==: sig2=:YQ==:', 'expected ","'),
        ('sig1=:YQ==:, ', 'trailing ","'),
        ('sig1=("a""b")', 'expected " " or ")"'),
        ('sig1=("a"', 'expected " " or ")"'),
        ('sig1=();=1', 'parameter key'),
        ('sig1=();created=-', 'integer'),
        ('sig1=();alg=?1', 'item'),
        ('sig1=("abc', 'unterminated string'),
        ('sig1=("\\x")', 'bad escape'),
        ('sig1=("\u00e9")', 'non-printable character'),
        ('sig1=:YQ==', 'unterminated byte sequence'),
        ('sig1=:Y!==:', 'bad base64'),
    ])
    def test_malformed_input_is_a_format_error(self, text, message):
        with pytest.raises(VerificationFormatError, match=re.escape(message)):
            rfc9421.parse_dictionary(text)
