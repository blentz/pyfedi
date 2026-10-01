"""The NNTP server's image attachment is fetched through get_request (R162 residue).

`PieFedNNTPServer._build_image_body` fetched the image with urllib, so the SSRF
guard (`is_invalid_get_request_uri`) and the pinned httpx client (app/pinned_http.py)
never saw it: a post whose image url named a private address made the NNTP server
fetch it.
"""
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.nntp.server import PieFedNNTPServer


def a_post(image_url):
    image = SimpleNamespace(source_url=image_url, medium_url=lambda: image_url)
    return SimpleNamespace(id=7, url=None, type=3, image=image, body='words')


def build(post):
    return PieFedNNTPServer._build_image_body(None, post)


def test_the_image_is_attached(app, http_mock):
    http_mock.get('https://cdn.example/a.png').mock(
        return_value=httpx.Response(200, content=b'PNGDATA', headers={'Content-Type': 'image/png'}))
    with app.app_context():
        body, headers = build(a_post('https://cdn.example/a.png'))
    assert 'Content-Type: image/png' in body
    assert 'filename="image.png"' in body
    assert 'UE5HREFUQQ==' in body          # base64 of PNGDATA
    assert headers['MIME-Version'] == '1.0'


def test_a_private_address_is_not_fetched(app, monkeypatch):
    """The guard refuses it, so there is no request and no attachment."""
    monkeypatch.setattr(app, 'debug', False)
    with app.app_context(), \
            patch('urllib.request.urlopen') as urlopen:
        body, _ = build(a_post('http://127.0.0.1/a.png'))
    assert not urlopen.called
    assert 'Content-Disposition' not in body
    assert 'words' in body


def test_a_response_that_is_not_ok_attaches_nothing(app, http_mock):
    http_mock.get('https://cdn.example/a.png').mock(return_value=httpx.Response(404))
    with app.app_context():
        body, _ = build(a_post('https://cdn.example/a.png'))
    assert 'Content-Disposition' not in body
