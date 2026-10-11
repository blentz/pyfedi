"""LDSignature against a signature Mastodon really made.

Every other LD-signature test signs with pyfedi's own LDSignature.create_signature, so a verifier that disagrees
with Mastodon about what is hashed still passes them. The document below was relayed to hell.cloud on 2026-10-11
(a public Delete from a bot account), and it verifies with Mastodon's own algorithm (Ruby json-ld). Mastodon hashes
every key of the signature section except type, id and signatureValue, so its `expires` is part of the signature.
"""
import copy

from app.activitypub.signature import LDSignature

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


def test_a_signature_mastodon_made_with_an_expiry_verifies(app):
    LDSignature.verify_signature(copy.deepcopy(MASTODON_SIGNED_DELETE), MASTODON_PUBLIC_KEY)
