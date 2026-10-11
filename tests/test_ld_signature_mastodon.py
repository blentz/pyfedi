"""LDSignature against a signature Mastodon really made.

Every other LD-signature test signs with pyfedi's own LDSignature.create_signature, so a verifier that disagrees
with Mastodon about what is hashed still passes them. The document below was relayed to hell.cloud on 2026-10-11
(a public Delete from a bot account), and it verifies with Mastodon's own algorithm (Ruby json-ld). Mastodon hashes
every key of the signature section except type, id and signatureValue, so its `expires` is part of the signature.
"""
import copy
from datetime import datetime

import pytest

from app.activitypub.signature import LDSignature, VerificationError, VerificationFormatError

MASTODON_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnHPQwmF4ObGgWnJ5KMAX
UELcrkWyhL04J+MX2b7K6Wtf+te+rptL1gSDBypC48ut1+F5/3MqhjE0k3M4xQ9w
FAiral86fydSLPkmbhoEOQ92mGPcu7cAJjvPjkqxgWTn8FzBqWpv+SxQIxcDtWrP
pMGjWrizQWHbpBhexrpCjrPyBXmb0jX65lVMXSIAKswomSYppGoR/7lwVbxhgx8I
IbCiChCgAUnjYIBjuf5C7cxGdxZw7dr6j1/bNc9IOqNHkKzBYl2Qo/Iuw3EfYbys
YZOikL2BbziXwe8AOXRqrT0rvBDrPPYu04gKNd/siJBJZwJq8kuoVzsyHa3fkkAC
dwIDAQAB
-----END PUBLIC KEY-----
"""

MASTODON_SIGNED_DELETE = {
    "@context": [
        "https://www.w3.org/ns/activitystreams",
        {"atomUri": "ostatus:atomUri", "ostatus": "http://ostatus.org#"},
        "https://w3id.org/security/v1",
    ],
    "actor": "https://lepoulsdumonde.com/users/ratp_ligne_13",
    "id": "https://lepoulsdumonde.com/users/ratp_ligne_13/statuses/117247501469422005#delete",
    "object": {
        "atomUri": "https://lepoulsdumonde.com/users/ratp_ligne_13/statuses/117247501469422005",
        "id": "https://lepoulsdumonde.com/users/ratp_ligne_13/statuses/117247501469422005",
        "type": "Tombstone",
    },
    "signature": {
        "created": "2026-10-11T02:11:06Z",
        "creator": "https://lepoulsdumonde.com/users/ratp_ligne_13#main-key",
        "expires": "2026-10-13T02:11:06Z",
        "signatureValue": "G6srpJkCrZxyr4fnQheT4MklyLq3UVvFEAC//6l3xqL0hT5VAFnqSBucsAJfyqumNI3wkrR66kdq+5NHQWxJVVn6qrIRB3s8"
                          "lk5Btfb+oT1zK2fc1JpLn1p4sGgOHhJ0/FH4bZM3jW7RbZ7zcuITdfxzkosoNKuK2eORulyEUUj7JX61Wrwt8c5Q7X/l"
                          "rJDEp2pvhHEk7I4aT9XU8/gnvEELL6vjM4idhnSpZT8Pcdyvh1JiZheH1M5KWSwfzMIFzXhVeZ1HUfyl8RMxV06hx+jW"
                          "DM26nl4WcFcSXuKgDBaIgO6jEkZFK3aePPaGmMlgWg1Vxs3g3DmFonyMeSMdmg==",
        "type": "RsaSignature2017",
    },
    "to": ["https://www.w3.org/ns/activitystreams#Public"],
    "type": "Delete",
}


def at(monkeypatch, when: str):
    """Pin the verifier's clock (naive UTC, as app.models.utcnow returns)."""
    monkeypatch.setattr('app.activitypub.signature.utcnow', lambda: datetime.fromisoformat(when))


def test_a_signature_mastodon_made_with_an_expiry_verifies(app, monkeypatch):
    at(monkeypatch, '2026-10-11T03:00:00')
    LDSignature.verify_signature(copy.deepcopy(MASTODON_SIGNED_DELETE), MASTODON_PUBLIC_KEY)


def test_a_signature_past_its_expiry_is_refused(app, monkeypatch):
    at(monkeypatch, '2026-10-13T02:30:00')
    with pytest.raises(VerificationError, match='Signature expired'):
        LDSignature.verify_signature(copy.deepcopy(MASTODON_SIGNED_DELETE), MASTODON_PUBLIC_KEY)


@pytest.mark.parametrize('alias', ['expiration', 'sec:expiration', 'https://w3id.org/security#expiration'])
def test_an_expiry_renamed_to_an_alias_is_still_enforced(app, monkeypatch, alias):
    """Every alias expands to the same sec:expiration triple, so renaming `expires` keeps the hash and the
    signature valid. The expiry must be read from what was signed, not from one JSON key."""
    at(monkeypatch, '2026-10-13T02:30:00')
    document = copy.deepcopy(MASTODON_SIGNED_DELETE)
    document['signature'][alias] = document['signature'].pop('expires')
    with pytest.raises(VerificationError, match='Signature expired'):
        LDSignature.verify_signature(document, MASTODON_PUBLIC_KEY)


def test_a_signature_just_past_its_expiry_is_within_clock_skew(app, monkeypatch):
    at(monkeypatch, '2026-10-13T02:14:00')
    LDSignature.verify_signature(copy.deepcopy(MASTODON_SIGNED_DELETE), MASTODON_PUBLIC_KEY)


def test_an_unreadable_expiry_is_a_format_error(app, monkeypatch):
    at(monkeypatch, '2026-10-11T03:00:00')
    document = copy.deepcopy(MASTODON_SIGNED_DELETE)
    document['signature']['expires'] = 'soon'
    with pytest.raises(VerificationFormatError, match='Invalid signature expiry'):
        LDSignature.verify_signature(document, MASTODON_PUBLIC_KEY)
