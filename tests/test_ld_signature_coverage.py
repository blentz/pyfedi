"""Ruling R-LDHARDEN: an LD signature is honoured only for documents whose JSON it actually covers.

RsaSignature2017 signs the URDNA2015 graph, pyfedi reads the JSON keys. Every tamper below keeps a genuine
signature (production's own `LDSignature.create_signature`) and is refused by `LDSignature.verify_signature`,
at the unit level and at each inbox path that accepts a document on an LD signature.
"""
import copy
import json

import pytest
from pyld import jsonld
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.activitypub.ld_contexts import (CANONICAL_LD_TERMS, MASTODON_CONTEXT_TERMS, PYFEDI_CONTEXT_TERMS,
                                         LDCoverageError, check_ld_coverage, ld_document_loader)
from app.activitypub.signature import HttpSignature, LDSignature, VerificationError, VerificationFormatError, \
    default_context
from app.models import ActivityPubLog
from tests.conftest import unsigned_but_precheck_clean_headers
from tests.factories import a_keypair, inbox_activity

AS = 'https://www.w3.org/ns/activitystreams'
SEC = 'https://w3id.org/security/v1'
AS_CONTENT = 'https://www.w3.org/ns/activitystreams#content'
ACTOR = 'https://m.example/users/alice'
NOTE = f'{ACTOR}/statuses/1'

# Mastodon's own shape: the AS context, the inline map context_helper.rb builds for what the object uses, and
# security/v1 appended by LinkedDataSignature#sign.
MASTODON_INLINE = {'ostatus': 'http://ostatus.org#', 'atomUri': 'ostatus:atomUri',
                   'inReplyToAtomUri': 'ostatus:inReplyToAtomUri', 'conversation': 'ostatus:conversation',
                   'sensitive': 'as:sensitive', 'toot': 'http://joinmastodon.org/ns#',
                   'votersCount': 'toot:votersCount', 'blurhash': 'toot:blurhash',
                   'focalPoint': {'@container': '@list', '@id': 'toot:focalPoint'}, 'Hashtag': 'as:Hashtag',
                   'Emoji': 'toot:Emoji'}


def mastodon_create():
    public = 'https://www.w3.org/ns/activitystreams#Public'
    return {
        '@context': [AS, copy.deepcopy(MASTODON_INLINE), SEC],
        'id': f'{NOTE}/activity', 'type': 'Create', 'actor': ACTOR, 'published': '2026-10-10T00:00:00Z',
        'to': [public], 'cc': [f'{ACTOR}/followers'],
        'object': {
            'id': NOTE, 'type': 'Note', 'summary': None, 'inReplyTo': None, 'published': '2026-10-10T00:00:00Z',
            'url': 'https://m.example/@alice/1', 'attributedTo': ACTOR, 'to': [public], 'cc': [],
            'sensitive': False, 'atomUri': NOTE, 'inReplyToAtomUri': None,
            'conversation': 'tag:m.example,2026-10-10:objectId=1:objectType=Conversation',
            'content': '<p>what alice wrote</p>', 'contentMap': {'en': '<p>what alice wrote</p>'},
            'attachment': [{'type': 'Document', 'mediaType': 'image/png', 'url': 'https://m.example/a.png',
                            'name': None, 'blurhash': 'UABC', 'focalPoint': [0.0, 0.0], 'width': 10, 'height': 10}],
            'tag': [{'type': 'Mention', 'href': 'https://x.example/u/bob', 'name': '@bob@x.example'},
                    {'type': 'Hashtag', 'href': 'https://m.example/tags/x', 'name': '#x'},
                    {'id': 'https://m.example/emojis/1', 'type': 'Emoji', 'name': ':e:',
                     'updated': '2026-10-10T00:00:00Z',
                     'icon': {'type': 'Image', 'mediaType': 'image/png', 'url': 'https://m.example/e.png'}}],
            'replies': {'id': f'{NOTE}/replies', 'type': 'Collection',
                        'first': {'type': 'CollectionPage', 'next': f'{NOTE}/replies?page=true',
                                  'partOf': f'{NOTE}/replies', 'items': []}},
        },
    }


def plain_create():
    """No extension map: 'sensitive' and 'stickied' are terms no context defines."""
    document = mastodon_create()
    document['@context'] = [AS, SEC]
    del document['object']['sensitive'], document['object']['atomUri'], document['object']['inReplyToAtomUri']
    del document['object']['conversation'], document['object']['attachment'], document['object']['tag']
    return document


def signed(document, private_key):
    document['signature'] = LDSignature.create_signature(document, private_key, f'{ACTOR}#main-key')
    return document


def graph_hash(document):
    return LDSignature.normalized_hash({k: v for k, v in document.items() if k != 'signature'})


# --- the three tamper classes -------------------------------------------------------------------------------

def tamper_unmapped_sensitive(document):
    document['object']['sensitive'] = True


def tamper_unmapped_stickied(document):
    document['object']['stickied'] = True


def tamper_inline_content_remap(document):
    note = document['object']
    note['@context'] = {'content': '_:ignored', 'realcontent': 'as:content'}
    note['realcontent'], note['content'] = note['content'], '<p>forged</p>'


def tamper_top_level_context_remap(document):
    document['@context'].append({'content': '_:ignored', 'realcontent': AS_CONTENT})
    note = document['object']
    note['realcontent'], note['content'] = note['content'], '<p>forged</p>'


def tamper_attributed_to_remap(document):
    document['@context'].append({'attributedTo': '_:ignored',
                                 'realAttributedTo': {'@id': 'as:attributedTo', '@type': '@id'}})
    note = document['object']
    note['realAttributedTo'], note['attributedTo'] = note['attributedTo'], 'https://evil.example/users/mallory'


def tamper_actor_remap(document):
    document['@context'].append({'actor': '_:ignored', 'realactor': {'@id': 'as:actor', '@type': '@id'}})
    document['realactor'], document['actor'] = document['actor'], 'https://evil.example/users/mallory'


def tamper_alias_without_redefinition(document):
    """No canonical term is redefined: 'content' moves under a new alias of as:content, so pyfedi reads none."""
    note = document['object']
    note['@context'] = {'realcontent': 'as:content'}
    note['realcontent'] = note.pop('content')


def tamper_nested_id_alias(document):
    note = document['object']
    note['@context'] = {'id': '_:x', 'realid': '@id'}
    note['realid'], note['id'] = note['id'], 'https://evil.example/objects/1'


SAME_GRAPH_TAMPERS = [tamper_unmapped_sensitive, tamper_unmapped_stickied, tamper_inline_content_remap,
                      tamper_top_level_context_remap, tamper_attributed_to_remap, tamper_actor_remap,
                      tamper_alias_without_redefinition]


@pytest.mark.parametrize('tamper', SAME_GRAPH_TAMPERS)
def test_a_tamper_that_keeps_the_signed_graph_is_refused(app, no_network_ld_signing, tamper):
    private_key, public_key = a_keypair()
    original = signed(plain_create(), private_key)
    tampered = copy.deepcopy(original)
    tamper(tampered)
    assert graph_hash(tampered) == graph_hash(original)        # the signature genuinely still matches

    with pytest.raises(VerificationFormatError, match='Signature does not cover the document'):
        LDSignature.verify_signature(tampered, public_key)


def test_a_nested_context_aliasing_id_is_refused(app, no_network_ld_signing):
    private_key, public_key = a_keypair()
    document = signed(plain_create(), private_key)
    tamper_nested_id_alias(document)

    with pytest.raises(VerificationFormatError, match="redefines 'id'"):
        LDSignature.verify_signature(document, public_key)


def test_the_untampered_document_still_verifies(app, no_network_ld_signing):
    private_key, public_key = a_keypair()
    assert LDSignature.verify_signature(signed(plain_create(), private_key), public_key) is None


def test_a_genuine_mastodon_shaped_create_with_its_inline_term_map_verifies(app, no_network_ld_signing):
    private_key, public_key = a_keypair()
    document = signed(mastodon_create(), private_key)

    assert LDSignature.verify_signature(document, public_key) is None


def test_mastodon_and_pyfedi_extension_terms_are_canonical(app):
    for term_map in (MASTODON_CONTEXT_TERMS, PYFEDI_CONTEXT_TERMS):
        check_ld_coverage({'@context': [AS, SEC, term_map], 'id': NOTE, 'type': 'Note'})
    assert 'sensitive' in CANONICAL_LD_TERMS and 'toot' not in CANONICAL_LD_TERMS


def test_pyfedis_full_context_verifies_and_is_sent_as_a_copy(app, monkeypatch, no_network_ld_signing):
    monkeypatch.setitem(app.config, 'FULL_AP_CONTEXT', True)
    private_key, public_key = a_keypair()
    document = plain_create()
    document['@context'] = default_context()
    document['object'].update(sensitive=True, stickied=False, language={'identifier': 'en', 'name': 'English'})

    assert LDSignature.verify_signature(signed(document, private_key), public_key) is None
    document['@context'][2]['moderators']['@id'] = 'changed'
    assert PYFEDI_CONTEXT_TERMS['moderators']['@id'] == 'lemmy:moderators'


# --- the gate, rule by rule -------------------------------------------------------------------------------

def note(**fields):
    return {'@context': [AS, SEC], 'id': NOTE, 'type': 'Note', **fields}


@pytest.mark.parametrize('document, reason', [
    (note(**{'@context': [AS, 'https://evil.example/ctx']}), 'is not bundled'),
    (note(object={'@context': 'https://evil.example/ctx', 'id': NOTE}), 'is not bundled'),
    (note(tag=[{'@context': [AS, {'content': '_:x'}], 'type': 'Note'}]), "redefines 'content'"),
    (note(**{'@context': [AS, None]}), 'other than a URL or a term map'),
    (note(**{'@context': [[AS]]}), 'other than a URL or a term map'),
    (note(**{'@context': [AS, {'@vocab': 'as:'}]}), "'@vocab' is not a plain term"),
    (note(**{'@context': [AS, {'as:x': 'as:x'}]}), "'as:x' is not a plain term"),
    (note(**{'@context': [AS, {'x': None}]}), "'x' has an unsupported definition"),
    (note(**{'@context': [AS, {'x': {'@reverse': 'as:x'}}]}), "'x' has an unsupported definition"),
    (note(**{'@context': [AS, {'x': {'@type': '@id'}}]}), "'x' has an unsupported definition"),
    (note(**{'@context': [AS, {'x': {'@id': 'as:x', '@type': '@vocab'}}]}), "'x' has an unsupported"),
    (note(**{'@context': [AS, {'x': {'@id': 'as:x', '@type': 5}}]}), "'x' has an unsupported"),
    (note(**{'@context': [AS, {'x': {'@id': 'as:x', '@container': '@index'}}]}), "'x' has an unsupported"),
    (note(**{'@context': [AS, {'x': {'@id': 'as:x', '@container': ['@set']}}]}), "'x' has an unsupported"),
    (note(**{'@context': [AS, {'a': 'b:x', 'b': 'a:y'}]}), 'could not be processed'),
    (note(**{'@context': [AS, {'x': 'https://evil.example/ns#x'}]}), "maps 'x' outside the known namespaces"),
    (note(**{'@context': [AS, {'x': '_:x'}]}), "maps 'x' outside the known namespaces"),
    (note(**{'@context': [AS, {'x': '@id'}]}), "maps 'x' outside the known namespaces"),
    (note(**{'@context': [AS, {'x': 'as:content'}]}), "makes 'x' an alias of"),
    (note(**{'@context': [AS, {'stickied': 'toot:votersCount'}]}), "redefines 'stickied'"),
    (note(nsfw=True), "'nsfw' is not defined"),
    (note(**{AS_CONTENT: 'x'}), 'is not defined'),
    (note(**{'@id': 'https://evil.example/1'}), "'@id' is not defined"),
    (note(id=5), "'id' is not a string"),
    (note(id='_:b0'), 'is not an absolute IRI'),
    (note(id='relative'), 'is not an absolute IRI'),
    (note(inReplyTo='relative'), 'is not an absolute IRI'),
    (note(to=['https://ok.example/u/a', 'not an iri']), 'is not an absolute IRI'),
    (note(type='Weird'), 'is not a defined type or an absolute IRI'),
    (note(type=['Note', 5]), 'is not a defined type or an absolute IRI'),
    (note(type='id'), 'names a keyword'),
    (note(**{'@context': [AS, SEC, {'xid': '@id'}]}), "maps 'xid' outside the known namespaces"),
])
def test_the_gate_refuses(app, document, reason):
    with pytest.raises(LDCoverageError, match=reason):
        check_ld_coverage(document)


@pytest.mark.parametrize('document', [
    note(contentMap={'en': 'x', 'und-fake key': 'y'}),
    note(type=['Note', 'https://misskey-hub.net/ns#Thing', 'as:Note'], to='as:Public'),
    note(object=[{'id': 'https://ok.example/1', 'type': 'Note', 'url': None}, 'https://ok.example/2'], cc=[]),
    note(**{'@context': [AS, {'misskey': 'https://misskey-hub.net/ns#',
                              '_misskey_content': 'misskey:_misskey_content',
                              'emojis': {'@id': 'misskey:emojis', '@type': '@id', '@container': '@set'}}]},
         _misskey_content='x', emojis=['https://ok.example/e/1']),
    note(**{'@context': [AS, SEC, {'expires': 'as:endTime'}]}, expires='2026-10-11T00:00:00Z'),
])
def test_the_gate_accepts(app, document):
    check_ld_coverage(document)


def test_the_loader_still_refuses_an_unbundled_context_should_anything_reach_it(app):
    with pytest.raises(jsonld.JsonLdError, match='is not bundled'):
        ld_document_loader('https://evil.example/ctx')


# --- malformed signature sections and keys ---------------------------------------------------------------

GOOD_SIGNATURE = {'type': 'RsaSignature2017', 'creator': f'{ACTOR}#main-key', 'created': '2026-10-10T00:00:00Z',
                  'signatureValue': 'YWJj'}


@pytest.mark.parametrize('signature', [
    'garbage', 42, None, [], {},
    {k: v for k, v in GOOD_SIGNATURE.items() if k != 'type'},
    {k: v for k, v in GOOD_SIGNATURE.items() if k != 'signatureValue'},
    {**GOOD_SIGNATURE, 'type': 5},
    {**GOOD_SIGNATURE, 'creator': ['x']},
    {**GOOD_SIGNATURE, 'created': None},
    {**GOOD_SIGNATURE, 'signatureValue': 7},
])
def test_a_malformed_signature_section_is_a_format_error(app, signature):
    with pytest.raises(VerificationFormatError, match='Invalid signature section'):
        LDSignature.verify_signature(note(signature=signature), 'unused')


@pytest.mark.parametrize('value', ['!!not base64!!', 'YWJ'])
def test_a_signature_value_that_is_not_base64_is_a_format_error(app, value):
    with pytest.raises(VerificationFormatError, match='Invalid signatureValue'):
        LDSignature.verify_signature(note(signature={**GOOD_SIGNATURE, 'signatureValue': value}), 'unused')


def _ed25519_public_pem():
    return ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode('ascii')


@pytest.mark.parametrize('public_key', [None, 'not a pem', 'ünïcode', 'ed25519'])
def test_an_unusable_public_key_is_a_verification_error(app, no_network_ld_signing, public_key):
    if public_key == 'ed25519':
        public_key = _ed25519_public_pem()
    private_key, _ = a_keypair()
    document = signed(plain_create(), private_key)

    with pytest.raises(VerificationError, match='Unusable public key'):
        LDSignature.verify_signature(document, public_key)


# --- each inbox path that accepts a document on an LD signature -------------------------------------------

def _peer_create(peer):
    note_id = f'{peer.ap_profile_id}/notes/1'
    return inbox_activity(peer, activity_type='Create', object={
        'id': note_id, 'type': 'Note', 'attributedTo': peer.ap_profile_id, 'content': '<p>what the peer wrote</p>',
        'to': ['https://www.w3.org/ns/activitystreams#Public']})


def _peer_tamper_inline_remap(activity):
    note = activity['object']
    note['@context'] = {'content': '_:ignored', 'realcontent': 'as:content'}
    note['realcontent'], note['content'] = note['content'], '<p>forged</p>'


def _peer_tamper_unmapped(activity):
    activity['object']['nsfw'] = True


def _peer_tamper_id_alias(activity):
    note = activity['object']
    note['@context'] = {'id': '_:x', 'realid': '@id'}
    note['realid'], note['id'] = note['id'], 'https://evil.example/objects/1'


PEER_TAMPERS = [_peer_tamper_inline_remap, _peer_tamper_unmapped, _peer_tamper_id_alias]


def _ld_signed_tampered(peer, tamper):
    activity = _peer_create(peer)
    activity['@context'] = default_context()
    activity['signature'] = LDSignature.create_signature(activity, peer.private_key, f'{peer.ap_profile_id}#main-key')
    if tamper:
        tamper(activity)
    return activity


@pytest.fixture
def dispatched(app, monkeypatch):
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    calls = []
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append(a))
    return calls


@pytest.mark.usefixtures('redis_double')
@pytest.mark.parametrize('tamper', PEER_TAMPERS)
def test_the_bounced_inbox_path_refuses_a_tampered_ld_signed_body(app, signing_peer, dispatched,
                                                                  no_network_ld_signing, tamper):
    """routes.py: no HTTP signature, so the LD signature is the only check."""
    body = json.dumps(_ld_signed_tampered(signing_peer, tamper)).encode('utf8')
    with app.test_client() as client:
        response = client.post('/inbox', data=body, headers=unsigned_but_precheck_clean_headers(body),
                               content_type='application/activity+json')

    assert response.status_code == 400 and dispatched == []
    assert ActivityPubLog.query.one().exception_message.startswith(
        'Could not verify LD signature: Signature does not cover the document')


@pytest.mark.usefixtures('redis_double')
def test_the_bounced_inbox_path_accepts_the_untampered_body(app, signing_peer, dispatched, no_network_ld_signing):
    body = json.dumps(_ld_signed_tampered(signing_peer, None)).encode('utf8')
    with app.test_client() as client:
        response = client.post('/inbox', data=body, headers=unsigned_but_precheck_clean_headers(body),
                               content_type='application/activity+json')

    assert response.status_code == 200 and len(dispatched) == 1


def _post_with_foreign_key_id(app, activity, private_key):
    _uri, headers, body = HttpSignature.signed_request(f"https://{app.config['SERVER_NAME']}/inbox", activity,
                                                       private_key, 'https://other.example/actor#main-key',
                                                       send_via_async=True)
    with app.test_client() as client:
        return client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')


@pytest.mark.usefixtures('redis_double')
@pytest.mark.parametrize('tamper', PEER_TAMPERS)
def test_a_foreign_key_id_is_not_excused_by_a_tampered_ld_signature(app, signing_peer, dispatched,
                                                                    no_network_ld_signing, tamper):
    """routes.py `_ld_signature_verifies`: a keyId on another host passes only with the actor's LD signature."""
    response = _post_with_foreign_key_id(app, _ld_signed_tampered(signing_peer, tamper), signing_peer.private_key)

    assert response.status_code == 400 and dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'Signature keyId host does not match actor'


@pytest.mark.usefixtures('redis_double')
def test_a_foreign_key_id_is_excused_by_the_untampered_ld_signature(app, signing_peer, dispatched,
                                                                   no_network_ld_signing):
    response = _post_with_foreign_key_id(app, _ld_signed_tampered(signing_peer, None), signing_peer.private_key)

    assert response.status_code == 200 and len(dispatched) == 1
