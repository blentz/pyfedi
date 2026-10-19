"""Remote media on an onion page loads through the onion (onion media proxy spec 2026-10-19, section 3.1).

The onion response rewrite points every remote media URL (`src`, `srcset`, `poster`, `data-*`, `style` url(...) on a
media element, and a link to a media file) at the signed proxy, so the visitor's Tor browser asks only the onion.
A remote embed (`iframe`, `embed`, `object`) becomes the placeholder and a plain link. Clearnet is unchanged.
"""
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from flask import Response, current_app

import app.storefront as storefront
from app.onion_media import proxy_url
from app.storefront import rewrite_response_for_storefront
from tests.test_storefront_links import in_onion_request
from tests.test_storefront_presentation import CANONICAL, ONION, onion  # noqa: F401  (onion is a fixture)

pytestmark = pytest.mark.usefixtures('site')

S3 = 'cdn.example.test'
HIDDEN = '<span class="onion-media-hidden">Media hidden on the onion site</span>'
IMG = 'https://lemmy.world/pictrs/image/a.webp'
EMBED = 'https://www.youtube-nocookie.com/embed/abc'


@pytest.fixture
def s3(app, monkeypatch):
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', S3)


def on_onion(body):
    context = in_onion_request(current_app._get_current_object())
    try:
        return rewrite_response_for_storefront(Response(body, mimetype='text/html')).get_data(as_text=True)
    finally:
        context.pop()


def test_remote_img_is_proxied(app, onion):
    out = on_onion(f'<img class="a b" src="{IMG}" alt="a"/>')   # class: a list in bs4, never a URL
    assert out == f'<img class="a b" src="{proxy_url(IMG)}" alt="a"/>'


def test_srcset_candidates_are_proxied_with_descriptors(app, onion):
    other = 'https://lemmy.world/pictrs/image/b.webp'
    out = on_onion(f'<img srcset="{IMG} 1x,{other}  2x, /local.jpg 3x"/>')
    assert out == f'<img srcset="{proxy_url(IMG)} 1x,{proxy_url(other)}  2x, /local.jpg 3x"/>'


@pytest.mark.parametrize('href', [IMG, 'https://lemmy.world/x/a.JPG', 'https://lemmy.world/pictrs/image/abc?f=webp',
                                  'https://files.test/v.mp4?x=1', 'https://files.test/s.FLAC'])
def test_media_link_is_proxied_page_link_is_not(app, onion, href):
    out = on_onion(f'<a href="{href}" rel="nofollow">x</a><a href="https://lemmy.world/post/1">p</a>')
    assert out == f'<a href="{proxy_url(href)}" rel="nofollow">x</a><a href="https://lemmy.world/post/1">p</a>'


@pytest.mark.parametrize('body', [f'<iframe src="{EMBED}" allowfullscreen></iframe>', f'<embed src="{EMBED}"/>',
                                  f'<object data="{EMBED}"></object>'])
def test_remote_iframe_becomes_placeholder_and_link(app, onion, body):
    soup = BeautifulSoup(on_onion(f'<div>{body}</div>'), 'html.parser')
    assert not soup.find_all(['iframe', 'embed', 'object'])
    assert soup.select('span.onion-media-hidden')
    link = soup.find('a')
    assert link['href'] == EMBED and link.string == EMBED
    assert link['rel'] == ['nofollow', 'noreferrer']


def test_style_background_url_is_proxied(app, onion):
    out = on_onion(f'<img src="/x.jpg" style="background-image: url(\'{IMG}\')"/>'
                   f'<video poster="{IMG}" data-src="{IMG}"></video>')
    assert out == (f'<img src="/x.jpg" style="background-image: url(\'{proxy_url(IMG)}\')"/>'
                   f'<video poster="{proxy_url(IMG)}" data-src="{proxy_url(IMG)}"></video>')


def test_a_style_url_that_cannot_be_rewritten_in_place_hides_the_element(app, onion):
    # escaped in the raw value, so no literal copy to replace: the element goes rather than load the remote host
    assert on_onion('<img style="background:url(https://lemmy\\2e world/x.jpg)"/>') == HIDDEN


def test_local_and_s3_urls_untouched_by_the_proxy(app, onion, s3):
    out = on_onion(f'<img src="/static/x.jpg"/><img src="https://{CANONICAL}/x.jpg"/>'
                   f'<img src="http://{ONION}/y.jpg"/><img src="https://{S3}/z.jpg"/>'
                   f'<a href="https://{S3}/z.jpg">s3</a><iframe src="https://{S3}/e.html"></iframe>')
    assert out == (f'<img src="/static/x.jpg"/><img src="http://{ONION}/x.jpg"/>'
                   f'<img src="http://{ONION}/y.jpg"/>{HIDDEN}s3{HIDDEN}')


def test_scheme_relative_and_odd_spellings_of_a_remote_host(app, onion):
    assert on_onion('<img src="//lemmy.world/a.jpg"/>') == f'<img src="{proxy_url("https://lemmy.world/a.jpg")}"/>'
    assert on_onion('<img src="https:\\\\lemmy.world/a.jpg"/>') == HIDDEN   # not fetchable as written: hidden


@pytest.mark.parametrize('body, shown', [
    ('<img src="https:lemmy.world/a.jpg"/>', HIDDEN),                   # no // after the scheme
    ('<img src="///lemmy.world/a.jpg"/>', HIDDEN),                       # no authority as urlsplit reads it
    ('<img srcset="/a.jpg 1x, https:lemmy.world/b.jpg 2x"/>', HIDDEN),
    ('<img style="background:url(https:lemmy.world/x.jpg)"/>', HIDDEN),
    ('<a href="https:lemmy.world/a.jpg">the picture</a>', 'the picture'),   # a link keeps its text
])
def test_a_remote_url_the_proxy_cannot_fetch_as_written_is_hidden(app, onion, body, shown):
    assert on_onion(body) == shown


def test_a_local_embed_is_left_alone(app, onion):
    body = '<iframe src="/embed/1"></iframe>'
    assert on_onion(body) == body


def test_an_embed_remote_only_in_its_style_becomes_the_placeholder_without_a_link(app, onion):
    assert on_onion('<iframe src="/e" style="background:url(https://lemmy.world/x.jpg)"></iframe>') == HIDDEN


def test_onion_page_has_no_remote_media_host(app, onion, s3):
    body = (f'<img src="{IMG}" srcset="{IMG} 1x, https://b.test/b.png 2x" data-src="https://c.test/c.gif"/>'
            f'<video poster="https://d.test/p.jpg"><source src="https://e.test/v.mp4"/>'
            f'<track src="https://f.test/t.vtt"/></video><audio src="https://g.test/a.mp3"></audio>'
            f'<picture><source srcset="https://h.test/a.webp 1x"/><img src="https://i.test/i.avif"/></picture>'
            f'<a href="https://j.test/pictrs/image/x"><img src="https://k.test/k.jpg"/></a>'
            f'<div style="x"><img style="background:url(https://l.test/l.jpg)"/></div>'
            f'<iframe src="{EMBED}"></iframe><img src="https://{S3}/s.jpg"/>')
    soup = BeautifulSoup(on_onion(body), 'html.parser')
    for tag in soup.find_all(True):
        for name, value in tag.attrs.items():
            if name in ('src', 'srcset', 'poster', 'style', 'data') or name.startswith('data-') or \
                    (name == 'href' and tag.name != 'a'):
                assert '.test' not in value and 'lemmy.world' not in value, (tag.name, name, value)
    assert not soup.find_all(['iframe', 'embed', 'object'])
    assert [a['href'] for a in soup.find_all('a')] == [proxy_url('https://j.test/pictrs/image/x'), EMBED]


def test_clearnet_page_is_byte_identical(app, onion):
    body = f'<img src="{IMG}"/><a href="{IMG}">x</a><iframe src="{EMBED}"></iframe>'
    with app.test_request_context(base_url=f'https://{CANONICAL}'):
        app.preprocess_request()
        assert rewrite_response_for_storefront(Response(body, mimetype='text/html')).get_data(as_text=True) == body


def test_a_page_without_any_known_host_is_still_parsed_on_the_onion(app, onion, monkeypatch):
    monkeypatch.setitem(current_app.config, 'S3_PUBLIC_URL', '')
    with patch.object(storefront, 'BeautifulSoup', wraps=storefront.BeautifulSoup) as parser:
        assert on_onion(f'<img src="{IMG}"/>') == f'<img src="{proxy_url(IMG)}"/>'
    assert parser.call_count == 1


def test_a_failure_hides_the_element(app, onion):
    with patch.object(storefront, 'proxy_url', side_effect=RuntimeError('boom')), \
            patch.object(current_app.logger, 'exception') as logged:
        assert on_onion(f'<p>a</p><img src="{IMG}"/>') == f'<p>a</p>{HIDDEN}'
    logged.assert_called_once()
