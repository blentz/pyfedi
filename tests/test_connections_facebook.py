"""Connected accounts P3: Facebook Pages (spec 2026-11-13-connected-accounts-design.md §2, §6; C1).

respx plays the Graph API. There is no Facebook to run end to end: the owner's live check with a real Meta app
(spec §11) is the end-to-end test.
"""
import hashlib
import hmac
import httpx
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from app.connections import SERVICES, AuthFailed, Permanent, Transient
from app.connections import facebook
from app.connections.facebook import FacebookService, graph, message_of
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_LINK

GRAPH = 'https://graph.facebook.com/v27.0'
SECRET = 'app-secret'


@pytest.fixture
def fb(app, monkeypatch):
    for name, value in (('FACEBOOK_APP_ID', 'app-1'), ('FACEBOOK_APP_SECRET', SECRET),
                        ('FACEBOOK_GRAPH_VERSION', 'v27.0')):
        monkeypatch.setitem(app.config, name, value)
    return facebook.SERVICE


def sent(route) -> dict:
    """The form fields of the last request (multipart bodies are read by their field names)."""
    request = route.calls.last.request
    body = request.read()
    if request.headers['Content-Type'].startswith('multipart/form-data'):
        return {'_multipart': body}
    return {name: values[0] for name, values in parse_qs(body.decode()).items()}


def a_post(**changes):
    post = SimpleNamespace(title='', body='', ap_id='https://piefed.example/post/7', content_warning=None,
                           nsfw=False, nsfl=False, gallery_count=0, gallery=[], image_id=None, image=None,
                           extensions=None, language=None, type=POST_TYPE_ARTICLE, url=None)
    for name, value in changes.items():
        setattr(post, name, value)
    return post


def page(**changes):
    return SimpleNamespace(remote_id='page-1', **changes)


def tokens(**changes):
    return {'page_token': 'page-token', 'user_token': 'user-token', 'user_id': 'user-1', 'expires_at': None,
            **changes}


def _png(tmp_path, name='a.png') -> SimpleNamespace:
    path = tmp_path / name
    Image.new('RGB', (30, 20), (10, 120, 200)).save(path, 'PNG')
    return SimpleNamespace(file_path=str(path), source_url=None, alt_text='a picture')


# --------------------------------------------------------------------------
# the app and the Graph API
# --------------------------------------------------------------------------

def test_facebook_is_configured_by_its_app(app, monkeypatch):
    service = FacebookService()
    monkeypatch.setitem(app.config, 'FACEBOOK_APP_ID', '')
    assert not service.configured()
    monkeypatch.setitem(app.config, 'FACEBOOK_APP_ID', 'x')
    monkeypatch.setitem(app.config, 'FACEBOOK_APP_SECRET', 'y')
    assert service.configured() and service.fits(None) and service.can_edit and not service.takes_nsfw
    assert SERVICES['facebook'] is facebook.SERVICE


def test_every_call_is_a_post_with_its_token_and_proof_in_the_body(app, fb, http_mock):
    route = http_mock.post(f'{GRAPH}/me').respond(200, json={'id': 'user-1'})
    assert graph('GET', 'me', 'tok', {'fields': 'id'}) == {'id': 'user-1'}
    fields = sent(route)
    assert route.calls.last.request.url.query == b''
    assert fields == {'fields': 'id', 'access_token': 'tok', 'method': 'get',
                      'appsecret_proof': hmac.new(SECRET.encode(), b'tok', hashlib.sha256).hexdigest()}


@pytest.mark.parametrize('status, body, failure, message', [
    (400, {'error': {'message': 'Session expired', 'code': 190}}, AuthFailed, 'Facebook: Session expired'),
    (400, {'error': {'message': 'Permission missing', 'code': 200}}, AuthFailed, 'Facebook: Permission missing'),
    (403, {}, AuthFailed, 'Facebook: HTTP 403'),
    (400, {'error': {'message': 'Too many calls', 'code': 4}}, Transient, 'Facebook: Too many calls'),
    (400, {'error': {'message': 'Try later', 'code': 9999, 'is_transient': True}}, Transient, 'Facebook: Try later'),
    (503, {}, Transient, 'Facebook: HTTP 503'),
    (429, {'error': 'not a dict'}, Transient, 'Facebook: HTTP 429'),
    (400, {'error': {'message': 'Bad parameter', 'code': 100}}, Permanent, 'Facebook: Bad parameter'),
    (200, ['a list'], Permanent, 'Facebook: HTTP 200'),
], ids=['token', 'permission', '403', 'rate', 'transient', '503', 'odd-429', 'request', 'not-a-dict'])
def test_graph_failures_mean_what_their_codes_say(app, fb, http_mock, status, body, failure, message):
    http_mock.post(f'{GRAPH}/x').respond(status, json=body)
    with pytest.raises(failure) as raised:
        graph('POST', 'x', 'tok')
    assert raised.value.message == message


def test_an_answer_that_is_not_json(app, fb, http_mock):
    http_mock.post(f'{GRAPH}/x').respond(502, text='<html>')
    with pytest.raises(Transient):
        graph('POST', 'x', None)


# --------------------------------------------------------------------------
# connecting
# --------------------------------------------------------------------------

def test_the_connect_button_sends_the_member_to_facebook(app, fb):
    url = fb.authorize_url(None, 'state-1', 'https://test.piefed.local/cb', {})
    parts = urlsplit(url)
    query = {name: values[0] for name, values in parse_qs(parts.query).items()}
    assert parts.netloc == 'www.facebook.com' and parts.path == '/v27.0/dialog/oauth'
    assert query == {'client_id': 'app-1', 'redirect_uri': 'https://test.piefed.local/cb', 'state': 'state-1',
                     'scope': facebook.SCOPES, 'response_type': 'code'}


def test_the_callback_offers_the_pages_the_member_may_post_to(app, fb, http_mock):
    token = http_mock.post(f'{GRAPH}/oauth/access_token')
    token.side_effect = [httpx.Response(200, json={'access_token': 'short'}),
                         httpx.Response(200, json={'access_token': 'long'})]
    http_mock.post(f'{GRAPH}/me').respond(200, json={'id': 'user-1'})
    pages = http_mock.post(f'{GRAPH}/me/accounts').respond(200, json={'data': [
        {'id': 'p1', 'name': 'My shop', 'access_token': 'pt1', 'tasks': ['MANAGE', 'CREATE_CONTENT']},
        {'id': 'p2', 'name': 'Read only', 'access_token': 'pt2', 'tasks': ['ANALYZE']},
        {'id': 'p3', 'access_token': 'pt3', 'tasks': ['CREATE_CONTENT']},
        {'id': 'p4', 'name': 'No token', 'tasks': ['CREATE_CONTENT']}, 'junk']})
    accounts = fb.finish(None, {'code': 'c-1'}, 'https://test.piefed.local/cb', {})
    assert [(a.remote_id, a.remote_name) for a in accounts] == [('p1', 'My shop'), ('p3', 'p3')]
    assert accounts[0].tokens == {'page_token': 'pt1', 'user_token': 'long', 'user_id': 'user-1', 'expires_at': None}
    first, second = (parse_qs(call.request.read().decode()) for call in token.calls)
    assert first['code'] == ['c-1'] and first['client_secret'] == [SECRET] and first['method'] == ['get']
    assert second['grant_type'] == ['fb_exchange_token'] and second['fb_exchange_token'] == ['short']
    assert sent(pages)['access_token'] == 'long'


def test_a_callback_with_no_token_connects_nothing(app, fb, http_mock):
    http_mock.post(f'{GRAPH}/oauth/access_token').respond(200, json={})
    with pytest.raises(Permanent):
        fb.finish(None, {'code': 'c'}, 'https://test.piefed.local/cb', {})


# --------------------------------------------------------------------------
# what a post becomes
# --------------------------------------------------------------------------

def test_the_message_is_the_text_and_the_link_back(app):
    assert message_of(a_post(title='Hi', body='there')) == 'Hi\n\nthere\n\nhttps://piefed.example/post/7'
    assert message_of(a_post(body='x', content_warning='Spoilers')) == 'CW: Spoilers\n\nhttps://piefed.example/post/7'
    long = message_of(a_post(body='y' * 70_000))
    assert len(long) <= facebook.MAX_MESSAGE and long.endswith('…\n\nhttps://piefed.example/post/7')


def test_a_text_post(app, fb, http_mock):
    feed = http_mock.post(f'{GRAPH}/page-1/feed').respond(200, json={'id': 'page-1_99'})
    result = fb.send(a_post(body='hello'), tokens(), page())
    assert (result.remote_id, result.remote_url) == ('page-1_99', 'https://www.facebook.com/page-1_99')
    fields = sent(feed)
    assert fields['message'] == 'hello\n\nhttps://piefed.example/post/7' and 'link' not in fields
    assert fields['access_token'] == 'page-token'


def test_a_link_post_passes_its_link_for_the_card(app, fb, http_mock):
    feed = http_mock.post(f'{GRAPH}/page-1/feed').respond(200, json={'id': 'page-1_5'})
    fb.send(a_post(title='A find', type=POST_TYPE_LINK, url='https://news.example/a'), tokens(), page())
    assert sent(feed)['link'] == 'https://news.example/a'


def test_one_picture_is_a_photo_post(app, fb, http_mock, tmp_path):
    photos = http_mock.post(f'{GRAPH}/page-1/photos').respond(200, json={'id': 'ph-1', 'post_id': 'page-1_7'})
    post = a_post(body='look', image_id=1, image=_png(tmp_path))
    assert fb.send(post, tokens(), page()).remote_id == 'page-1_7'
    body = sent(photos)['_multipart']
    assert b'name="caption"' in body and b'look' in body and b'name="published"\r\n\r\ntrue' in body
    assert b'filename="picture.jpg"' in body and b'\xff\xd8' in body


def test_a_photo_answer_without_its_post_id(app, fb, http_mock, tmp_path):
    http_mock.post(f'{GRAPH}/page-1/photos').respond(200, json={'id': 'ph-2'})
    assert fb.send(a_post(body='x', image_id=1, image=_png(tmp_path)), tokens(), page()).remote_id == 'page-1_ph-2'


def test_a_gallery_is_one_post_of_its_photos(app, fb, http_mock, tmp_path):
    uploads = http_mock.post(f'{GRAPH}/page-1/photos')
    uploads.side_effect = [httpx.Response(200, json={'id': f'ph-{n}'}) for n in range(2)]
    feed = http_mock.post(f'{GRAPH}/page-1/feed').respond(200, json={'id': 'page-1_8'})
    files = [_png(tmp_path, 'a.png'), SimpleNamespace(file_path=None, source_url=None, alt_text=''),
             _png(tmp_path, 'b.png')]
    post = a_post(body='trip', gallery_count=3, gallery=files, type=POST_TYPE_LINK, url='https://x.example')
    fb.send(post, tokens(), page())
    assert uploads.call_count == 2 and b'name="published"\r\n\r\nfalse' in sent(uploads)['_multipart']
    fields = sent(feed)
    assert fields['attached_media[0]'] == '{"media_fbid":"ph-0"}' and fields['attached_media[1]'] == '{"media_fbid":"ph-1"}'
    assert 'link' not in fields   # the photos are the post, not a card


@pytest.mark.parametrize('changes', [{'extensions': {'media': {'mime': 'video/mp4'}}}, {'content_warning': 'CW'}],
                         ids=['video', 'warning'])
def test_a_video_or_a_warning_goes_as_text(app, fb, http_mock, tmp_path, changes):
    feed = http_mock.post(f'{GRAPH}/page-1/feed').respond(200, json={'id': 'page-1_9'})
    fb.send(a_post(body='clip', image_id=1, image=_png(tmp_path), **changes), tokens(), page())
    assert 'attached_media[0]' not in sent(feed)


# --------------------------------------------------------------------------
# edits, deletes, disconnecting
# --------------------------------------------------------------------------

def test_an_edit_a_delete_and_a_revoke(app, fb, http_mock):
    edit = http_mock.post(f'{GRAPH}/page-1_9').respond(200, json={'success': True})
    fb.edit(a_post(body='renamed'), tokens(), 'page-1_9')
    assert sent(edit)['message'] == 'renamed\n\nhttps://piefed.example/post/7' and 'method' not in sent(edit)
    fb.delete(tokens(), 'page-1_9')
    assert sent(edit)['method'] == 'delete'
    revoke = http_mock.post(f'{GRAPH}/user-1/permissions').respond(200, json={'success': True})
    fb.revoke(tokens())
    fb.revoke(tokens(user_token=None))
    assert revoke.call_count == 1 and sent(revoke)['method'] == 'delete' and sent(revoke)['access_token'] == 'user-token'
