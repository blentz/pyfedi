"""Media on the S3 public host is not loaded on the onion (owner ruling 2026-10-18, "Hide S3 media").

With `S3_PUBLIC_URL` set, uploaded media is served from that clearnet host, which the storefront rewrite never moves.
On an onion storefront the response rewrite replaces whatever points at that host with a placeholder, so the onion
never makes a request to it. Clearnet is unchanged, and with S3 unset nothing is parsed or changed.
"""
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from flask import Response, current_app

import app.storefront as storefront
from app import db
from app.models import SiteDomain
from app.storefront import clear_domain_cache, rewrite_response_for_storefront, storefront_html
from app.utils import feed_entry_body
from tests.test_storefront_links import in_onion_request
from tests.test_storefront_presentation import CANONICAL, ONION, on_onion, onion, world  # noqa: F401  (fixtures)

pytestmark = pytest.mark.usefixtures('site')

S3 = 'cdn.example.test'
HIDDEN = '<span class="onion-media-hidden">Media hidden on the onion site</span>'


@pytest.fixture
def s3(app, monkeypatch):
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', S3)


def hide(body, mimetype='text/html'):
    context = in_onion_request(current_app._get_current_object())
    try:
        return rewrite_response_for_storefront(Response(body, mimetype=mimetype)).get_data(as_text=True)
    finally:
        context.pop()


@pytest.mark.parametrize('body, shown', [
    (f'<p>a</p><img src="https://{S3}/posts/ab/cd/x.jpg" alt="x"/><p>b</p>', f'<p>a</p>{HIDDEN}<p>b</p>'),
    (f'<img srcset="https://elsewhere.test/a.jpg 1x, https://{S3}/b.jpg 2x"/>', HIDDEN),
    (f'<img src="//{S3.upper()}:443/x.jpg"/>', HIDDEN),
    (f'<img data-src="https://{S3}/lazy.jpg"/>', HIDDEN),
    (f'<video controls poster="https://{S3}/p.jpg"><source src="https://elsewhere.test/v.mp4"/></video>', HIDDEN),
    (f'<video controls><source src="https://{S3}/v.mp4" type="video/mp4"/><track src="/t.vtt"/></video>', HIDDEN),
    (f'<audio controls><source src="https://elsewhere.test/a.mp3"/><track src="https://{S3}/t.vtt"/></audio>',
     HIDDEN),
    (f'<picture><source srcset="https://{S3}/a.webp"/><img src="/fallback.jpg"/></picture>', HIDDEN),
    (f'<a href="https://{S3}/files/doc.pdf">the file</a>', 'the file'),
    (f'<a href="https://{S3}/x.jpg"><img src="https://{S3}/x.jpg"/></a>', HIDDEN),
    (f'<area href="https://{S3}/x" alt="a"/>', '<area alt="a"/>'),
    (f'<link rel="preload" as="image" href="https://{S3}/x.jpg"/><meta property="og:image" content="https://{S3}/x.jpg"/>'
     f'<script src="https://{S3}/x.js"></script>', ''),
    (f'<div class="c" data-media="https://{S3}/x.jpg" data-keep="https://elsewhere.test/x">t</div>',
     '<div class="c" data-keep="https://elsewhere.test/x">t</div>'),
    (f'<div style="background: url(\'https://{S3}/x.jpg\') no-repeat">t</div>', '<div>t</div>'),
    (f'<img src="https:\\\\{S3}/x.jpg"/>', HIDDEN),   # a browser reads the backslashes as slashes
    (f'<link rel="preconnect" href="{S3}"/><link rel="preconnect" href="{S3}/prefix"/>', ''),
    (f'<iframe src="https://{S3}/x.html"></iframe>', HIDDEN),
])
def test_s3_media_is_hidden_on_the_onion(app, onion, s3, body, shown):
    out = hide(body)
    assert out == shown
    assert S3 not in out.lower()


@pytest.mark.parametrize('body', [
    f'<img src="https://{S3}.evil.test/x.jpg"/>',              # a host that merely starts with ours
    f'<img src="https://evil.test/{S3}/x.jpg"/>',              # ours in a path
    f'<img src="https://evil.test/?u=https://{S3}/x.jpg"/>',   # ours in a query
    f'<img src="https://user@evil.test/{S3}"/>',
    f'<p>{S3} and https://{S3}/x.jpg as text</p>',
    f'<div style="color: red; content: \'{S3}\'">t</div>',
    f'<img src="/static/{S3}/x.jpg" class="a b"/>',
    '<img src="/static/x.jpg" class="a b"/>',
])
def test_what_is_not_on_the_s3_host_is_left_alone(app, onion, s3, body):
    assert hide(body) == body


def test_a_page_with_no_s3_mention_is_not_parsed(app, onion, s3):
    with patch.object(storefront, 'BeautifulSoup') as parser:
        assert hide('<img src="/x.jpg"/>') == '<img src="/x.jpg"/>'
    parser.assert_not_called()


@pytest.mark.parametrize('public, hidden', [
    ('', False), (f'https://{S3}/', True), (f'{S3}/media', True), (f'{S3}:9000', True), (f'https://user@{S3}/x', True),
    ('https://[::1', False),   # not a host: nothing is hidden, and nothing raises
])
def test_the_s3_host_is_read_from_the_public_url_as_a_host(app, onion, monkeypatch, public, hidden):
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', public)
    out = hide(f'<img src="https://{S3}/x.jpg"/>')
    assert (HIDDEN in out) is hidden


def test_nothing_is_parsed_when_s3_is_not_configured(app, onion, monkeypatch):
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', '')
    body = f'<img src="https://{S3}/x.jpg"/><a href="https://{CANONICAL}/x">x</a>'
    context = in_onion_request(current_app._get_current_object())
    try:
        with patch.object(storefront, 'BeautifulSoup', wraps=storefront.BeautifulSoup) as parser:
            out = rewrite_response_for_storefront(Response(body, mimetype='text/html')).get_data(as_text=True)
    finally:
        context.pop()
    assert out == f'<img src="https://{S3}/x.jpg"/><a href="http://{ONION}/x">x</a>'
    assert parser.call_count == 1   # the link rewrite's own parse, none for S3


def test_s3_media_is_hidden_before_the_links_move(app, onion, s3):
    out = hide(f'<img src="https://{S3}/x.jpg"/><a href="https://{CANONICAL}/x">x</a>')
    assert out == f'{HIDDEN}<a href="http://{ONION}/x">x</a>'


def test_the_placeholder_is_translated(app, onion, s3):
    with patch.object(storefront, '_', side_effect=lambda text: f'[{text}]'):
        assert '<span class="onion-media-hidden">[Media hidden on the onion site]</span>' in hide(
            f'<img src="https://{S3}/x.jpg"/>')


def test_json_and_other_types_are_left_alone(app, onion, s3):
    body = f'{{"image": "https://{S3}/x.jpg"}}'
    assert hide(body, mimetype='application/json') == body


def test_clearnet_keeps_s3_media(app, onion, s3):
    body = f'<img src="https://{S3}/x.jpg"/>'
    with app.test_request_context(base_url=f'https://{CANONICAL}'):
        app.preprocess_request()
        assert rewrite_response_for_storefront(Response(body, mimetype='text/html')).get_data(as_text=True) == body


def test_a_clearnet_alias_keeps_s3_media(app, onion, s3):
    # only an onion storefront hides it: another domain of the site is as public as the canonical one
    db.session.add(SiteDomain(domain='alias.test', scheme='https', federates=False))
    db.session.commit()
    clear_domain_cache()
    body = f'<img src="https://{S3}/x.jpg"/>'
    with app.test_request_context(base_url='https://alias.test'):
        app.preprocess_request()
        assert rewrite_response_for_storefront(Response(body, mimetype='text/html')).get_data(as_text=True) == body


def test_s3_host_that_is_one_of_ours_is_not_hidden(app, onion, monkeypatch):
    # the host is served by this site, so the rewrite moves it onto the onion: nothing leaves it
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', CANONICAL)
    assert hide(f'<img src="https://{CANONICAL}/x.jpg"/>') == f'<img src="http://{ONION}/x.jpg"/>'


def test_a_feed_entry_on_the_onion_hides_s3_media(app, onion, s3):
    class Post:
        content_warning = None
        body_html = f'<p>pic</p><img src="https://{S3}/x.jpg"/>'

    context = in_onion_request(current_app._get_current_object())
    try:
        assert feed_entry_body(Post()) == f'<p>pic</p>{HIDDEN}'
        assert storefront_html(None) is None
    finally:
        context.pop()
    with app.test_request_context(base_url=f'https://{CANONICAL}'):
        app.preprocess_request()
        assert feed_entry_body(Post()) == Post.body_html


def test_an_onion_page_with_an_s3_image_shows_the_placeholder_and_no_s3_host(app, world, s3):
    founder, author, community, post, image = world
    post.body_html = f'<p>look</p><img src="https://{S3}/posts/ab/cd/x.jpg"/><a href="https://{S3}/y.mp4">v</a>'
    db.session.commit()
    onion_page = on_onion(app).get(f'/post/{post.id}', base_url=f'https://{ONION}').get_data(as_text=True)
    assert 'Media hidden on the onion site' in onion_page
    assert S3 not in onion_page
    clearnet_page = on_onion(app).get(f'/post/{post.id}', headers={'Host': CANONICAL}).get_data(as_text=True)
    assert f'<img src="https://{S3}/posts/ab/cd/x.jpg"/>' in clearnet_page


def test_an_onion_page_is_unchanged_when_s3_is_not_configured(app, world, monkeypatch):
    founder, author, community, post, image = world
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', '')
    post.body_html = f'<p>look</p><img src="https://{S3}/posts/ab/cd/x.jpg"/>'
    db.session.commit()
    page = on_onion(app).get(f'/post/{post.id}', base_url=f'https://{ONION}').get_data(as_text=True)
    assert f'<img src="https://{S3}/posts/ab/cd/x.jpg"/>' in page
    assert 'onion-media-hidden' not in page
