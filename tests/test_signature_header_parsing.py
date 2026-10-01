"""Characterisation of the Signature request header as app/activitypub/signature.py reads it.

D768 part 3, owner ruling: the module had two parsers for one header --
`HttpSignature.parse_signature`, which the verify path uses, and `signature_part`,
which `headers_from_request` used for the `(created)` and `(expires)`
pseudo-headers -- by acknowledged accident. These rows were written and passing
BEFORE the two were merged into one, and pass unchanged after: realistic headers
as Mastodon, Lemmy, Misskey and PieFed itself send them, rsa-sha256 and hs2019,
and an hs2019 signature over `(created)`/`(expires)` verified end to end with a
real key.
"""
import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from flask import request

from app.activitypub.signature import HttpSignature, VerificationError, signature_part
from tests.factories import a_keypair

SIG = base64.b64encode(b'not a real signature, only bytes to round-trip').decode('ascii')

SAMPLES = {
    'mastodon': (f'keyId="https://mastodon.social/users/Gargron#main-key",algorithm="rsa-sha256",'
                 f'headers="(request-target) host date digest content-type",signature="{SIG}"',
                 'https://mastodon.social/users/Gargron#main-key', 'rsa-sha256',
                 ['(request-target)', 'host', 'date', 'digest', 'content-type']),
    'lemmy': (f'keyId="https://lemmy.ml/u/nutomic#main-key",algorithm="hs2019",'
              f'headers="(request-target) content-type date digest host",signature="{SIG}"',
              'https://lemmy.ml/u/nutomic#main-key', 'hs2019',
              ['(request-target)', 'content-type', 'date', 'digest', 'host']),
    'misskey': (f'keyId="https://misskey.io/users/9abcdefghi#main-key",algorithm="rsa-sha256",'
                f'headers="(request-target) date host digest",signature="{SIG}"',
                'https://misskey.io/users/9abcdefghi#main-key', 'rsa-sha256',
                ['(request-target)', 'date', 'host', 'digest']),
    'piefed': (f'keyId="https://piefed.social/u/rimu#main-key",headers="(request-target) host date digest",'
               f'signature="{SIG}",algorithm="rsa-sha256"',
               'https://piefed.social/u/rimu#main-key', 'rsa-sha256',
               ['(request-target)', 'host', 'date', 'digest']),
    'hs2019 with pseudo-headers': (
        f'keyId="https://gts.example/users/a/main-key",algorithm="hs2019",created=1758240000,'
        f'expires=1758243600,headers="(request-target) (created) (expires) host digest",signature="{SIG}"',
        'https://gts.example/users/a/main-key', 'hs2019',
        ['(request-target)', '(created)', '(expires)', 'host', 'digest']),
}


@pytest.mark.parametrize('sample', SAMPLES.values(), ids=SAMPLES.keys())
def test_a_real_header_is_read_into_its_details(sample):
    header, key_id, algorithm, headers = sample

    details = HttpSignature.parse_signature(header)

    assert details == {'keyid': key_id, 'algorithm': algorithm, 'headers': headers,
                       'signature': base64.b64decode(SIG)}


@pytest.mark.parametrize('sample', SAMPLES.values(), ids=SAMPLES.keys())
def test_the_header_piefed_compiles_reads_back_the_same(sample):
    details = HttpSignature.parse_signature(sample[0])

    assert HttpSignature.parse_signature(HttpSignature.compile_signature(details)) == details


def test_the_pseudo_header_values_are_read_bare():
    header = SAMPLES['hs2019 with pseudo-headers'][0]

    assert (signature_part(header, 'created'), signature_part(header, 'expires')) == ('1758240000', '1758243600')
    assert signature_part(SAMPLES['mastodon'][0], 'created') == ''


def test_a_header_missing_its_signature_is_refused():
    with pytest.raises(VerificationError, match='Missing item from details'):
        HttpSignature.parse_signature('keyId="https://mastodon.social/users/Gargron#main-key",'
                                      'algorithm="rsa-sha256",headers="(request-target) host date"')


def _signed_inbox_request(app, signature_header_for):
    private_key, public_key = a_keypair()
    body = b'{"type": "Create"}'
    headers = {'Host': 'test.piefed.local', 'Digest': HttpSignature.calculate_digest(body),
               'Content-Type': 'application/activity+json'}
    names = ['(request-target)', '(created)', '(expires)', 'host', 'digest']
    cleartext = '\n'.join(['(request-target): post /inbox', '(created): 1758240000',
                           '(expires): 1758243600', 'host: test.piefed.local',
                           f'digest: {headers["Digest"]}'])
    key = serialization.load_pem_private_key(private_key.encode('ascii'), password=None)
    signature = base64.b64encode(key.sign(cleartext.encode('ascii'), padding.PKCS1v15(),
                                          hashes.SHA256())).decode('ascii')
    headers['Signature'] = signature_header_for(' '.join(names), signature)
    return app.test_request_context('/inbox', method='POST', headers=headers, data=body), public_key


@pytest.mark.parametrize('shape', [
    lambda names, sig: (f'keyId="https://gts.example/users/a/main-key",algorithm="hs2019",created=1758240000,'
                        f'expires=1758243600,headers="{names}",signature="{sig}"'),
    lambda names, sig: (f'keyId="https://gts.example/users/a/main-key",algorithm="hs2019",created="1758240000",'
                        f'expires="1758243600",headers="{names}",signature="{sig}"'),
], ids=['bare numbers', 'quoted numbers'])
def test_an_hs2019_signature_over_created_and_expires_verifies(app, shape):
    context, public_key = _signed_inbox_request(app, shape)
    with context:
        assert HttpSignature.verify_request(request, public_key) is True


def test_a_signature_whose_created_was_altered_does_not_verify(app):
    context, public_key = _signed_inbox_request(
        app, lambda names, sig: (f'keyId="https://gts.example/users/a/main-key",algorithm="hs2019",'
                                 f'created=1758240001,expires=1758243600,headers="{names}",signature="{sig}"'))
    with context:
        with pytest.raises(VerificationError, match='Signature mismatch'):
            HttpSignature.verify_request(request, public_key)
