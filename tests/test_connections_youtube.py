"""Connected accounts P5: YouTube (spec 2026-11-13-connected-accounts-design.md §2, §6, §8).

respx plays Google's token endpoints, the YouTube Data API and its upload host. There is no YouTube to run end to
end: the owner's live check with a verified Google app (spec §11) is the end-to-end test.
"""
import base64
import hashlib
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.clock import utcnow
from app.connections import SERVICES, AuthFailed, Permanent, QuotaWait, Transient
from app.connections import youtube
from app.connections.youtube import (YouTubeService, answer_of, next_reset, snippet_of, take_upload)
from app.constants import POST_TYPE_IMAGE, POST_TYPE_VIDEO

API = 'https://www.googleapis.com/youtube/v3'
UPLOAD = 'https://www.googleapis.com/upload/youtube/v3'
SESSION = f'{UPLOAD}/videos?uploadType=resumable&upload_id=abc'


@pytest.fixture
def yt(app, monkeypatch, redis_double):
    monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_CLIENT_ID', 'client-1')
    monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_SECRET', 'secret-1')
    monkeypatch.setitem(app.config, 'YOUTUBE_DAILY_UPLOADS', 100)
    return youtube.SERVICE


def form(route, call=-1) -> dict:
    return {name: values[0] for name, values in parse_qs(route.calls[call].request.read().decode()).items()}


def a_post(**changes):
    post = SimpleNamespace(title='My clip', body='what happened', ap_id='https://piefed.example/post/7',
                           content_warning=None, nsfw=False, nsfl=False, image_id=None, image=None,
                           extensions={'media': {'mime': 'video/mp4'}}, type=POST_TYPE_VIDEO,
                           url='https://piefed.example/static/media/v.mp4', tags=[], gallery_count=0, gallery=[])
    for name, value in changes.items():
        setattr(post, name, value)
    return post


def tokens(**changes):
    return {'access_token': 'at-1', 'refresh_token': 'rt-1', 'expires_at': time.time() + 3600, **changes}


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------

def test_youtube_is_configured_by_its_google_client(app, monkeypatch):
    service = YouTubeService()
    monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_CLIENT_ID', '')
    assert not service.configured()
    monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_CLIENT_ID', 'c')
    monkeypatch.setitem(app.config, 'YOUTUBE_OAUTH_SECRET', 's')
    assert service.configured() and service.can_edit and not service.takes_nsfw
    assert service.fits(a_post()) and not service.fits(a_post(extensions=None))
    assert service.fits_type(POST_TYPE_VIDEO) and not service.fits_type(POST_TYPE_IMAGE)
    assert SERVICES['youtube'] is youtube.SERVICE


def test_the_connect_button_asks_google_with_pkce(app, yt):
    pending = {}
    url = urlsplit(yt.authorize_url(None, 's-1', 'https://x/cb', pending))
    query = {name: values[0] for name, values in parse_qs(url.query).items()}
    assert url.netloc == 'accounts.google.com' and query['scope'] == youtube.SCOPE
    assert (query['access_type'], query['prompt'], query['state']) == ('offline', 'consent', 's-1')
    expected = base64.urlsafe_b64encode(hashlib.sha256(pending['verifier'].encode()).digest()).rstrip(b'=').decode()
    assert query['code_challenge'] == expected and query['code_challenge_method'] == 'S256'


def test_the_callback_connects_the_channel(app, yt, http_mock):
    token = http_mock.post('https://oauth2.googleapis.com/token').respond(200, json={
        'access_token': 'at', 'refresh_token': 'rt', 'expires_in': 3599})
    channels = http_mock.get(f'{API}/channels').respond(200, json={'items': [
        {'id': 'UC1', 'snippet': {'title': 'My channel'}}]})
    [account] = yt.finish(None, {'code': 'c-1'}, 'https://x/cb', {'verifier': 'v-1'})
    assert (account.remote_id, account.remote_name) == ('UC1', 'My channel')
    assert account.tokens['refresh_token'] == 'rt' and account.tokens['expires_at'] > time.time() + 3500
    sent = form(token)
    assert (sent['code'], sent['code_verifier'], sent['grant_type']) == ('c-1', 'v-1', 'authorization_code')
    request = channels.calls.last.request
    assert request.headers['Authorization'] == 'Bearer at' and 'mine=true' in str(request.url)


@pytest.mark.parametrize('case', ['no-refresh-token', 'no-channel'])
def test_a_callback_that_connects_nothing(app, yt, http_mock, case):
    http_mock.post('https://oauth2.googleapis.com/token').respond(200, json={
        'access_token': 'at', **({} if case == 'no-refresh-token' else {'refresh_token': 'rt'})})
    if case == 'no-channel':
        http_mock.get(f'{API}/channels').respond(200, json={'items': []})
    with pytest.raises(Permanent):
        yt.finish(None, {'code': 'c'}, 'https://x/cb', {'verifier': 'v'})


def test_a_refresh(app, yt, http_mock):
    route = http_mock.post('https://oauth2.googleapis.com/token').respond(200, json={'access_token': 'at-2',
                                                                                     'expires_in': 3599})
    renewed = yt.refresh(tokens())
    assert (renewed['access_token'], renewed['refresh_token']) == ('at-2', 'rt-1')
    assert form(route)['grant_type'] == 'refresh_token'


@pytest.mark.parametrize('answer', [httpx.Response(200, json={}),
                                    httpx.Response(400, json={'error': 'invalid_grant'})],
                         ids=['no-token', 'invalid-grant'])
def test_a_refresh_that_ends_the_connection(app, yt, http_mock, answer):
    http_mock.post('https://oauth2.googleapis.com/token').mock(return_value=answer)
    with pytest.raises(AuthFailed):
        yt.refresh(tokens())


def test_revoking(app, yt, http_mock):
    route = http_mock.post('https://oauth2.googleapis.com/revoke').respond(200)
    yt.revoke(tokens())
    yt.revoke(tokens(refresh_token=None))
    assert route.call_count == 1 and form(route)['token'] == 'rt-1'


# --------------------------------------------------------------------------
# Google's answers, and the daily allowance
# --------------------------------------------------------------------------

def _error(status, reason=None, message='nope'):
    errors = [{'reason': reason}] if reason else []
    return httpx.Response(status, json={'error': {'code': status, 'message': message, 'errors': errors}})


@pytest.mark.parametrize('response, failure', [
    (_error(403, 'quotaExceeded'), QuotaWait),
    (_error(403, 'uploadLimitExceeded'), QuotaWait),
    (_error(401), AuthFailed),
    (_error(403, 'insufficientPermissions'), AuthFailed),
    (_error(429), Transient),
    (_error(503), Transient),
    (_error(400, 'invalidTitle', 'bad title'), Permanent),
    (httpx.Response(404, text='<html>'), Permanent),
    (httpx.Response(400, json={'error': 'odd', 'error_description': 'odd thing'}), Permanent),
], ids=['quota', 'upload-limit', '401', 'permission', '429', '503', 'invalid', 'html', 'oauth-shape'])
def test_google_failures_mean_what_their_reasons_say(app, response, failure):
    with pytest.raises(failure) as raised:
        answer_of(response)
    if failure is QuotaWait:
        assert raised.value.until == next_reset()
    elif failure is Permanent and response.status_code == 400 and 'errors' in response.text:
        assert raised.value.message == 'YouTube: bad title'


def test_good_answers(app):
    assert answer_of(httpx.Response(204)) == {}
    assert answer_of(httpx.Response(200, json=['a list'])) == {}
    assert answer_of(httpx.Response(200, json={'id': 'v'})) == {'id': 'v'}


def test_the_reset_is_the_next_pacific_midnight(app):
    reset = next_reset()
    assert utcnow() < reset <= utcnow() + timedelta(hours=25)
    assert reset.tzinfo is None


def test_the_daily_allowance(app, yt, monkeypatch):
    monkeypatch.setitem(app.config, 'YOUTUBE_DAILY_UPLOADS', 2)
    take_upload()
    take_upload()
    with pytest.raises(QuotaWait) as raised:
        take_upload()
    assert raised.value.until == next_reset()


# --------------------------------------------------------------------------
# what a post becomes
# --------------------------------------------------------------------------

def test_the_snippet(app):
    post = a_post(title='A <b>clip</b>', body='Look at <this>', tags=[SimpleNamespace(name='cats'),
                                                                   SimpleNamespace(name=None)])
    snippet = snippet_of(post)
    assert snippet['title'] == 'A ‹b›clip‹/b›' and snippet['categoryId'] == youtube.CATEGORY
    assert snippet['description'].endswith('\n\nhttps://piefed.example/post/7') and '<' not in snippet['description']
    assert snippet['tags'] == ['cats']
    assert snippet_of(a_post(title='', body='First line\nmore'))['title'] == 'First line'
    assert snippet_of(a_post(title='', body=''))['title'] == 'Video'
    many = snippet_of(a_post(tags=[SimpleNamespace(name='t' * 99) for _ in range(10)]))
    assert len(many['tags']) == 5 and sum(len(tag) + 1 for tag in many['tags']) <= youtube.MAX_TAGS
    assert len(snippet_of(a_post(title='x' * 300))['title']) <= youtube.MAX_TITLE


def _session(http_mock, location=SESSION):
    headers = {'Location': location} if location else {}
    return http_mock.post(f'{UPLOAD}/videos').respond(200, headers=headers)


def test_an_upload_from_this_disk_with_its_thumbnail(app, yt, http_mock, monkeypatch, tmp_path):
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'\0' * 3000)
    poster = tmp_path / 'poster.jpg'
    poster.write_bytes(b'\xff\xd8poster')
    monkeypatch.setattr('app.connections.content.media_local_path', lambda post: str(video))
    session = _session(http_mock)
    put = http_mock.put(SESSION).respond(200, json={'id': 'vid-1'})
    thumbnail = http_mock.post(f'{UPLOAD}/thumbnails/set').respond(200, json={})
    post = a_post(image_id=5, image=SimpleNamespace(file_path=str(poster)))
    sent = yt.send(post, tokens(), None)
    assert (sent.remote_id, sent.remote_url) == ('vid-1', 'https://www.youtube.com/watch?v=vid-1')
    metadata = json.loads(session.calls.last.request.read())
    assert metadata['status'] == {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}
    assert metadata['snippet']['title'] == 'My clip'
    assert session.calls.last.request.headers['X-Upload-Content-Type'] == 'video/mp4'
    request = put.calls.last.request
    assert request.read() == b'\0' * 3000 and request.headers['Content-Length'] == '3000'
    assert request.headers['Authorization'] == 'Bearer at-1'
    assert 'videoId=vid-1' in str(thumbnail.calls.last.request.url)


def test_a_refused_thumbnail_does_not_fail_the_copy(app, yt, http_mock, monkeypatch, tmp_path):
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'v')
    poster = tmp_path / 'poster.jpg'
    poster.write_bytes(b'p')
    monkeypatch.setattr('app.connections.content.media_local_path', lambda post: str(video))
    _session(http_mock)
    http_mock.put(SESSION).respond(200, json={'id': 'vid-2'})
    http_mock.post(f'{UPLOAD}/thumbnails/set').mock(return_value=_error(403, 'forbidden'))
    post = a_post(image_id=5, image=SimpleNamespace(file_path=str(poster)))
    assert yt.send(post, tokens(), None).remote_id == 'vid-2'


def test_an_upload_from_storage(app, yt, http_mock, monkeypatch):
    monkeypatch.setattr('app.connections.content.media_local_path', lambda post: None)
    http_mock.get('https://piefed.example/static/media/v.mp4').respond(200, content=b'stored video')
    _session(http_mock)
    put = http_mock.put(SESSION).respond(200, json={'id': 'vid-3'})
    assert yt.send(a_post(), tokens(), None).remote_id == 'vid-3'
    assert put.calls.last.request.read() == b'stored video'


@pytest.mark.parametrize('case, failure', [
    ('answered-500', Transient), ('unreachable', Transient), ('too-large', Permanent), ('not-https', Permanent)])
def test_a_stored_video_that_cannot_be_read(app, yt, http_mock, monkeypatch, case, failure):
    monkeypatch.setattr('app.connections.content.media_local_path', lambda post: None)
    _session(http_mock)
    url = 'https://piefed.example/static/media/v.mp4'
    if case == 'answered-500':
        http_mock.get(url).respond(500)
    elif case == 'unreachable':
        http_mock.get(url).mock(side_effect=httpx.ConnectError('down'))
    elif case == 'too-large':
        http_mock.get(url).respond(200, content=b'x' * 100)
        monkeypatch.setattr(youtube, 'MAX_VIDEO_BYTES', 10)
    post = a_post(url='http://plain.example/v.mp4') if case == 'not-https' else a_post()
    with pytest.raises(failure):
        yt.send(post, tokens(), None)


@pytest.mark.parametrize('location', [None, 'https://evil.example/upload'], ids=['none', 'elsewhere'])
def test_an_upload_session_that_did_not_open(app, yt, http_mock, location):
    _session(http_mock, location)
    with pytest.raises(Transient):
        yt.send(a_post(), tokens(), None)


def test_an_upload_with_no_id_in_the_answer(app, yt, http_mock, monkeypatch, tmp_path):
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'v')
    monkeypatch.setattr('app.connections.content.media_local_path', lambda post: str(video))
    _session(http_mock)
    http_mock.put(SESSION).respond(200, json={})
    with pytest.raises(Transient):
        yt.send(a_post(), tokens(), None)


def test_an_upload_past_the_allowance_waits(app, yt, monkeypatch):
    monkeypatch.setitem(app.config, 'YOUTUBE_DAILY_UPLOADS', 0)
    with pytest.raises(QuotaWait):
        yt.send(a_post(), tokens(), None)


def test_an_edit_and_a_delete(app, yt, http_mock):
    update = http_mock.put(f'{API}/videos').respond(200, json={'id': 'vid-1'})
    delete = http_mock.delete(f'{API}/videos').respond(204)
    yt.edit(a_post(title='Renamed'), tokens(), 'vid-1')
    body = json.loads(update.calls.last.request.read())
    assert body['id'] == 'vid-1' and body['snippet']['title'] == 'Renamed' and body['snippet']['categoryId'] == '22'
    assert 'part=snippet' in str(update.calls.last.request.url)
    yt.delete(tokens(), 'vid-1')
    assert 'id=vid-1' in str(delete.calls.last.request.url)
