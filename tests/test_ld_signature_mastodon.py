"""LDSignature against a signature Mastodon really made.

Every other LD-signature test signs with pyfedi's own LDSignature.create_signature, so a verifier that disagrees
with Mastodon about what is hashed still passes them. The document below was relayed to hell.cloud on 2026-10-11
(a public Delete from a bot account), and it verifies with Mastodon's own algorithm (Ruby json-ld). Mastodon hashes
every key of the signature section except type, id and signatureValue, so its `expires` is part of the signature.
"""
import copy
from datetime import datetime

import pytest

from app.activitypub.ld_contexts import MASTODON_CONTEXT_TERMS, check_ld_coverage
from app.activitypub.signature import LDSignature, VerificationError, VerificationFormatError
from tests.factories import a_keypair

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


def test_an_expiry_without_a_timezone_is_read_as_utc(app, monkeypatch):
    """A naive `expires` is taken as UTC, as `created` is, so one that has passed is still refused."""
    at(monkeypatch, '2026-10-13T02:30:00')
    document = copy.deepcopy(MASTODON_SIGNED_DELETE)
    document['signature']['expires'] = '2026-10-13T02:11:06'
    with pytest.raises(VerificationError, match='Signature expired'):
        LDSignature.verify_signature(document, MASTODON_PUBLIC_KEY)


def test_a_signature_section_json_ld_cannot_expand_is_a_format_error(app, monkeypatch):
    """The expiry is read from the expanded options, so options that do not expand cannot be checked for one."""
    at(monkeypatch, '2026-10-11T03:00:00')
    document = copy.deepcopy(MASTODON_SIGNED_DELETE)
    document['signature']['@id'] = 5
    with pytest.raises(VerificationFormatError, match='Invalid signature section'):
        LDSignature.verify_signature(document, MASTODON_PUBLIC_KEY)


# --- a Mastodon Create with its usual inline term map ---
# No genuine LD-signed Mastodon Create was at hand, so this one is signed with LDSignature.create_signature. Its
# shape is a Mastodon 4.5 Note activity (ActivityPub::NoteSerializer: hashtag, custom emoji, mention, image
# attachment with blurhash and focalPoint, replies/likes/shares collections, interactionPolicy), and every inline
# term is copied from MASTODON_CONTEXT_TERMS, the copy of Mastodon's CONTEXT_EXTENSION_MAP. The context order is
# the one the real Delete above carries: activitystreams, the inline map, security/v1.

MASTODON_ACTOR = 'https://mastodon.example/users/alice'
MASTODON_STATUS = f'{MASTODON_ACTOR}/statuses/115000000000000001'
MASTODON_CREATE_TERMS = ('ostatus', 'atomUri', 'inReplyToAtomUri', 'conversation', 'sensitive', 'toot',
                         'votersCount', 'blurhash', 'focalPoint', 'Hashtag', 'Emoji', 'gts', 'interactionPolicy',
                         'canQuote', 'automaticApproval', 'manualApproval')


def mastodon_create() -> dict:
    public = 'https://www.w3.org/ns/activitystreams#Public'
    followers = f'{MASTODON_ACTOR}/followers'
    return {
        '@context': [
            'https://www.w3.org/ns/activitystreams',
            {term: copy.deepcopy(MASTODON_CONTEXT_TERMS[term]) for term in MASTODON_CREATE_TERMS},
            'https://w3id.org/security/v1',
        ],
        'id': f'{MASTODON_STATUS}/activity',
        'type': 'Create',
        'actor': MASTODON_ACTOR,
        'published': '2026-10-11T02:00:00Z',
        'to': [public],
        'cc': [followers, 'https://piefed.example/u/bob'],
        'object': {
            'id': MASTODON_STATUS,
            'type': 'Note',
            'summary': None,
            'inReplyTo': None,
            'published': '2026-10-11T02:00:00Z',
            'url': 'https://mastodon.example/@alice/115000000000000001',
            'attributedTo': MASTODON_ACTOR,
            'to': [public],
            'cc': [followers, 'https://piefed.example/u/bob'],
            'sensitive': False,
            'atomUri': MASTODON_STATUS,
            'inReplyToAtomUri': None,
            'conversation': 'tag:mastodon.example,2026-10-11:objectId=1:objectType=Conversation',
            'content': '<p>Hello <span class="h-card"><a href="https://piefed.example/u/bob">@bob</a></span> '
                       ':blobcat: <a href="https://mastodon.example/tags/fediverse" rel="tag">#fediverse</a></p>',
            'contentMap': {'en': '<p>Hello @bob :blobcat: #fediverse</p>'},
            'updated': '2026-10-11T02:05:00Z',
            'attachment': [{
                'type': 'Document',
                'mediaType': 'image/png',
                'url': 'https://mastodon.example/system/media_attachments/files/1/original/1.png',
                'name': 'a cat',
                'blurhash': 'UBL_:rOpGG-oBUNG,qRj2so|=eE1w^n4S5NH',
                'focalPoint': [0.0, 0.25],
                'width': 640,
                'height': 480,
            }],
            'tag': [
                {'type': 'Mention', 'href': 'https://piefed.example/u/bob', 'name': '@bob@piefed.example'},
                {'type': 'Hashtag', 'href': 'https://mastodon.example/tags/fediverse', 'name': '#fediverse'},
                {'id': 'https://mastodon.example/emojis/1', 'type': 'Emoji', 'name': ':blobcat:',
                 'updated': '2026-01-01T00:00:00Z',
                 'icon': {'type': 'Image', 'mediaType': 'image/png',
                          'url': 'https://mastodon.example/system/custom_emojis/images/1/original/1.png'}},
            ],
            'interactionPolicy': {'canQuote': {'automaticApproval': [public], 'manualApproval': []}},
            'replies': {
                'id': f'{MASTODON_STATUS}/replies',
                'type': 'Collection',
                'first': {'type': 'CollectionPage',
                          'next': f'{MASTODON_STATUS}/replies?only_other_accounts=true&page=true',
                          'partOf': f'{MASTODON_STATUS}/replies', 'items': []},
            },
            'likes': {'id': f'{MASTODON_STATUS}/likes', 'type': 'Collection', 'totalItems': 3},
            'shares': {'id': f'{MASTODON_STATUS}/shares', 'type': 'Collection', 'totalItems': 1},
        },
    }


def test_a_mastodon_create_with_its_inline_term_map_verifies(app):
    """LDSignature.verify_signature runs check_ld_coverage before the hash, so this passing proves every key of a
    usual Mastodon Create (toot:, sensitive, Hashtag, Emoji, blurhash, focalPoint, ostatus:, interactionPolicy) is
    covered by its graph, not only that the signature matches."""
    private_key, public_key = a_keypair()
    document = mastodon_create()
    check_ld_coverage(copy.deepcopy(document))
    document['signature'] = LDSignature.create_signature(document, private_key, f'{MASTODON_ACTOR}#main-key')

    LDSignature.verify_signature(document, public_key)

    assert document['object']['tag'][2]['type'] == 'Emoji' and document['object']['sensitive'] is False


def test_a_mastodon_create_whose_content_is_changed_after_signing_is_refused(app):
    private_key, public_key = a_keypair()
    document = mastodon_create()
    document['signature'] = LDSignature.create_signature(document, private_key, f'{MASTODON_ACTOR}#main-key')
    document['object']['content'] = '<p>forged</p>'

    with pytest.raises(VerificationError, match='Signature mismatch'):
        LDSignature.verify_signature(document, public_key)
