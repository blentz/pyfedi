"""Connected accounts P2: Bluesky (spec 2026-11-13-connected-accounts-design.md §2, §6; C5).

respx plays the PLC directory, the handle resolver and a PDS that is its own authorization server, answering as the
PDS did in the P0 spike (interop/spike_bluesky/report.json). interop/test_bluesky.py runs the same code against a
real PDS.
"""
import io
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from jwt.algorithms import ECAlgorithm
from PIL import Image

from app import cli
from app.connections import AuthFailed, MaybeSent, Permanent, Progress, Transient, setting_name
from app.connections import bluesky, content
from app.atproto import records
from app.atproto.records import (as_jpeg, build_record, compose, existing_record, facets, labels, new_tid,
                                 upload_video, video_embed)
from app.atproto.resolve import did_document, handle_of, pds_of, resolve_handle
from app.connections.bluesky import (BlueskyService, OAuthSession, authorization_server, client_assertion,
                                     client_metadata, dpop_proof, jwks, new_key_jwk)
from app.connections.content import image_bytes, plain_text
from app.constants import COMMUNITY_KIND_MICROBLOGS, POST_TYPE_ARTICLE, POST_TYPE_LINK
from app.sealing import generate_key
from app.utils import set_setting

pytestmark = pytest.mark.usefixtures('site')

PDS = 'https://pds.example'
PLC = 'https://plc.example'
RESOLVER = 'https://resolver.example'
DID = 'did:plc:alice123'
AS_METADATA = {'issuer': PDS, 'pushed_authorization_request_endpoint': f'{PDS}/oauth/par',
               'authorization_endpoint': f'{PDS}/oauth/authorize', 'token_endpoint': f'{PDS}/oauth/token',
               'revocation_endpoint': f'{PDS}/oauth/revoke'}
DOCUMENT = {'id': DID, 'alsoKnownAs': ['at://alice.example'],
            'service': [{'id': '#atproto_pds', 'type': 'AtprotoPersonalDataServer', 'serviceEndpoint': PDS}]}


@pytest.fixture
def bsky(app, db_session, monkeypatch):
    key = new_key_jwk()
    for name, value in (('BLUESKY_OAUTH_KEY', key), ('BLUESKY_PLC_URL', PLC), ('BLUESKY_RESOLVER', RESOLVER),
                        ('BLUESKY_DEFAULT_SERVER', PDS), ('CONNECTIONS_SECRETS_KEY', generate_key())):
        monkeypatch.setitem(app.config, name, value)
    monkeypatch.setattr(bluesky, '_nonces', {})
    set_setting(setting_name('bluesky'), True)
    return SimpleNamespace(key=ECAlgorithm.from_jwk(key), service=bluesky.SERVICE)


def tokens(**changes) -> dict:
    return {'access_token': 'at-1', 'refresh_token': 'rt-1', 'expires_at': time.time() + 3600,
            'dpop_jwk': new_key_jwk(), 'pds': PDS, 'issuer': PDS, 'token_endpoint': f'{PDS}/oauth/token',
            'revocation_endpoint': f'{PDS}/oauth/revoke', 'did': DID, **changes}


def session(**changes) -> OAuthSession:
    return OAuthSession(tokens(**changes))


MICROBLOGS = SimpleNamespace(kind=COMMUNITY_KIND_MICROBLOGS, is_local=lambda: True)


def a_post(**changes):
    """A post in the local microblogs (a full post, A5) unless `community` says otherwise."""
    post = SimpleNamespace(title='', body='', ap_id='https://piefed.example/post/7', content_warning=None,
                           nsfw=False, nsfl=False, gallery_count=0, gallery=[], image_id=None, image=None,
                           extensions=None, language=None, type=POST_TYPE_ARTICLE, url=None, community=MICROBLOGS)
    for name, value in changes.items():
        setattr(post, name, value)
    return post


def serve_identity(http_mock, did=DID):
    http_mock.get(f'{PLC}/{did}').respond(200, json=DOCUMENT)


def serve_server(http_mock, prm=True):
    if prm:
        http_mock.get(f'{PDS}/.well-known/oauth-protected-resource').respond(
            200, json={'authorization_servers': [PDS]})
    http_mock.get(f'{PDS}/.well-known/oauth-authorization-server').respond(200, json=AS_METADATA)


def form_of(request) -> dict:
    return {name: values[0] for name, values in parse_qs(request.read().decode()).items()}


# --------------------------------------------------------------------------
# the client: its key, metadata, assertions and proofs
# --------------------------------------------------------------------------

def test_the_client_is_configured_by_an_es256_private_key(app, monkeypatch):
    service = BlueskyService()
    for value in ('', 'not json', '{"kty": "RSA", "n": "x", "e": "AQAB"}'):
        monkeypatch.setitem(app.config, 'BLUESKY_OAUTH_KEY', value)
        assert not service.configured()
    public = json.dumps(json.loads(ECAlgorithm.to_jwk(ECAlgorithm.from_jwk(new_key_jwk()).public_key())))
    monkeypatch.setitem(app.config, 'BLUESKY_OAUTH_KEY', public)
    assert not service.configured()   # a public key cannot sign
    monkeypatch.setitem(app.config, 'BLUESKY_OAUTH_KEY', new_key_jwk())
    assert service.configured() and service.fits(None) and service.takes_nsfw and not service.can_edit


def test_the_metadata_names_the_main_domain_and_the_public_key(app, bsky):
    with app.test_request_context():
        metadata = client_metadata()
        keys = jwks()['keys']
    assert metadata['client_id'] == 'https://test.piefed.local/connections/bluesky/client-metadata.json'
    assert metadata['redirect_uris'] == ['https://test.piefed.local/user/settings/connections/bluesky/callback']
    assert metadata['token_endpoint_auth_method'] == 'private_key_jwt' and metadata['dpop_bound_access_tokens']
    assert metadata['scope'] == bluesky.IDENTITY_SCOPE   # the most any sign-in asks for (ATProto spec §5)
    assert len(keys) == 1 and 'd' not in keys[0] and keys[0]['alg'] == 'ES256' and keys[0]['kid']


def test_the_public_documents_are_served_only_when_bluesky_is_on(app, bsky, monkeypatch):
    client = app.test_client()
    metadata = client.get('/connections/bluesky/client-metadata.json')
    assert metadata.status_code == 200 and metadata.json['client_name'] == 'test.piefed.local'
    assert metadata.headers['Cache-Control'] == 'public, max-age=600'
    assert client.get('/connections/bluesky/jwks.json').json['keys'][0]['kty'] == 'EC'
    with monkeypatch.context() as patched:   # a domain that switched Bluesky off serves no client (C12)
        patched.setattr('app.user.connections.current_storefront', lambda: SimpleNamespace(id=7))
        patched.setattr('app.connections.apps.domain_switch', lambda service, domain_id: 'off' if domain_id == 7 else None)
        assert client.get('/connections/bluesky/jwks.json').status_code == 404
    set_setting(setting_name('bluesky'), False)
    assert client.get('/connections/bluesky/client-metadata.json').status_code == 404


def test_a_client_assertion_and_a_dpop_proof(app, bsky):
    with app.test_request_context():
        assertion = client_assertion(PDS)
        kid = jwks()['keys'][0]['kid']
    claims = jwt.decode(assertion, bsky.key.public_key(), algorithms=['ES256'], audience=PDS)
    assert claims['iss'] == claims['sub'] == 'https://test.piefed.local/connections/bluesky/client-metadata.json'
    assert jwt.get_unverified_header(assertion)['kid'] == kid
    key = new_key_jwk()
    proof = dpop_proof(key, 'POST', f'{PDS}/xrpc/x?a=1', 'n-1', 'token')
    header = jwt.get_unverified_header(proof)
    assert header['typ'] == 'dpop+jwt' and 'd' not in header['jwk']
    claims = jwt.decode(proof, ECAlgorithm.from_jwk(json.dumps(header['jwk'])), algorithms=['ES256'])
    assert claims['htu'] == f'{PDS}/xrpc/x' and claims['nonce'] == 'n-1' and claims['ath']
    assert 'nonce' not in jwt.decode(dpop_proof(key, 'GET', PDS, None), options={'verify_signature': False})


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def test_a_handle_resolves_by_its_well_known_answer(app, bsky, http_mock):
    http_mock.get('https://alice.example/.well-known/atproto-did').respond(200, text=f'{DID}\n')
    assert resolve_handle('alice.example') == DID


@pytest.mark.parametrize('answer, expected', [({'did': DID}, DID), ({'did': 'nope'}, None), (None, None)],
                         ids=['resolver', 'not-a-did', 'unreachable'])
def test_a_handle_with_no_well_known_answer_goes_to_the_resolver(app, bsky, http_mock, answer, expected):
    http_mock.get('https://dns.example/.well-known/atproto-did').respond(404)
    route = http_mock.get(f'{RESOLVER}/xrpc/com.atproto.identity.resolveHandle')
    if answer is None:
        route.respond(400, json={'error': 'InvalidRequest'})
    else:
        route.respond(200, json=answer)
    assert resolve_handle('dns.example') == expected


def test_a_well_known_answer_that_is_no_did_falls_through(app, bsky, http_mock):
    http_mock.get('https://odd.example/.well-known/atproto-did').respond(200, text='<html>')
    http_mock.get(f'{RESOLVER}/xrpc/com.atproto.identity.resolveHandle').respond(200, json={'did': DID})
    assert resolve_handle('odd.example') == DID


def test_did_documents_and_what_they_name(app, bsky, http_mock):
    serve_identity(http_mock)
    http_mock.get('https://web.example/.well-known/did.json').respond(200, json={'id': 'did:web:web.example'})
    assert did_document(DID) == DOCUMENT and did_document('did:web:web.example')['id'] == 'did:web:web.example'
    with pytest.raises(Permanent):
        did_document('did:key:z6Mk')
    assert pds_of(DOCUMENT) == PDS and handle_of(DOCUMENT) == 'alice.example'
    assert handle_of({'alsoKnownAs': ['https://x', 7]}) is None
    with pytest.raises(Permanent):
        pds_of({'service': [{'id': '#atproto_pds', 'serviceEndpoint': 'http://insecure.example'}, 'junk']})
    with pytest.raises(Permanent):
        pds_of({})


def test_a_server_that_does_not_answer_json(app, bsky, http_mock):
    http_mock.get(f'{PLC}/{DID}').respond(200, text='<html>')
    with pytest.raises(Permanent):
        did_document(DID)


def test_the_authorization_server_of_a_pds_or_an_entryway(app, bsky, http_mock):
    serve_server(http_mock)
    assert authorization_server(PDS) == (PDS, AS_METADATA)


def test_an_entryway_with_no_protected_resource_document(app, bsky, http_mock):
    http_mock.get(f'{PDS}/.well-known/oauth-protected-resource').respond(404)
    http_mock.get(f'{PDS}/.well-known/oauth-authorization-server').respond(200, json=AS_METADATA)
    assert authorization_server(PDS)[0] == PDS


def test_a_server_claiming_another_issuer_is_refused(app, bsky, http_mock):
    http_mock.get(f'{PDS}/.well-known/oauth-protected-resource').respond(200, json={})
    http_mock.get(f'{PDS}/.well-known/oauth-authorization-server').respond(
        200, json={**AS_METADATA, 'issuer': 'https://evil.example'})
    with pytest.raises(Permanent):
        authorization_server(PDS)


# --------------------------------------------------------------------------
# connecting
# --------------------------------------------------------------------------

def _par(http_mock, nonce_first=False):
    route = http_mock.post(f'{PDS}/oauth/par')
    answers = [httpx.Response(201, json={'request_uri': 'urn:req-1'}, headers={'DPoP-Nonce': 'n-2'})]
    if nonce_first:
        answers.insert(0, httpx.Response(400, json={'error': 'use_dpop_nonce'}, headers={'DPoP-Nonce': 'n-1'}))
    route.side_effect = answers
    return route


def test_connecting_by_handle(app, bsky, http_mock):
    http_mock.get('https://alice.example/.well-known/atproto-did').respond(200, text=DID)
    serve_identity(http_mock)
    serve_server(http_mock)
    par = _par(http_mock, nonce_first=True)
    pending = {}
    with app.test_request_context():
        url = bsky.service.authorize_url(None, 'state-1', 'ignored', pending, hint='@Alice.Example')
    assert url == f'{PDS}/oauth/authorize?client_id=https%3A%2F%2Ftest.piefed.local%2Fconnections%2Fbluesky%2F' \
                  'client-metadata.json&request_uri=urn%3Areq-1'
    sent = form_of(par.calls.last.request)
    assert sent['login_hint'] == 'alice.example' and sent['state'] == 'state-1' and sent['scope'] == bluesky.SCOPE
    assert sent['redirect_uri'] == 'https://test.piefed.local/user/settings/connections/bluesky/callback'
    proof = jwt.decode(par.calls.last.request.headers['DPoP'], options={'verify_signature': False})
    assert proof['nonce'] == 'n-1' and bluesky._nonces[PDS] == 'n-2'
    assert pending['did'] == DID and pending['issuer'] == PDS and pending['verifier'] and pending['dpop_jwk']


@pytest.mark.parametrize('hint', ['', 'https://pds.example/whatever', 'pds.example'], ids=['none', 'url', 'server'])
def test_connecting_by_server(app, bsky, http_mock, hint):
    if hint == 'pds.example':   # a name no one answers for as a handle is tried as a server
        http_mock.get('https://pds.example/.well-known/atproto-did').respond(404)
        http_mock.get(f'{RESOLVER}/xrpc/com.atproto.identity.resolveHandle').respond(400, json={})
    serve_server(http_mock)
    par = _par(http_mock)
    pending = {}
    with app.test_request_context():
        bsky.service.authorize_url(None, 's', 'ignored', pending, hint=hint)
    assert 'login_hint' not in form_of(par.calls.last.request) and pending['did'] is None


def test_connecting_by_did(app, bsky, http_mock):
    serve_identity(http_mock)
    serve_server(http_mock)
    par = _par(http_mock)
    with app.test_request_context():
        bsky.service.authorize_url(None, 's', 'ignored', {}, hint=DID)
    assert form_of(par.calls.last.request)['login_hint'] == DID


def test_a_hint_that_is_neither_handle_nor_server(app, bsky):
    with app.test_request_context(), pytest.raises(Permanent):
        bsky.service.authorize_url(None, 's', 'ignored', {}, hint='not a handle!')


def _pending():
    return {'issuer': PDS, 'did': DID, 'verifier': 'v', 'dpop_jwk': new_key_jwk(), 'token_endpoint': f'{PDS}/oauth/token',
            'revocation_endpoint': f'{PDS}/oauth/revoke'}


def _token_answer(**changes):
    return {'access_token': 'at-1', 'refresh_token': 'rt-1', 'token_type': 'DPoP', 'expires_in': 3599,
            'scope': bluesky.SCOPE, 'sub': DID, **changes}


def test_the_callback_makes_the_connection(app, bsky, http_mock):
    token = http_mock.post(f'{PDS}/oauth/token').respond(200, json=_token_answer())
    serve_identity(http_mock)
    serve_server(http_mock)
    pending = _pending()
    with app.test_request_context():
        [account] = bsky.service.finish(None, {'iss': PDS, 'code': 'c-1'}, 'ignored', pending)
    sent = form_of(token.calls.last.request)
    assert sent['code'] == 'c-1' and sent['code_verifier'] == 'v' and sent['grant_type'] == 'authorization_code'
    assert (account.remote_id, account.remote_name, account.variant) == (DID, 'alice.example', 'oauth')
    assert account.tokens['pds'] == PDS and account.tokens['dpop_jwk'] == pending['dpop_jwk']
    assert account.tokens['expires_at'] > time.time() + 3500


@pytest.mark.parametrize('case', ['wrong-iss', 'bearer', 'no-atproto', 'other-account', 'other-server'])
def test_a_callback_that_is_refused(app, bsky, http_mock, case):
    args = {'iss': 'https://evil.example' if case == 'wrong-iss' else PDS, 'code': 'c'}
    if case != 'wrong-iss':
        answer = {'bearer': _token_answer(token_type='Bearer'), 'no-atproto': _token_answer(scope='other'),
                  'other-account': _token_answer(sub='did:plc:mallory')}.get(case, _token_answer())
        http_mock.post(f'{PDS}/oauth/token').respond(200, json=answer)
    if case == 'other-server':
        serve_identity(http_mock)
        http_mock.get(f'{PDS}/.well-known/oauth-protected-resource').respond(
            200, json={'authorization_servers': ['https://other.example']})
        http_mock.get('https://other.example/.well-known/oauth-authorization-server').respond(
            200, json={**AS_METADATA, 'issuer': 'https://other.example'})
    with app.test_request_context(), pytest.raises(Permanent):
        bsky.service.finish(None, args, 'ignored', _pending())


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def test_a_refresh(app, bsky, http_mock):
    http_mock.post(f'{PDS}/oauth/token').respond(200, json=_token_answer(access_token='at-2', refresh_token='rt-2'))
    with app.test_request_context():
        fresh = bsky.service.refresh(tokens())
    assert (fresh['access_token'], fresh['refresh_token'], fresh['pds']) == ('at-2', 'rt-2', PDS)


@pytest.mark.parametrize('case', ['invalid-grant', 'no-refresh-token', 'other-account'])
def test_a_refresh_that_ends_the_connection(app, bsky, http_mock, case):
    if case == 'invalid-grant':
        http_mock.post(f'{PDS}/oauth/token').respond(400, json={'error': 'invalid_grant'})
    elif case == 'other-account':
        http_mock.post(f'{PDS}/oauth/token').respond(200, json=_token_answer(sub='did:plc:other'))
    with app.test_request_context(), pytest.raises(AuthFailed):
        bsky.service.refresh(tokens(refresh_token=None) if case == 'no-refresh-token' else tokens())


def test_revoking(app, bsky, http_mock):
    route = http_mock.post(f'{PDS}/oauth/revoke').respond(200)
    with app.test_request_context():
        bsky.service.revoke(tokens())
        bsky.service.revoke(tokens(revocation_endpoint=None))
    assert route.call_count == 1 and form_of(route.calls.last.request)['token'] == 'rt-1'


def test_an_unreachable_server(app, bsky, http_mock):
    http_mock.post(f'{PDS}/oauth/token').mock(side_effect=httpx.ConnectError('down'))
    http_mock.post(f'{PDS}/xrpc/com.atproto.repo.deleteRecord').mock(side_effect=httpx.ConnectError('down'))
    with app.test_request_context():
        with pytest.raises(Transient):
            bsky.service.refresh(tokens())
        with pytest.raises(Transient):
            bsky.service.delete(tokens(), f'at://{DID}/app.bsky.feed.post/3k')


def test_nonces_twice_refused_end_in_the_failure(app, bsky, http_mock):
    http_mock.post(f'{PDS}/oauth/token').respond(400, json={'error': 'use_dpop_nonce'}, headers={'DPoP-Nonce': 'x'})
    http_mock.post(f'{PDS}/xrpc/com.atproto.repo.deleteRecord').respond(
        401, headers={'WWW-Authenticate': 'DPoP error="use_dpop_nonce"', 'DPoP-Nonce': 'y'})
    with app.test_request_context():
        with pytest.raises(Permanent):
            bsky.service.refresh(tokens())
        with pytest.raises(AuthFailed):
            bsky.service.delete(tokens(), f'at://{DID}/app.bsky.feed.post/3k')


# --------------------------------------------------------------------------
# what a post becomes
# --------------------------------------------------------------------------

def test_the_text_of_a_post(app):
    assert plain_text(a_post(title='Hello', body='The [docs](https://d.example/x) say so.')) == \
        'Hello\n\nThe docs https://d.example/x say so.'
    assert plain_text(a_post(title='Hello there', body='Hello there, world')) == 'Hello there, world'
    assert plain_text(a_post(title='Only a title')) == 'Only a title'


def test_a_short_post_goes_as_it_is_and_a_long_one_is_cut_with_its_link(app):
    link = 'https://piefed.example/post/7'
    assert compose(a_post(body='short'), with_link=False) == 'short'
    assert compose(a_post(body='short'), with_link=True) == f'short\n\n{link}'
    long = compose(a_post(body='word ' * 100), with_link=False)
    assert len(long) <= 300 and long.endswith(f'…\n{link}')
    wide = compose(a_post(body='界' * 290 + ' end'), with_link=True)   # fits alone, not with the link
    assert len(wide) <= 300 and wide.endswith(link) and '…' in wide
    accent = 'e' + chr(0x301)   # e and a combining acute: one character to a reader, two code points
    accents = compose(a_post(body='x' + accent * 200), with_link=False)   # never cut between e and its accent
    assert accents.endswith(accent + '…\nhttps://piefed.example/post/7')


def test_a_cut_backs_off_an_accent_or_a_joiner(app):
    accent, joiner = chr(0x301), chr(0x200D)
    assert content.cut('a' + ('e' + accent) * 10, 3) == 'a…'   # the cut would have kept an accent without its e
    assert content.cut('ab' + joiner + 'cdefgh', 4) == 'a…'     # nor keep half of a joined sequence


def test_four_byte_characters_count_once(app):
    wide = chr(0x1D54F) * 300   # 1200 bytes: the lexicon's 3000-byte limit cannot be reached in 300
    assert compose(a_post(body=wide), with_link=False) == wide


def test_a_content_warning_sends_only_the_warning_and_the_link(app):
    text = compose(a_post(body='spoilers', content_warning='Film plot'), with_link=False)
    assert text == 'CW: Film plot\nhttps://piefed.example/post/7'


def test_facets_count_bytes(app):
    text = 'café https://x.example/a. #piefed and #éa'
    found = facets(text)
    link, tag, second = found
    assert text.encode()[link['index']['byteStart']:link['index']['byteEnd']] == b'https://x.example/a'
    assert link['features'][0]['uri'] == 'https://x.example/a'
    assert text.encode()[tag['index']['byteStart']:tag['index']['byteEnd']] == b'#piefed'
    assert second['features'][0]['tag'] == 'éa'
    assert facets('no#tag here') == []


def test_self_labels(app):
    assert labels(a_post()) is None
    assert [value['val'] for value in labels(a_post(nsfw=True, nsfl=True))['values']] == ['sexual', 'graphic-media']


def _png(size=(40, 30), mode='RGBA') -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 128) if mode == 'RGBA' else (200, 30, 30)).save(out, 'PNG')
    return out.getvalue()


def test_pictures_become_jpegs_within_the_limit(app, monkeypatch):
    content, width, height = as_jpeg(_png())
    assert content[:2] == b'\xff\xd8' and (width, height) == (40, 30)
    assert as_jpeg(b'not a picture') is None
    noise = io.BytesIO()
    Image.effect_noise((3000, 2000), 120).convert('RGB').save(noise, 'PNG')
    content, width, height = as_jpeg(noise.getvalue())
    assert len(content) <= records.MAX_IMAGE_BYTES and max(width, height) <= records.MAX_IMAGE_SIDE
    monkeypatch.setattr(records, 'MAX_IMAGE_BYTES', 10)
    assert as_jpeg(_png()) is None


def test_a_pictures_bytes(app, tmp_path, http_mock):
    stored = tmp_path / 'a.png'
    stored.write_bytes(b'stored')
    assert image_bytes(SimpleNamespace(file_path=str(stored), source_url=None)) == b'stored'
    http_mock.get('https://cdn.example/b.png').respond(200, content=b'fetched')
    http_mock.get('https://cdn.example/gone.png').respond(404)
    assert image_bytes(SimpleNamespace(file_path='/no/such', source_url='https://cdn.example/b.png')) == b'fetched'
    assert image_bytes(SimpleNamespace(file_path=None, source_url='https://cdn.example/gone.png')) is None
    assert image_bytes(SimpleNamespace(file_path=None, source_url='http://cdn.example/c.png')) is None


def _upload(http_mock):
    return http_mock.post(f'{PDS}/xrpc/com.atproto.repo.uploadBlob').respond(200, json={'blob': {
        '$type': 'blob', 'ref': {'$link': 'bafk'}, 'mimeType': 'image/jpeg', 'size': 99}})


def test_a_gallery_sends_four_pictures_and_its_link(app, bsky, http_mock, tmp_path):
    upload = _upload(http_mock)
    files = []
    for number in range(6):
        path = tmp_path / f'{number}.png'
        path.write_bytes(_png())
        files.append(SimpleNamespace(file_path=str(path), source_url=None, alt_text=f'picture {number}'))
    files[1].file_path = None   # one picture that cannot be read is left out, and the link said instead
    post = a_post(title='Trip', body='photos', gallery_count=6, gallery=files,
                  language=SimpleNamespace(code='en'), nsfw=True)
    with app.test_request_context():
        record = build_record(post, session())
    assert upload.call_count == 3 and upload.calls.last.request.headers['Content-Type'] == 'image/jpeg'
    images = record['embed']['images']
    assert [image['alt'] for image in images] == ['picture 0', 'picture 2', 'picture 3']
    assert images[0]['aspectRatio'] == {'width': 40, 'height': 30}
    assert record['text'].endswith('https://piefed.example/post/7') and record['langs'] == ['en']
    assert record['labels']['values'] == [{'val': 'sexual'}] and record['createdAt'].endswith('Z')
    assert record['facets'][0]['features'][0]['uri'] == 'https://piefed.example/post/7'


def test_a_link_post_carries_its_card(app, bsky):
    post = a_post(title='A find', body='worth reading', type=POST_TYPE_LINK, url='https://news.example/a',
                  language=SimpleNamespace(code='und'))
    with app.test_request_context():
        record = build_record(post, session())
    assert record['embed'] == {'$type': 'app.bsky.embed.external', 'external': {
        'uri': 'https://news.example/a', 'title': 'A find', 'description': 'A find\n\nworth reading'}}
    assert 'langs' not in record and 'labels' not in record and 'facets' not in record


def test_an_uploaded_video_goes_as_text_and_link(app, bsky):
    post = a_post(body='my clip', extensions={'media': {'mime': 'video/mp4'}}, image_id=None)
    with app.test_request_context():
        record = build_record(post, session())
    assert 'embed' not in record and record['text'] == 'my clip\n\nhttps://piefed.example/post/7'


# --------------------------------------------------------------------------
# video (spec 19.5)
# --------------------------------------------------------------------------

UPLOAD_BLOB = f'{PDS}/xrpc/com.atproto.repo.uploadBlob'
VIDEO = bytes(range(256)) * 40
BLOB = {'$type': 'blob', 'ref': {'$link': 'bafvideo'}, 'mimeType': 'video/mp4', 'size': len(VIDEO)}


def a_video_post(monkeypatch, tmp_path, **media):
    path = tmp_path / 'v.mp4'
    path.write_bytes(VIDEO)
    monkeypatch.setattr('app.connections.content.media_local_path', lambda post: str(path))
    return a_post(body='my clip', extensions={'media': {'mime': 'video/mp4', **media}},
                  url='https://piefed.example/static/media/v.mp4')


def test_a_video_goes_up_whole_and_again_after_a_nonce(app, bsky, http_mock, monkeypatch, tmp_path):
    upload = http_mock.post(UPLOAD_BLOB)
    upload.side_effect = [httpx.Response(401, headers={'WWW-Authenticate': 'DPoP error="use_dpop_nonce"',
                                                       'DPoP-Nonce': 'n-1'}),
                          httpx.Response(200, json={'blob': BLOB})]
    assert upload_video(a_video_post(monkeypatch, tmp_path), session()) == BLOB
    for call in upload.calls:   # the second try sends the whole file again, from its start
        assert call.request.read() == VIDEO and call.request.headers['Content-Type'] == 'video/mp4'
    assert upload.calls.last.request.headers['Content-Length'] == str(len(VIDEO))


def test_a_video_that_cannot_go_leaves_text_and_link(app, bsky, http_mock, monkeypatch, tmp_path):
    post = a_video_post(monkeypatch, tmp_path)
    assert upload_video(a_post(extensions={'media': {'mime': 'video/webm'}}), session()) is None
    assert upload_video(a_post(content_warning='CW', extensions={'media': {'mime': 'video/mp4'}}), session()) is None
    monkeypatch.setattr(records, 'MAX_VIDEO_BYTES', 10)
    assert upload_video(post, session()) is None   # larger than the lexicon takes
    monkeypatch.setattr(records, 'MAX_VIDEO_BYTES', 300_000_000)
    http_mock.post(UPLOAD_BLOB).respond(400, json={'error': 'BlobTooLarge', 'message': 'too big'})
    assert upload_video(post, session()) is None   # the PDS's own limit is lower


def test_a_video_upload_that_may_work_later_is_tried_again(app, bsky, http_mock, monkeypatch, tmp_path):
    http_mock.post(UPLOAD_BLOB).respond(503)
    with pytest.raises(Transient):
        upload_video(a_video_post(monkeypatch, tmp_path), session())


@pytest.mark.parametrize('media, ratio', [({'width': 1280, 'height': 720}, {'width': 1280, 'height': 720}),
                                          ({'width': 0, 'height': 720}, None), ({'width': '1280', 'height': 720}, None),
                                          ({}, None)], ids=['sized', 'zero', 'text', 'unknown'])
def test_a_video_embed_and_its_shape(app, media, ratio):
    embed = video_embed(a_post(extensions={'media': {'mime': 'video/mp4', **media}}), BLOB)
    assert embed['$type'] == 'app.bsky.embed.video' and embed['video'] == BLOB
    assert embed.get('aspectRatio') == ratio


def test_a_video_post_goes_with_its_video_uploaded_once(app, bsky, http_mock, monkeypatch, tmp_path):
    upload = http_mock.post(UPLOAD_BLOB).respond(200, json={'blob': BLOB})
    create = http_mock.post(f'{PDS}/xrpc/com.atproto.repo.createRecord')
    create.side_effect = [httpx.Response(502), httpx.Response(200, json={'uri': f'at://{DID}/app.bsky.feed.post/v1'})]
    http_mock.get(GET_RECORD).respond(400, json={'error': 'RecordNotFound'})
    post = a_video_post(monkeypatch, tmp_path, width=1080, height=1920)
    progress = Progress()
    with app.test_request_context():
        with pytest.raises(Transient):
            bsky.service.send(post, tokens(), None, progress)
        assert progress.data['video'] == BLOB
        bsky.service.send(post, tokens(), None, progress)
    assert upload.call_count == 1
    record = json.loads(create.calls.last.request.read())['record']
    assert record['embed'] == {'$type': 'app.bsky.embed.video', 'video': BLOB,
                               'aspectRatio': {'width': 1080, 'height': 1920}}
    assert record['text'] == 'my clip'   # the video is the post: no link back unless cut


def test_a_video_post_whose_video_cannot_go_is_text_and_link(app, bsky, http_mock, monkeypatch, tmp_path):
    http_mock.post(UPLOAD_BLOB).respond(400, json={'error': 'BlobTooLarge'})
    create = http_mock.post(f'{PDS}/xrpc/com.atproto.repo.createRecord').respond(
        200, json={'uri': f'at://{DID}/app.bsky.feed.post/v2'})
    progress = Progress()
    with app.test_request_context():
        bsky.service.send(a_video_post(monkeypatch, tmp_path), tokens(), None, progress)
    record = json.loads(create.calls.last.request.read())['record']
    assert 'embed' not in record and record['text'] == 'my clip\n\nhttps://piefed.example/post/7'
    assert progress.data['video'] is None   # not tried again on a later attempt


# --------------------------------------------------------------------------
# sending and deleting
# --------------------------------------------------------------------------

def test_sending_and_deleting(app, bsky, http_mock):
    create = http_mock.post(f'{PDS}/xrpc/com.atproto.repo.createRecord')
    create.side_effect = [httpx.Response(401, headers={'WWW-Authenticate': 'DPoP error="use_dpop_nonce"',
                                                       'DPoP-Nonce': 'n-9'}),
                          httpx.Response(200, json={'uri': f'at://{DID}/app.bsky.feed.post/3kabc', 'cid': 'c'})]
    delete = http_mock.post(f'{PDS}/xrpc/com.atproto.repo.deleteRecord').respond(200, json={})
    with app.test_request_context():
        sent = bsky.service.send(a_post(body='hi'), tokens(), None)
        bsky.service.delete(tokens(), sent.remote_id)
    assert sent.remote_url == f'https://bsky.app/profile/{DID}/post/3kabc'
    body = json.loads(create.calls.last.request.read())
    assert body['repo'] == DID and body['record']['text'] == 'hi'
    request = create.calls.last.request
    assert request.headers['Authorization'] == 'DPoP at-1'
    assert jwt.decode(request.headers['DPoP'], options={'verify_signature': False})['nonce'] == 'n-9'
    assert json.loads(delete.calls.last.request.read())['rkey'] == '3kabc'


def test_a_record_key_is_a_tid_and_they_sort_by_time():
    first, second = new_tid(), new_tid()
    assert len(first) == 13 and set(first) <= set(records.TID_CHARS) and first[0] in '234567abcdefghij'
    assert first[:9] <= second[:9]   # the leading characters are time, microseconds apart


GET_RECORD = f'{PDS}/xrpc/com.atproto.repo.getRecord'


def test_a_send_chooses_its_record_key_first_and_keeps_it(app, bsky, http_mock):
    create = http_mock.post(f'{PDS}/xrpc/com.atproto.repo.createRecord')
    create.side_effect = [httpx.ReadTimeout('slow'), httpx.Response(200, json={'uri': f'at://{DID}/app.bsky.feed.post/x'})]
    lookup = http_mock.get(GET_RECORD).respond(400, json={'error': 'RecordNotFound', 'message': 'Could not locate'})
    saved = []
    progress = Progress(save=saved.append)
    with app.test_request_context():
        with pytest.raises(MaybeSent):
            bsky.service.send(a_post(body='hi'), tokens(), None, progress)
        rkey = progress.data['rkey']
        assert saved == [{'rkey': rkey}]   # stored before the call that got no answer
        assert json.loads(create.calls.last.request.read())['rkey'] == rkey
        bsky.service.send(a_post(body='hi'), tokens(), None, progress)   # nothing there: made under the same key
    assert dict(lookup.calls.last.request.url.params) == {'repo': DID, 'collection': 'app.bsky.feed.post', 'rkey': rkey}
    assert 'Authorization' not in lookup.calls.last.request.headers
    assert json.loads(create.calls.last.request.read())['rkey'] == rkey and len(saved) == 1


def test_a_record_an_earlier_attempt_made_is_the_copy(app, bsky, http_mock):   # no createRecord: respx refuses one
    http_mock.get(GET_RECORD).respond(200, json={'uri': f'at://{DID}/app.bsky.feed.post/3kold', 'value': {}})
    with app.test_request_context():
        sent = bsky.service.send(a_post(body='hi'), tokens(), None, Progress({'rkey': '3kold'}))
    assert sent.remote_id == f'at://{DID}/app.bsky.feed.post/3kold'
    assert sent.remote_url == f'https://bsky.app/profile/{DID}/post/3kold'


@pytest.mark.parametrize('answer, outcome', [
    (httpx.Response(200, json={}), f'at://{DID}/app.bsky.feed.post/3kk'),
    (httpx.Response(400, json={'error': 'InvalidRequest', 'message': 'bad'}), Permanent),
    (httpx.Response(400, text='not json'), Permanent),
    (httpx.Response(400, json=['odd']), Permanent),
    (httpx.Response(502), Transient),
], ids=['found-no-uri', 'other-error', 'not-json', 'not-object', 'down'])
def test_what_the_record_lookup_makes_of_answers(app, bsky, http_mock, answer, outcome):
    http_mock.get(GET_RECORD).mock(return_value=answer)
    if isinstance(outcome, str):
        assert existing_record(session(), '3kk') == outcome
    else:
        with pytest.raises(outcome):
            existing_record(session(), '3kk')


def test_the_key_command_prints_a_usable_key(app):
    cli.register(app)
    result = app.test_cli_runner().invoke(args=['connections-bluesky-key'])
    key = ECAlgorithm.from_jwk(result.output.strip())
    assert key.curve.name == 'secp256r1' and hasattr(key, 'private_numbers')


# --------------------------------------------------------------------------
# an identity (ATProto spec 2026-11-23 §5, A8): the wider scope
# --------------------------------------------------------------------------

def test_an_identity_sign_in_asks_for_the_wider_scope(app, bsky, http_mock):
    serve_identity(http_mock)
    serve_server(http_mock)
    par = _par(http_mock)
    with app.test_request_context():
        bsky.service.authorize_url(None, 's', 'ignored', {'identity': True}, hint=DID)
    scope = form_of(par.calls.last.request)['scope']
    assert scope == bluesky.IDENTITY_SCOPE and 'repo:app.bsky.graph.block?action=create&action=delete' in scope
    assert bluesky.NOTIFY_SCOPE in bluesky.SCOPE.split() and 'app.bsky.actor.profile' not in scope


def test_an_identity_sign_in_that_granted_it_all(app, bsky, http_mock):
    http_mock.post(f'{PDS}/oauth/token').respond(200, json=_token_answer(scope=bluesky.IDENTITY_SCOPE))
    serve_identity(http_mock)
    serve_server(http_mock)
    with app.test_request_context():
        [account] = bsky.service.finish(None, {'iss': PDS, 'code': 'c'}, 'ignored', {**_pending(), 'identity': True})
    assert account.identity and account.tokens['scope'] == bluesky.IDENTITY_SCOPE
    assert bluesky.identity_granted(bluesky.IDENTITY_SCOPE) and not bluesky.identity_granted(bluesky.SCOPE)


def test_an_identity_sign_in_that_granted_less_changes_nothing(app, bsky, http_mock):
    http_mock.post(f'{PDS}/oauth/token').respond(200, json=_token_answer())
    serve_identity(http_mock)
    serve_server(http_mock)
    with app.test_request_context(), pytest.raises(Permanent):
        bsky.service.finish(None, {'iss': PDS, 'code': 'c'}, 'ignored', {**_pending(), 'identity': True})


def test_a_refresh_keeps_the_scope_it_had_when_the_answer_names_none(app, bsky, http_mock):
    answer = _token_answer(access_token='at-2')
    del answer['scope']
    http_mock.post(f'{PDS}/oauth/token').respond(200, json=answer)
    with app.test_request_context():
        assert bsky.service.refresh(tokens(scope='kept'))['scope'] == 'kept'
