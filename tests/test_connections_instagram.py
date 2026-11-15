"""Connected accounts P4: Instagram, both ways in (spec 2026-11-13-connected-accounts-design.md §2, §6; C6).

respx plays graph.facebook.com, graph.instagram.com and api.instagram.com. There is no Instagram to run end to end:
the owner's live check with a reviewed Meta app (spec §11) is the end-to-end test.
"""
import io
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from app import db
from app.connections import (SERVICES, AuthFailed, MaybeSent, Pending, Permanent, Progress, Transient,
                             Unconfirmed)
from app.connections import facebook, instagram
from app.connections.instagram import (DIRECT, VIA_FACEBOOK, InstagramService, caption_of, fitted, media_file_id,
                                       media_url)
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_GALLERY, POST_TYPE_IMAGE
from app.models import File

pytestmark = pytest.mark.usefixtures('site')

FB = 'https://graph.facebook.com/v27.0'
IG = 'https://graph.instagram.com/v27.0'


@pytest.fixture
def ig(app, monkeypatch):
    for name, value in (('FACEBOOK_APP_ID', 'fb-app'), ('FACEBOOK_APP_SECRET', 'fb-secret'),
                        ('INSTAGRAM_APP_ID', 'ig-app'), ('INSTAGRAM_APP_SECRET', 'ig-secret'),
                        ('FACEBOOK_GRAPH_VERSION', 'v27.0')):
        monkeypatch.setitem(app.config, name, value)
    return instagram.SERVICE


def fields(route, call=-1) -> dict:
    request = route.calls[call].request
    if request.method == 'GET':
        return {name: values[0] for name, values in parse_qs(urlsplit(str(request.url)).query).items()}
    return {name: values[0] for name, values in parse_qs(request.read().decode()).items()}


def picture(tmp_path, name='a.png', size=(40, 40), file_id=11):
    path = tmp_path / name
    Image.new('RGB', size, (200, 50, 50)).save(path, 'PNG')
    return SimpleNamespace(id=file_id, file_path=str(path), source_url=None, alt_text=f'alt {name}')


def a_post(files=(), **changes):
    files = list(files)
    post = SimpleNamespace(title='', body='', ap_id='https://piefed.example/post/7', content_warning=None, nsfw=False,
                           nsfl=False, gallery_count=len(files) if len(files) > 1 else 0, gallery=files,
                           image_id=files[0].id if len(files) == 1 else None, image=files[0] if len(files) == 1 else None,
                           extensions=None, language=None, type=POST_TYPE_IMAGE, url=None, tags=[])
    for name, value in changes.items():
        setattr(post, name, value)
    return post


def account(variant):
    return SimpleNamespace(remote_id='ig-1', variant=variant)


# --------------------------------------------------------------------------
# the service and its two ways in
# --------------------------------------------------------------------------

def test_the_ways_in_follow_the_credentials(app, monkeypatch):
    service = InstagramService()
    for name in ('FACEBOOK_APP_ID', 'FACEBOOK_APP_SECRET', 'INSTAGRAM_APP_ID', 'INSTAGRAM_APP_SECRET'):
        monkeypatch.setitem(app.config, name, '')
    assert service.variants == () and not service.configured()
    monkeypatch.setitem(app.config, 'INSTAGRAM_APP_ID', 'i')
    monkeypatch.setitem(app.config, 'INSTAGRAM_APP_SECRET', 's')
    assert service.variants == (DIRECT,)
    monkeypatch.setitem(app.config, 'FACEBOOK_APP_ID', 'f')
    monkeypatch.setitem(app.config, 'FACEBOOK_APP_SECRET', 's')
    assert service.variants == (VIA_FACEBOOK, DIRECT) and service.configured()
    assert service.variant_label(VIA_FACEBOOK) == 'Instagram, through Facebook'
    assert service.variant_label(DIRECT) == 'Instagram'
    assert service.can_delete(VIA_FACEBOOK) and not service.can_delete(DIRECT)
    assert not service.can_edit and not service.takes_nsfw and SERVICES['instagram'] is instagram.SERVICE


def test_only_posts_with_pictures_fit(app, tmp_path):
    service = InstagramService()
    assert service.fits(a_post([picture(tmp_path)]))
    assert not service.fits(a_post())
    assert not service.fits(a_post([picture(tmp_path)], extensions={'media': {'mime': 'video/mp4'}}))
    assert service.fits_type(POST_TYPE_IMAGE) and service.fits_type(POST_TYPE_GALLERY)
    assert not service.fits_type(POST_TYPE_ARTICLE)


def test_the_two_connect_buttons(app, ig):
    via_facebook = urlsplit(ig.authorize_url(VIA_FACEBOOK, 's1', 'https://x/cb', {}))
    query = parse_qs(via_facebook.query)
    assert via_facebook.netloc == 'www.facebook.com' and query['client_id'] == ['fb-app']
    assert 'instagram_manage_contents' in query['scope'][0] and query['state'] == ['s1']
    direct = urlsplit(ig.authorize_url(DIRECT, 's2', 'https://x/cb', {}))
    query = parse_qs(direct.query)
    assert direct.netloc == 'www.instagram.com' and query['client_id'] == ['ig-app']
    assert query['scope'] == [instagram.DIRECT_SCOPES] and query['redirect_uri'] == ['https://x/cb']


def test_connecting_through_facebook_offers_the_linked_accounts(app, ig, http_mock):
    token = http_mock.post(f'{FB}/oauth/access_token')
    token.side_effect = [httpx.Response(200, json={'access_token': 'short'}),
                         httpx.Response(200, json={'access_token': 'long'})]
    http_mock.post(f'{FB}/me').respond(200, json={'id': 'user-1'})
    pages = http_mock.post(f'{FB}/me/accounts').respond(200, json={'data': [
        {'id': 'p1', 'access_token': 'pt1', 'instagram_business_account': {'id': 'ig-9', 'username': 'shop'}},
        {'id': 'p2', 'access_token': 'pt2'},
        {'id': 'p3', 'access_token': 'pt3', 'instagram_business_account': {'id': 'ig-8'}},
        {'id': 'p4', 'instagram_business_account': {'id': 'ig-7'}}, 'junk']})
    accounts = ig.finish(VIA_FACEBOOK, {'code': 'c'}, 'https://x/cb', {})
    assert [(a.remote_id, a.remote_name, a.variant) for a in accounts] == [
        ('ig-9', '@shop', VIA_FACEBOOK), ('ig-8', '@ig-8', VIA_FACEBOOK)]
    assert accounts[0].tokens == {'token': 'pt1', 'user_token': 'long', 'user_id': 'user-1', 'expires_at': None}
    assert 'instagram_business_account' in fields(pages)['fields']


def test_connecting_through_facebook_with_no_token(app, ig, http_mock):
    http_mock.post(f'{FB}/oauth/access_token').respond(200, json={})
    with pytest.raises(Permanent):
        ig.finish(VIA_FACEBOOK, {'code': 'c'}, 'https://x/cb', {})


def test_connecting_directly(app, ig, http_mock):
    short = http_mock.post('https://api.instagram.com/oauth/access_token').respond(
        200, json={'access_token': 'short', 'user_id': 123})
    exchange = http_mock.get('https://graph.instagram.com/access_token').respond(
        200, json={'access_token': 'long', 'expires_in': 5_184_000})
    me = http_mock.get(f'{IG}/me').respond(200, json={'user_id': '17841', 'username': 'artist'})
    [connected] = ig.finish(DIRECT, {'code': 'c'}, 'https://x/cb', {})
    assert (connected.remote_id, connected.remote_name, connected.variant) == ('17841', '@artist', DIRECT)
    assert connected.tokens['token'] == 'long' and connected.tokens['expires_at'] > time.time() + 5_000_000
    sent = fields(short)
    assert sent['client_secret'] == 'ig-secret' and sent['grant_type'] == 'authorization_code' and sent['code'] == 'c'
    assert fields(exchange)['grant_type'] == 'ig_exchange_token'
    assert me.calls.last.request.headers['Authorization'] == 'Bearer long'
    assert 'access_token' not in str(me.calls.last.request.url)


@pytest.mark.parametrize('case', ['no-short', 'no-long', 'no-account'])
def test_a_direct_connection_that_gets_no_account(app, ig, http_mock, case):
    http_mock.post('https://api.instagram.com/oauth/access_token').respond(
        200, json={} if case == 'no-short' else {'access_token': 'short'})
    if case != 'no-short':
        http_mock.get('https://graph.instagram.com/access_token').respond(
            200, json={} if case == 'no-long' else {'access_token': 'long', 'expires_in': 100})
    if case == 'no-account':
        http_mock.get(f'{IG}/me').respond(200, json={})
    with pytest.raises(Permanent):
        ig.finish(DIRECT, {'code': 'c'}, 'https://x/cb', {})


def test_a_direct_token_is_renewed_a_week_before_it_ends(app, ig, http_mock):
    assert not ig.expired({'expires_at': None})
    assert not ig.expired({'expires_at': time.time() + 30 * 86400})
    assert ig.expired({'expires_at': time.time() + 3 * 86400})
    http_mock.get('https://graph.instagram.com/refresh_access_token').respond(
        200, json={'access_token': 'renewed', 'expires_in': 5_184_000})
    renewed = ig.refresh({'token': 'old', 'expires_at': 1})
    assert renewed['token'] == 'renewed' and renewed['expires_at'] > time.time() + 5_000_000


def test_a_token_instagram_will_not_renew(app, ig, http_mock):
    http_mock.get('https://graph.instagram.com/refresh_access_token').respond(200, json={})
    with pytest.raises(AuthFailed):
        ig.refresh({'token': 'old', 'expires_at': 1})


# --------------------------------------------------------------------------
# the pictures Instagram fetches
# --------------------------------------------------------------------------

def test_a_media_address_names_its_file_for_an_hour(app, monkeypatch):
    with app.test_request_context():
        url = media_url(42)
    assert url.startswith('https://test.piefed.local/connections/media/') and url.endswith('.jpg')
    token = url.rsplit('/', 1)[1].removesuffix('.jpg')
    assert media_file_id(token) == 42
    assert media_file_id(token[:-2] + 'xx') is None
    assert media_file_id(instagram._signer().dumps(['not', 'a', 'dict'])) is None
    assert media_file_id(instagram._signer().dumps({'f': True})) is None
    monkeypatch.setattr(instagram, 'MEDIA_SECONDS', -1)
    assert media_file_id(token) is None


@pytest.mark.parametrize('size, expected', [((400, 400), (400, 400)), ((200, 800), (640, 800)),
                                            ((1000, 200), (1000, 524))],
                         ids=['square', 'tall', 'wide'])
def test_pictures_are_padded_into_instagrams_shapes(app, size, expected):
    out = io.BytesIO()
    Image.new('RGB', size, (0, 0, 0)).save(out, 'PNG')
    jpeg = fitted(out.getvalue())
    assert jpeg[:2] == b'\xff\xd8' and Image.open(io.BytesIO(jpeg)).size == expected
    assert fitted(b'not a picture') is None


def test_the_media_route_serves_the_fitted_picture(app, tmp_path):
    path = tmp_path / 'p.png'
    Image.new('RGB', (200, 800), (0, 0, 0)).save(path, 'PNG')
    stored = File(file_path=str(path))
    gone = File(file_path=str(tmp_path / 'missing.png'))
    db.session.add_all([stored, gone])
    db.session.commit()
    client = app.test_client()
    with app.test_request_context():
        good, missing = media_url(stored.id), media_url(gone.id)
    response = client.get(urlsplit(good).path)
    assert response.status_code == 200 and response.headers['Content-Type'] == 'image/jpeg'
    assert Image.open(io.BytesIO(response.data)).size == (640, 800)
    assert client.get(urlsplit(missing).path).status_code == 404
    assert client.get('/connections/media/forged.jpg').status_code == 404
    with app.test_request_context():
        nowhere = media_url(999_999)
    assert client.get(urlsplit(nowhere).path).status_code == 404


# --------------------------------------------------------------------------
# what a post becomes
# --------------------------------------------------------------------------

def test_the_caption(app, tmp_path):
    post = a_post([picture(tmp_path)], title='Sunset', body='over the bay',
                  tags=[SimpleNamespace(name='photo'), SimpleNamespace(name='sky'), SimpleNamespace(name=None)])
    assert caption_of(post) == 'Sunset\n\nover the bay\n\n#photo #sky\n\nhttps://piefed.example/post/7'
    many = a_post([picture(tmp_path, f'{n}.png', file_id=n) for n in range(12)], body='lots')
    with app.test_request_context():
        assert caption_of(many) == 'lots\n\n(+2 more pictures)\n\nhttps://piefed.example/post/7'
    long = caption_of(a_post([picture(tmp_path)], body='z' * 5000))
    assert len(long) <= instagram.MAX_CAPTION and long.endswith('https://piefed.example/post/7')


def test_one_picture_through_facebook(app, ig, http_mock, tmp_path):
    media = http_mock.post(f'{FB}/ig-1/media').respond(200, json={'id': 'container-1'})
    status = http_mock.post(f'{FB}/container-1').respond(200, json={'status_code': 'FINISHED'})
    publish = http_mock.post(f'{FB}/ig-1/media_publish').respond(200, json={'id': 'm-1'})
    http_mock.post(f'{FB}/m-1').respond(200, json={'permalink': 'https://www.instagram.com/p/abc/'})
    with app.test_request_context():
        sent = ig.send(a_post([picture(tmp_path)], body='hi'), {'token': 'pt'}, account(VIA_FACEBOOK))
    assert (sent.remote_id, sent.remote_url) == ('m-1', 'https://www.instagram.com/p/abc/')
    made = fields(media)
    assert made['image_url'].startswith('https://test.piefed.local/connections/media/')
    assert made['caption'] == 'hi\n\nhttps://piefed.example/post/7' and made['alt_text'] == 'alt a.png'
    assert made['access_token'] == 'pt' and made['appsecret_proof']
    assert fields(publish)['creation_id'] == 'container-1'
    assert fields(status)['fields'] == 'status_code' and fields(status)['method'] == 'get'


def test_a_carousel_directly(app, ig, http_mock, tmp_path):
    media = http_mock.post(f'{IG}/ig-1/media')
    media.side_effect = [httpx.Response(200, json={'id': f'child-{n}'}) for n in range(2)] + \
        [httpx.Response(200, json={'id': 'carousel'})]
    http_mock.get(f'{IG}/carousel').respond(200, json={'status_code': 'FINISHED'})
    http_mock.post(f'{IG}/ig-1/media_publish').respond(200, json={'id': 'm-2'})
    http_mock.get(f'{IG}/m-2').respond(503)
    files = [picture(tmp_path, 'a.png', file_id=1), SimpleNamespace(id=2, file_path=None, source_url=None, alt_text=''),
             picture(tmp_path, 'b.png', file_id=3)]
    with app.test_request_context():
        sent = ig.send(a_post(files, body='set', type=POST_TYPE_GALLERY), {'token': 'direct'}, account(DIRECT))
    assert (sent.remote_id, sent.remote_url) == ('m-2', None)   # published; its link could not be read
    child, carousel = fields(media, 0), fields(media, 2)
    assert child['is_carousel_item'] == 'true' and 'caption' not in child
    assert carousel == {'media_type': 'CAROUSEL', 'children': 'child-0,child-1', 'caption': 'set\n\nhttps://piefed.example/post/7'}
    assert media.calls[0].request.headers['Authorization'] == 'Bearer direct'


def test_a_published_copy_keeps_its_id_whatever_its_link_lookup_meets(app, ig, http_mock, tmp_path):
    http_mock.post(f'{IG}/ig-1/media').respond(200, json={'id': 'c'})
    http_mock.get(f'{IG}/c').respond(200, json={'status_code': 'FINISHED'})
    http_mock.post(f'{IG}/ig-1/media_publish').respond(200, json={'id': 'm-3'})
    http_mock.get(f'{IG}/m-3').respond(400, json={'error': {'message': 'no', 'code': 100}})
    with app.test_request_context():
        sent = ig.send(a_post([picture(tmp_path)]), {'token': 'direct'}, account(DIRECT))
    assert (sent.remote_id, sent.remote_url) == ('m-3', None)


def test_no_answer_to_media_publish_is_unconfirmed_and_to_a_container_is_tried_again(app, ig, http_mock, tmp_path):
    media = http_mock.post(f'{IG}/ig-1/media')
    media.side_effect = [httpx.ReadTimeout('slow'), httpx.Response(200, json={'id': 'c'})]
    http_mock.get(f'{IG}/c').respond(200, json={'status_code': 'FINISHED'})
    publish = http_mock.post(f'{IG}/ig-1/media_publish').mock(side_effect=httpx.ReadTimeout('slow'))
    with app.test_request_context():
        with pytest.raises(MaybeSent) as raised:
            ig.send(a_post([picture(tmp_path)]), {'token': 'direct'}, account(DIRECT))
        assert not isinstance(raised.value, Unconfirmed) and not publish.called
        with pytest.raises(Unconfirmed):
            ig.send(a_post([picture(tmp_path)]), {'token': 'direct'}, account(DIRECT))


def test_a_container_not_ready_yet_is_published_when_it_is(app, ig, http_mock, tmp_path):
    http_mock.post(f'{IG}/ig-1/media').respond(200, json={'id': 'c-5'})
    status = http_mock.get(f'{IG}/c-5')
    status.side_effect = [httpx.Response(200, json={'status_code': 'IN_PROGRESS'}),
                          httpx.Response(200, json={'status_code': 'FINISHED'})]
    publish = http_mock.post(f'{IG}/ig-1/media_publish').respond(200, json={'id': 'm-5'})
    http_mock.get(f'{IG}/m-5').respond(200, json={'permalink': 'https://www.instagram.com/p/five/'})
    with app.test_request_context():
        pending = ig.send(a_post([picture(tmp_path)]), {'token': 'direct'}, account(DIRECT))
        assert pending == Pending({'container': 'c-5'}) and not publish.called
        sent = ig.resume(None, {'token': 'direct'}, account(DIRECT), Progress(pending.data))
    assert (sent.remote_id, sent.remote_url) == ('m-5', 'https://www.instagram.com/p/five/')
    assert fields(publish)['creation_id'] == 'c-5'


@pytest.mark.parametrize('status, failure', [('ERROR', Permanent), ('EXPIRED', Permanent),
                                             ('PUBLISHED', Unconfirmed)])
def test_a_container_instagram_will_not_publish(app, ig, http_mock, status, failure):
    http_mock.get(f'{IG}/c-6').respond(200, json={'status_code': status})
    with app.test_request_context(), pytest.raises(failure):
        ig.resume(None, {'token': 'direct'}, account(DIRECT), Progress({'container': 'c-6'}))


def test_a_post_whose_pictures_cannot_be_read(app, ig):
    files = [SimpleNamespace(id=1, file_path=None, source_url=None, alt_text='')]
    with app.test_request_context(), pytest.raises(Permanent):
        ig.send(a_post(files), {'token': 't'}, account(DIRECT))


def test_only_the_facebook_route_shares_graphs_pauses(app, ig, redis_double):
    facebook.pause('object', 'ig-1', 600)
    assert ig.paused_until(account(VIA_FACEBOOK)) is not None
    assert ig.paused_until(account(DIRECT)) is None


def test_a_delete_and_a_revoke(app, ig, http_mock):
    route = http_mock.post(f'{FB}/m-1').respond(200, json={'success': True})
    ig.delete({'token': 'pt'}, 'm-1')
    assert fields(route)['method'] == 'delete'
    revoke = http_mock.post(f'{FB}/user-1/permissions').respond(200, json={'success': True})
    ig.revoke({'token': 'pt', 'user_token': 'ut', 'user_id': 'user-1'})
    ig.revoke({'token': 'direct'})
    assert revoke.call_count == 1
