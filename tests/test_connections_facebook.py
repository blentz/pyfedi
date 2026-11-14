"""Connected accounts P3: Facebook Pages (spec 2026-11-13-connected-accounts-design.md §2, §6; C1).

respx plays the Graph API. There is no Facebook to run end to end: the owner's live check with a real Meta app
(spec §11) is the end-to-end test.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from app.clock import utcnow
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


def test_a_warning_goes_as_text(app, fb, http_mock, tmp_path):
    feed = http_mock.post(f'{GRAPH}/page-1/feed').respond(200, json={'id': 'page-1_9'})
    post = a_post(body='clip', image_id=1, image=_png(tmp_path), content_warning='CW',
                  extensions={'media': {'mime': 'video/mp4'}})
    fb.send(post, tokens(), page())
    assert 'attached_media[0]' not in sent(feed)


# --------------------------------------------------------------------------
# video, by Graph's chunked upload (spec 18.3)
# --------------------------------------------------------------------------

def _video_post(monkeypatch, tmp_path, size=2500):
    video = tmp_path / 'v.mp4'
    video.write_bytes(bytes(range(256)) * (size // 256) + b'x' * (size % 256))
    monkeypatch.setattr('app.connections.content.media_local_path', lambda post: str(video))
    return a_post(body='my clip', extensions={'media': {'mime': 'video/mp4'}}), video.read_bytes()


def test_a_video_goes_up_in_the_chunks_graph_asks_for(app, fb, http_mock, monkeypatch, tmp_path):
    post, data = _video_post(monkeypatch, tmp_path)
    videos = http_mock.post(f'{GRAPH}/page-1/videos')
    videos.side_effect = [
        httpx.Response(200, json={'upload_session_id': 's1', 'video_id': 'v9', 'start_offset': '0', 'end_offset': '1000'}),
        httpx.Response(200, json={'start_offset': '1000', 'end_offset': '2500'}),
        httpx.Response(200, json={'start_offset': '2500', 'end_offset': '2500'}),
        httpx.Response(200, json={'success': True})]
    result = fb.send(post, tokens(), page())
    assert (result.remote_id, result.remote_url) == ('video:v9', 'https://www.facebook.com/page-1/videos/v9')
    start = parse_qs(videos.calls[0].request.read().decode())
    assert start['upload_phase'] == ['start'] and start['file_size'] == ['2500']
    first, second = videos.calls[1].request.read(), videos.calls[2].request.read()
    assert data[:1000] in first and b'name="start_offset"\r\n\r\n0' in first
    assert data[1000:] in second and b'name="start_offset"\r\n\r\n1000' in second
    finish = parse_qs(videos.calls[3].request.read().decode())
    assert finish['upload_phase'] == ['finish'] and finish['description'] == ['my clip\n\nhttps://piefed.example/post/7']


@pytest.mark.parametrize('answers', [
    [{'start_offset': '0', 'end_offset': '10'}],
    [{'upload_session_id': 's', 'video_id': 'v', 'start_offset': '0', 'end_offset': '1000'},
     {'start_offset': '0', 'end_offset': '1000'}],
    [{'upload_session_id': 's', 'video_id': 'v', 'start_offset': '0', 'end_offset': '2500'},
     {'start_offset': '2500', 'end_offset': '2500'}, {'success': False}],
], ids=['no-session', 'stalled', 'not-finished'])
def test_a_video_upload_that_does_not_complete(app, fb, http_mock, monkeypatch, tmp_path, answers):
    post, _data = _video_post(monkeypatch, tmp_path)
    http_mock.post(f'{GRAPH}/page-1/videos').side_effect = [httpx.Response(200, json=a) for a in answers]
    with pytest.raises(Transient):
        fb.send(post, tokens(), page())


def test_a_video_copy_is_edited_and_deleted_as_a_video(app, fb, http_mock):
    route = http_mock.post(f'{GRAPH}/v9').respond(200, json={'success': True})
    fb.edit(a_post(body='renamed'), tokens(), 'video:v9')
    assert sent(route)['description'] == 'renamed\n\nhttps://piefed.example/post/7'
    fb.delete(tokens(), 'video:v9')
    assert sent(route)['method'] == 'delete'


# --------------------------------------------------------------------------
# pausing before Graph refuses (spec 18.3)
# --------------------------------------------------------------------------

def test_an_app_nearly_out_of_calls_pauses_every_copy(app, fb, http_mock, redis_double):
    http_mock.post(f'{GRAPH}/x').respond(200, json={}, headers={
        'X-App-Usage': json.dumps({'call_count': 95, 'total_time': 10, 'total_cputime': 10})})
    assert facebook.paused_until('page-1') is None
    graph('POST', 'x', 'tok')
    until = facebook.paused_until('page-1')
    assert until is not None and utcnow() + timedelta(minutes=55) < until <= utcnow() + timedelta(hours=1, seconds=5)
    assert facebook.paused_until(None) == until   # every account of the app


@pytest.mark.parametrize('minutes, expected', [(7, timedelta(minutes=7)), (0, timedelta(hours=1))],
                         ids=['graph-says', 'graph-silent'])
def test_a_page_nearly_out_of_calls_pauses_that_page(app, fb, http_mock, redis_double, minutes, expected):
    http_mock.post(f'{GRAPH}/x').respond(200, json={}, headers={'X-Business-Use-Case-Usage': json.dumps({
        'page-1': [{'type': 'pages', 'call_count': 99, 'total_cputime': 1, 'total_time': 1,
                    'estimated_time_to_regain_access': minutes}], 'page-2': 'junk'})})
    graph('POST', 'x', 'tok')
    until = facebook.paused_until('page-1')
    assert until is not None and abs((until - utcnow()) - expected) < timedelta(seconds=10)
    assert facebook.paused_until('page-3') is None


def test_usage_under_the_limit_or_unreadable_pauses_nothing(app, fb, http_mock, redis_double):
    http_mock.post(f'{GRAPH}/x').respond(200, json={}, headers={
        'X-App-Usage': '{not json', 'X-Business-Use-Case-Usage': json.dumps({'p': [{'call_count': 'many'}]})})
    graph('POST', 'x', 'tok')
    http_mock.post(f'{GRAPH}/y').respond(200, json={}, headers={'X-App-Usage': json.dumps({'call_count': 50})})
    graph('POST', 'y', 'tok')
    assert facebook.paused_until('p') is None


def test_a_rate_limit_naming_no_account_pauses_the_app_briefly(app, fb, http_mock, redis_double):
    http_mock.post(f'{GRAPH}/x').respond(400, json={'error': {'message': 'Too many', 'code': 4}})
    with pytest.raises(Transient):
        graph('POST', 'x', 'tok')
    until = facebook.paused_until(None)
    assert until is not None and until <= utcnow() + timedelta(minutes=15, seconds=5)


def test_a_paused_until_already_past_is_no_pause(app, fb, redis_double):
    facebook.pause('object', 'page-1', 1)
    redis_double.set(facebook._pause_key('object', 'page-1'), int(time.time()) - 5)
    assert facebook.paused_until('page-1') is None
    facebook.pause('object', None, 60)
    facebook.pause('object', 'page-1', 0)
    assert facebook.paused_until('page-1') is None


def test_the_service_asks_for_its_page(app, fb, redis_double):
    facebook.pause('object', 'page-1', 600)
    assert fb.paused_until(SimpleNamespace(remote_id='page-1')) is not None
    assert fb.paused_until(SimpleNamespace(remote_id='page-2')) is None


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
