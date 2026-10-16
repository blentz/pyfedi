"""Ruling R-LDHARDEN: an LD signature is honoured only for documents whose JSON it actually covers.

RsaSignature2017 signs the URDNA2015 graph, pyfedi reads the JSON keys. Every tamper below keeps a genuine
signature (production's own `LDSignature.create_signature`) and is refused by `LDSignature.verify_signature`,
at the unit level and at each inbox path that accepts a document on an LD signature.
"""
import copy
import json
import uuid

import pytest
from pyld import jsonld
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.activitypub.ld_contexts import (CANONICAL_LD_TERMS, MASTODON_CONTEXT_TERMS, PYFEDI_CONTEXT_TERMS,
                                         LDCoverageError, check_ld_coverage, ld_document_loader, strip_empty_values)
from app.activitypub.signature import HttpSignature, LDSignature, VerificationError, VerificationFormatError, \
    default_context
from app import db
from app.activitypub.routes import UNVERIFIED_LOG_MESSAGES, process_delete_request, process_inbox_request
from app.activitypub.util import log_incoming_ap, verify_announced_inner, verify_ld_accepted_activity
from app.constants import APLOG_CREATE, APLOG_FAILURE, APLOG_SUCCESS
from app.models import ActivityPubLog, Post, User, utcnow
from tests.conftest import unsigned_but_precheck_clean_headers
from tests.factories import AS_PUBLIC_URI, a_keypair, inbox_activity, make_community, make_user, note_document, \
    serve_remote_object
from tests.test_inbox_dispatch_announce import _seed_announcing_community

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
    (note(to=['as:Public']), 'is a compact IRI'),
    (note(inReplyTo='sec:x'), 'is a compact IRI'),
    (note(id='as:x'), 'is a compact IRI'),
    (note(type='as:Note'), 'spells a defined type another way'),
    (note(type='https://www.w3.org/ns/activitystreams#Note'), 'spells a defined type another way'),
    (note(**{'@context': [AS, SEC, {'xid': '@id'}]}), "maps 'xid' outside the known namespaces"),
    # A value shaped like a keyword that is none: pyld warns and defines nothing, and the gate raised KeyError
    # looking the term up. Found by the ld_contexts fuzz target.
    (note(**{'@context': [AS, {'realid': '@ad'}]}), "'realid' is ignored by JSON-LD"),
    (note(**{'@context': [AS, {'realid': {'@id': '@ad'}}]}), "'realid' is ignored by JSON-LD"),
])
def test_the_gate_refuses(app, document, reason):
    with pytest.raises(LDCoverageError, match=reason):
        check_ld_coverage(document)


@pytest.mark.parametrize('document', [
    note(contentMap={'en': 'x', 'und-fake key': 'y'}),
    note(type=['Note', 'https://misskey-hub.net/ns#Thing'], to='https://www.w3.org/ns/activitystreams#Public'),
    note(object=[{'id': 'https://ok.example/1', 'type': 'Note', 'url': None}, 'https://ok.example/2'], cc=[]),
    note(**{'@context': [AS, {'misskey': 'https://misskey-hub.net/ns#',
                              '_misskey_content': 'misskey:_misskey_content',
                              'emojis': {'@id': 'misskey:emojis', '@type': '@id', '@container': '@set'}}]},
         _misskey_content='x', emojis=['https://ok.example/e/1']),
    note(**{'@context': [AS, SEC, {'expires': 'as:endTime'}]}, expires='2026-10-11T00:00:00Z'),
])
def test_the_gate_accepts(app, document):
    check_ld_coverage(document)


@pytest.mark.parametrize('nested', [
    json.loads('[' * 5000 + ']' * 5000),
    json.loads('{"content": "x", "object": ' * 5000 + '1' + '}' * 5000),
])
def test_the_gate_refuses_a_document_nested_past_the_recursion_limit(app, nested):
    """json.loads reads any depth, and the gate recursed once per level: RecursionError escaped it, and with it
    LDSignature.verify_signature. Found by the ld_contexts fuzz target."""
    with pytest.raises(LDCoverageError, match='nested too deeply'):
        check_ld_coverage(note(object=nested))


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
    monkeypatch.setattr('app.activitypub.routes.process_inbox_request', lambda *a, **kw: calls.append((a, kw)))
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
    assert dispatched[0][1] == {'ld_signed': True}     # R-LDDOWNGRADE: the worker takes content from the origin


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
    assert dispatched[0][1] == {'ld_signed': True}


@pytest.mark.usefixtures('redis_double')
def test_an_http_signature_from_the_actors_own_host_is_not_marked_ld_signed(app, signing_peer, dispatched,
                                                                           no_network_ld_signing):
    _uri, headers, body = HttpSignature.signed_request(
        f"https://{app.config['SERVER_NAME']}/inbox", _ld_signed_tampered(signing_peer, None),
        signing_peer.private_key, f'{signing_peer.ap_profile_id}#main-key', send_via_async=True)
    with app.test_client() as client:
        response = client.post('/inbox', data=body, headers=headers, content_type='application/activity+json')

    # signed_time travels with an HTTP-signed delivery (R-UNDORELEASE); nothing marks it LD-signed
    assert response.status_code == 200 and set(dispatched[0][1]) == {'signed_time'}


# --- R-LDDOWNGRADE: what an LD signature still cannot vouch for ---------------------------------------------

def test_null_and_empty_values_are_stripped_after_verification(app, monkeypatch, no_network_ld_signing):
    """An injected null or empty list produces no triple, so the signature still verifies: it must not be read."""
    monkeypatch.setitem(app.config, 'FULL_AP_CONTEXT', True)
    private_key, public_key = a_keypair()
    original = plain_create()
    original['@context'] = default_context()
    original = signed(original, private_key)
    tampered = copy.deepcopy(original)
    tampered['object'].update(commentsEnabled=None, stickied=None, sensitive=[], bcc=[], cc=None,
                              nameMap={'fr': None}, tag=[None])
    assert graph_hash(tampered) == graph_hash(original)

    LDSignature.verify_signature(tampered, public_key)

    for key in ('commentsEnabled', 'stickied', 'sensitive', 'bcc', 'cc', 'nameMap', 'tag', 'summary',
                'inReplyTo'):
        assert key not in tampered['object']
    assert tampered['object']['content'] == '<p>what alice wrote</p>'


def test_strip_keeps_falsy_values_that_are_signed(app):
    document = {'a': False, 'b': 0, 'c': '', 'd': [0, None, [], {}], 'e': {'f': None}}
    strip_empty_values(document)
    assert document == {'a': False, 'b': 0, 'c': '', 'd': [0]}


CAROL = 'https://other.example/users/carol'
CAROL_NOTE = 'https://other.example/objects/1'


def _carol_and_community():
    _instance, community = _seed_announcing_community(host='other.example')
    carol = make_user(_instance, 'carol', with_keys=True)
    carol.ap_fetched_at = utcnow()
    db.session.commit()
    return carol, community


def _ld_create(carol, community, content='<p>forged in transit</p>'):
    addressing = {'to': [AS_PUBLIC_URI], 'audience': community.ap_profile_id}
    activity = {'@context': default_context(), 'id': f'{CAROL}/activities/{uuid.uuid4().hex}', 'type': 'Create',
                'actor': CAROL, **addressing,
                'object': note_document(attributed_to=CAROL, uri=CAROL_NOTE, fields={'content': content, **addressing})}
    activity['signature'] = LDSignature.create_signature(activity, carol.private_key, f'{CAROL}#main-key')
    LDSignature.verify_signature(activity, carol.public_key)
    return activity


def _ld_messages(activity_id):
    return [log.exception_message for log in ActivityPubLog.query.filter_by(activity_id=activity_id).all()]


def test_an_ld_signed_create_stores_the_origins_content(app, db_session, monkeypatch, http_mock,
                                                         no_network_ld_signing):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    carol, community = _carol_and_community()
    activity = _ld_create(carol, community)
    serve_remote_object(http_mock, uri=CAROL_NOTE, document=note_document(
        attributed_to=CAROL, uri=CAROL_NOTE, fields={'content': 'what the origin says', 'to': [AS_PUBLIC_URI],
                                                     'audience': community.ap_profile_id}))

    process_inbox_request(activity, True, ld_signed=True)

    assert 'what the origin says' in Post.query.filter_by(ap_id=CAROL_NOTE).one().body_html
    messages = _ld_messages(activity['id'])
    assert messages and all(message.startswith('LD-signed') for message in messages)   # never replayed


def test_an_ld_signed_create_whose_origin_names_another_author_is_dropped(app, db_session, monkeypatch, http_mock,
                                                                         no_network_ld_signing):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    carol, community = _carol_and_community()
    activity = _ld_create(carol, community)
    serve_remote_object(http_mock, uri=CAROL_NOTE, document=note_document(
        attributed_to='https://other.example/users/dave', uri=CAROL_NOTE, fields={'to': [AS_PUBLIC_URI]}))

    process_inbox_request(activity, True, ld_signed=True)

    assert Post.query.filter_by(ap_id=CAROL_NOTE).first() is None
    assert _ld_messages(activity['id']) == [
        'LD-signed activity dropped: the fetched object is not attributed to the Announced activity actor']


def _ld_delete(actor, object_id):
    activity = {'@context': default_context(), 'id': f'{actor.ap_profile_id}#delete-{uuid.uuid4().hex}',
                'type': 'Delete', 'actor': actor.ap_profile_id, 'object': object_id,
                'to': [AS_PUBLIC_URI]}
    activity['signature'] = LDSignature.create_signature(activity, actor.private_key,
                                                         f'{actor.ap_profile_id}#main-key')
    LDSignature.verify_signature(activity, actor.public_key)
    return activity


def test_an_ld_signed_delete_of_an_object_its_origin_still_serves_is_dropped(app, db_session, monkeypatch,
                                                                            http_mock, no_network_ld_signing):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    carol, _community = _carol_and_community()
    activity = _ld_delete(carol, CAROL_NOTE)
    http_mock.get(CAROL_NOTE).respond(200, json=note_document(attributed_to=CAROL, uri=CAROL_NOTE))

    process_inbox_request(activity, True, ld_signed=True)

    assert _ld_messages(activity['id']) == ['LD-signed activity dropped: LD-signed Delete of an object that still exists']


@pytest.mark.parametrize('status, deleted', [(410, True), (200, False)])
def test_an_ld_signed_self_delete_needs_the_origin_to_have_dropped_the_actor(app, db_session, monkeypatch, http_mock,
                                                                            no_network_ld_signing, status, deleted):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    carol, _community = _carol_and_community()
    activity = _ld_delete(carol, CAROL)
    http_mock.get(CAROL).respond(status)

    process_delete_request(activity, True, ld_signed=True)

    db.session.expire_all()     # the task wrote through its own session
    assert db.session.get(User, carol.id).deleted is deleted
    assert all(message.startswith('LD-signed') for message in _ld_messages(activity['id']))
    if not deleted:
        assert _ld_messages(activity['id']) == [
            'LD-signed activity dropped: LD-signed Delete of an object that still exists']


def test_other_ld_signed_activities_keep_the_verified_document(app):
    like = {'id': 'https://other.example/likes/1', 'type': 'Like', 'actor': CAROL, 'object': CAROL_NOTE}
    assert verify_ld_accepted_activity(like) == (like, None)
    assert verify_ld_accepted_activity({'type': 'Delete', 'actor': CAROL, 'object': {'type': 'Tombstone'}}) == \
        (None, 'LD-signed Delete names no object')


def test_the_mastodon_shaped_create_still_verifies_after_the_downgrade_rules(app, no_network_ld_signing):
    private_key, public_key = a_keypair()
    document = signed(mastodon_create(), private_key)
    LDSignature.verify_signature(document, public_key)
    assert 'summary' not in document['object'] and document['object']['contentMap'] == {'en': '<p>what alice wrote</p>'}


# --- round 2: replay, Announce, addressing --------------------------------------------------------------------

@pytest.mark.usefixtures('redis_double')
def test_a_duplicate_of_an_ld_accepted_delivery_is_logged_as_ld_signed(app, signing_peer, dispatched, monkeypatch,
                                                                       no_network_ld_signing):
    """The claim after verification loses a race with a concurrent delivery of the same id."""
    monkeypatch.setattr('app.activitypub.routes.claim_activity', lambda *a: False)
    body = json.dumps(_ld_signed_tampered(signing_peer, None)).encode('utf8')
    with app.test_client() as client:
        client.post('/inbox', data=body, headers=unsigned_but_precheck_clean_headers(body),
                    content_type='application/activity+json')

    assert dispatched == []
    assert ActivityPubLog.query.one().exception_message == 'LD-signed: Already aware of this activity'


@pytest.mark.usefixtures('redis_double')
def test_a_repeat_delivery_is_logged_before_any_signature_check_and_is_not_replayable(app, signing_peer,
                                                                                      dispatched,
                                                                                      no_network_ld_signing):
    activity = _ld_signed_tampered(signing_peer, None)
    with app.test_client() as client:
        for _ in range(2):
            body = json.dumps(activity).encode('utf8')
            client.post('/inbox', data=body, headers=unsigned_but_precheck_clean_headers(body),
                        content_type='application/activity+json')

    assert len(dispatched) == 1
    assert ActivityPubLog.query.one().exception_message.startswith(UNVERIFIED_LOG_MESSAGES)


def test_rows_outside_an_ld_accepted_delivery_are_not_marked(app, db_session, monkeypatch):
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    log_incoming_ap('https://other.example/a/1', APLOG_CREATE, APLOG_SUCCESS, None)
    log_incoming_ap('https://other.example/a/2', APLOG_CREATE, APLOG_FAILURE, None, 'why')
    assert [row.exception_message for row in ActivityPubLog.query.order_by(ActivityPubLog.id)] == [None, 'why']


def _announce_of(inner, community):
    group = community.ap_profile_id
    announce = {'@context': default_context(), 'id': f'{group}/activities/announce/{uuid.uuid4().hex}',
                'type': 'Announce', 'actor': group, 'object': inner, 'to': [AS_PUBLIC_URI]}
    return announce


def _inner_create(community, content='<p>forged in transit</p>'):
    addressing = {'to': [AS_PUBLIC_URI], 'audience': community.ap_profile_id}
    return {'id': f'{CAROL}/activities/{uuid.uuid4().hex}', 'type': 'Create', 'actor': CAROL, **addressing,
            'object': note_document(attributed_to=CAROL, uri=CAROL_NOTE, fields={'content': content, **addressing})}


def _serve_origin_note(http_mock, community, fields=None):
    serve_remote_object(http_mock, uri=CAROL_NOTE, document=note_document(
        attributed_to=CAROL, uri=CAROL_NOTE, fields={'content': 'what the origin says', 'to': [AS_PUBLIC_URI],
                                                     'audience': community.ap_profile_id, **(fields or {})}))


@pytest.mark.parametrize('as_list', [False, True])
def test_an_ld_signed_announce_of_a_same_host_create_stores_the_origins_content(app, db_session, monkeypatch,
                                                                               http_mock, as_list):
    """R-LDANNOUNCE: the Group's own host would otherwise be trusted on the (LD) signature alone."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    _carol, community = _carol_and_community()
    community.ap_fetched_at = utcnow()
    db.session.commit()
    inner = _inner_create(community)
    announce = _announce_of([inner] if as_list else inner, community)
    _serve_origin_note(http_mock, community)

    process_inbox_request(announce, True, ld_signed=True)

    assert 'what the origin says' in Post.query.filter_by(ap_id=CAROL_NOTE).one().body_html


def test_an_announce_of_a_same_host_create_without_ld_is_trusted_as_before(app, db_session):
    _carol, community = _carol_and_community()
    inner = _inner_create(community)
    assert verify_announced_inner(_announce_of(inner, community), community) == (inner, None)


@pytest.mark.parametrize('status, expected', [
    (200, (None, 'Announced Delete of an object that still exists')),
    (410, 'processed'),
])
def test_an_ld_signed_announce_of_a_same_host_delete_needs_the_origin_gone(app, db_session, http_mock, status,
                                                                          expected):
    _carol, community = _carol_and_community()
    inner = {'id': f'{CAROL}#delete-1', 'type': 'Delete', 'actor': CAROL, 'object': CAROL_NOTE}
    http_mock.get(CAROL_NOTE).respond(status)

    result = verify_announced_inner(_announce_of(inner, community), community, ld_signed=True)

    assert result == (expected if status == 200 else (inner, None))


def test_an_ld_signed_announce_of_a_delete_naming_no_object_is_refused(app, db_session):
    _carol, community = _carol_and_community()
    inner = {'id': f'{CAROL}#delete-1', 'type': 'Delete', 'actor': CAROL, 'object': {'type': 'Tombstone'}}
    assert verify_announced_inner(_announce_of(inner, community), community, ld_signed=True) == \
        (None, 'Announced Delete names no object')


def test_an_ld_signed_announce_whose_inner_content_is_refused_stays_refused(app, db_session, http_mock):
    _carol, community = _carol_and_community()
    serve_remote_object(http_mock, uri=CAROL_NOTE, document=note_document(
        attributed_to='https://other.example/users/dave', uri=CAROL_NOTE))
    assert verify_announced_inner(_announce_of(_inner_create(community), community), community, ld_signed=True) == \
        (None, 'the fetched object is not attributed to the Announced activity actor')


def test_an_ld_signed_create_takes_its_addressing_from_the_origin_object(app, db_session, monkeypatch, http_mock,
                                                                        no_network_ld_signing):
    """The wrapper's to/audience are LD JSON too: find_community and the private flag must follow the origin."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    carol, community = _carol_and_community()
    decoy = make_community('decoy', host='other.example')
    decoy.ap_fetched_at = utcnow()
    db.session.commit()
    activity = _ld_create(carol, community)
    activity.update(audience=decoy.ap_profile_id, to=[f'{CAROL}/followers'], cc=[decoy.ap_profile_id])
    _serve_origin_note(http_mock, community)

    process_inbox_request(activity, True, ld_signed=True)

    post = Post.query.filter_by(ap_id=CAROL_NOTE).one()
    assert post.community_id == community.id and post.private is False

