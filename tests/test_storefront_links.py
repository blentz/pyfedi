"""Stored links are saved canonical and shown on the visitor's storefront (onion storefront spec §3.5).

On save, `allowlist_html` (which `markdown_to_html` and federated HTML both pass through) ends with
`canonicalise_links`: a link to any registered host is stored on the canonical SERVER_URL. On render, a response on
a storefront other than the default has its registered-host links moved onto that storefront, once, by an
`after_request` hook, and RSS item bodies get the same rewrite. Attributes only, never text; foreign hosts never.
"""
import gzip
import re
from unittest.mock import patch
from urllib.parse import quote
import xml.etree.ElementTree as ET  # parses only this app's own feed

import pytest
from bs4 import BeautifulSoup
from flask import Response

from app import db
from app.models import Site, SiteDomain, Tag
import app.storefront as storefront
from app.storefront import (canonicalise_links, current_storefront, domain_for_host, rewrite_links,
                            rewrite_response_for_storefront, storefront_html)
from app.utils import allowlist_html, feed_entry_body, markdown_to_html
from tests.factories import make_domain
from tests.test_storefront_presentation import (CANONICAL, ONION, on_onion, onion, post_on_onion,  # noqa: F401
                                                world)  # noqa: F401  (onion and world are fixtures)

pytestmark = pytest.mark.usefixtures('site')

SCRIPTS = re.compile(r'<script\b[^>]*>(.*?)</script>', re.S)   # an inline script's text


def script_attrs(html):
    return [tag.attrs for tag in BeautifulSoup(html, 'html.parser').find_all('script')]


def in_onion_request(app):
    context = app.test_request_context(base_url=f'http://{ONION}')
    context.push()
    app.preprocess_request()
    return context


# --- canonical on save --------------------------------------------------------------------------------------------

@pytest.mark.parametrize('stored, saved', [
    ('<a href="http://abc.onion/post/1">x</a>', f'<a href="https://{CANONICAL}/post/1">x</a>'),
    ('<a href="//abc.onion/post/1">x</a>', f'<a href="https://{CANONICAL}/post/1">x</a>'),
    ('<a href="https://ABC.onion/post/1">x</a>', f'<a href="https://{CANONICAL}/post/1">x</a>'),
    (f'<a href="http://{CANONICAL}/post/1?a=1&amp;b=#c">x</a>',
     f'<a href="https://{CANONICAL}/post/1?a=1&amp;b=#c">x</a>'),
    ('<a href="https://abc.onion">x</a>', f'<a href="https://{CANONICAL}">x</a>'),
    ('<a href="https://abc.onion/">x</a>', f'<a href="https://{CANONICAL}/">x</a>'),
    ('<img src="http://abc.onion/a.jpg"/>', f'<img src="https://{CANONICAL}/a.jpg"/>'),
    ('<img srcset="http://abc.onion/a.jpg 1x, http://abc.onion/b.jpg 2x"/>',
     f'<img srcset="https://{CANONICAL}/a.jpg 1x, https://{CANONICAL}/b.jpg 2x"/>'),
    ('<img srcset="http://abc.onion/a,b.jpg, https://example.com/c.jpg 2x"/>',
     f'<img srcset="https://{CANONICAL}/a,b.jpg, https://example.com/c.jpg 2x"/>'),
])
def test_canonicalise_variants(app, onion, stored, saved):
    assert canonicalise_links(stored) == saved


@pytest.mark.parametrize('html', [
    '<a href="https://example.com/x">abc.onion</a>',          # a foreign host, and the host in visible text
    '<p>see http://abc.onion/post/1</p>',                     # text, never rewritten
    '<a href="https:\\\\abc.onion/x">x</a>',                    # a backslash URL names no host we can trust
    '<a href="ftp://abc.onion/x">x</a>',                      # not a web link
    '<a href="mailto:me@abc.onion">x</a>',
    '<a title="http://abc.onion/x" href="/post/1">x</a>',     # not a link attribute
    '<a href="https://notabc.onion/x">x</a>',
    '<a href="https://abc.o\tnion/x">abc.onion</a>',         # urlsplit drops the tab: the host is not as written
])
def test_canonicalise_leaves_the_rest_alone(app, onion, html):
    assert canonicalise_links(html) is html


def test_federated_html_is_canonicalised_and_nothing_else_changes(app, onion):
    incoming = ('<p>Hello <a href="https://mastodon.example/@a">@a</a>, see '
                '<a href="http://abc.onion/post/1">http://abc.onion/post/1</a> '
                '<img src="https://cdn.example/x.png" alt="x"/></p>')
    expected = allowlist_html(incoming.replace('href="http://abc.onion/post/1"',
                                               f'href="https://{CANONICAL}/post/1"'))
    assert allowlist_html(incoming) == expected
    assert 'http://abc.onion/post/1</a>' in expected   # the visible text is what the author wrote


def test_markdown_is_saved_canonical(app, onion):
    html = markdown_to_html('[a post](http://abc.onion/post/1) ![pic](//abc.onion/a.jpg)')
    assert f'href="https://{CANONICAL}/post/1"' in html
    assert f'src="https://{CANONICAL}/a.jpg"' in html
    assert 'abc.onion' not in html


def test_allowlist_test_env_reads_no_domains():
    # test_env runs without an app: like the fediverse domains, the registered domains are not read
    html = '<a href="http://abc.onion/post/1">x</a>'
    assert 'http://abc.onion/post/1' in allowlist_html(html, test_env={'fn_string': 'fn-test'})


# --- the storefront on render -------------------------------------------------------------------------------------

def test_rewrite_links_attributes(app, onion):
    body = (f'<html><head><meta property="og:url" content="https://{CANONICAL}/post/1"/>'
            f'<meta property="og:image" content="https://{CANONICAL}/static/a.jpg"/>'
            f'<meta name="description" content="all about {CANONICAL}"/>'
            f'<link rel="canonical" href="https://{CANONICAL}/post/1"/></head><body>'
            f'<a href="https://{CANONICAL}">home</a><a href="https://{CANONICAL}/">home/</a>'
            f'<a href="//{CANONICAL}/c/x?q=1#f">x</a><a href="https://example.com/{CANONICAL}">foreign</a>'
            f'<form action="https://{CANONICAL}/search"></form>'
            f'<img src="https://{CANONICAL}/i.png" srcset="https://{CANONICAL}/i.png 1x, https://{CANONICAL}/j.png 2x"/>'
            f'<div data-sse-url="https://{CANONICAL}/live/stream?feed=x" data-label="https://{CANONICAL} rocks"></div>'
            f'<a href="https:\\\\{CANONICAL}/x">bs</a>'
            f'<p>visit https://{CANONICAL}/post/1</p><span content="https://{CANONICAL}/x"></span>'
            f'</body></html>')
    out = rewrite_links(body, domain_for_host(ONION))
    assert f'<meta property="og:url" content="http://{ONION}/post/1"/>' in out
    assert f'content="http://{ONION}/static/a.jpg"' in out
    assert f'content="all about {CANONICAL}"' in out                  # meta text that is no URL
    assert f'<link rel="canonical" href="http://{ONION}/post/1"/>' in out
    assert f'<a href="http://{ONION}">home</a>' in out
    assert f'<a href="http://{ONION}/">home/</a>' in out
    assert f'<a href="http://{ONION}/c/x@{CANONICAL}?q=1#f">x</a>' in out
    assert f'<a href="https://example.com/{CANONICAL}">foreign</a>' in out
    assert f'action="http://{ONION}/search"' in out
    assert f'src="http://{ONION}/i.png"' in out
    assert f'srcset="http://{ONION}/i.png 1x, http://{ONION}/j.png 2x"' in out
    assert f'data-sse-url="http://{ONION}/live/stream?feed=x"' in out
    assert f'data-label="https://{CANONICAL} rocks"' in out           # data that is no URL
    assert f'href="https:\\\\{CANONICAL}/x"' in out
    assert f'<p>visit https://{CANONICAL}/post/1</p>' in out           # text, never rewritten
    assert f'<span content="https://{CANONICAL}/x"></span>' in out    # content is a link only on <meta>


def test_rewrite_links_returns_unchanged_body_when_nothing_moves(app, onion):
    body = f'<p>@someone@{CANONICAL.upper()} says hi</p><br>'
    assert rewrite_links(body, domain_for_host(ONION)) is body   # parsed, but no attribute moved: not re-serialised


def test_rewrite_skips_when_absent(app, onion):
    # off the onion: an onion page is always parsed, since any host may be remote media to proxy
    db.session.add(SiteDomain(domain='alias.test', scheme='https', federates=False))
    db.session.commit()
    storefront.clear_domain_cache()
    body = '<a href="https://alias.test/post/1">x</a><a href="https://example.com/">y</a>'
    with patch.object(storefront, 'BeautifulSoup') as parser:
        assert rewrite_links(body, domain_for_host('alias.test')) is body
        assert canonicalise_links('<a href="https://example.com/">y</a>') == '<a href="https://example.com/">y</a>'
    parser.assert_not_called()


def test_rewrite_precheck_ignores_case(app, onion):
    out = rewrite_links(f'<a href="https://{CANONICAL.upper()}/x">x</a>', domain_for_host(ONION))
    assert out == f'<a href="http://{ONION}/x">x</a>'


def test_onion_page_has_no_canonical_links(app, world):
    founder, author, community, post, image = world
    post.body_html = f'<p><a href="https://{CANONICAL}/post/1">https://{CANONICAL}/post/1</a></p>'
    db.session.commit()
    response = on_onion(app).get(f'/post/{post.id}', base_url=f'https://{ONION}')
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert f'<a href="http://{ONION}/post/1">https://{CANONICAL}/post/1</a>' in text
    assert f'href="https://{CANONICAL}' not in text
    assert int(response.headers['Content-Length']) == len(response.data)


def test_rewritten_page_keeps_doctype_and_scripts(app, world):
    """html.parser re-serialises the page: a DOCTYPE, inline scripts and the page itself must survive it."""
    founder, author, community, post, image = world
    post.body_html = f'<p><a href="https://{CANONICAL}/c/general">there</a></p>'
    db.session.commit()
    seen = []

    def spy(body, where):
        seen.append(body)
        return rewrite_links(body, where)

    with patch.object(storefront, 'rewrite_links', side_effect=spy):
        response = on_onion(app).get(f'/post/{post.id}', base_url=f'https://{ONION}')
    assert response.status_code == 200
    original, text = seen[0], response.get_data(as_text=True)
    assert text != original
    assert text.lstrip().lower().startswith('<!doctype html>')
    assert original.lstrip().lower().startswith('<!doctype html>')
    # script text is byte-identical; a script tag's attributes are the same, though html.parser re-quotes them
    assert SCRIPTS.findall(text) and SCRIPTS.findall(text) == SCRIPTS.findall(original)
    assert script_attrs(text) == script_attrs(original)
    assert f'href="http://{ONION}/c/general@{CANONICAL}"' in text


def test_inline_script_text_is_byte_identical():
    page = ('<!DOCTYPE html>\n<html><head><script nonce="n">if (a < b && c > d) { s = "</div>&amp;"; }</script>'
            '</head><body><a href="https://test.piefed.local/x">x</a></body></html>')
    out = storefront._rebase_links(page, 'http://abc.onion', frozenset({CANONICAL}), render=True)
    assert SCRIPTS.findall(out) == SCRIPTS.findall(page)
    assert out.startswith('<!DOCTYPE html>')
    assert '<a href="http://abc.onion/x">x</a>' in out


@pytest.mark.parametrize('location, expected', [
    (f'https://{CANONICAL}/c/general?x=1&y=#top', f'http://{ONION}/c/general@{CANONICAL}?x=1&y=#top'),
    (f'https://{CANONICAL}', f'http://{ONION}'),
    (f'https://{CANONICAL}/?', f'http://{ONION}/?'),
    ('/c/general?x=1', '/c/general?x=1'),                          # relative: already on the storefront
    ('https://example.com/c/general', 'https://example.com/c/general'),
    (f'https:\\\\{CANONICAL}/c/general', f'https:\\\\{CANONICAL}/c/general'),
    (f'http://{ONION}/c/general', f'http://{ONION}/c/general'),
])
def test_location_header_rewrite(app, onion, location, expected):
    context = in_onion_request(app)
    try:
        response = Response('', status=302, headers={'Location': location, 'X-Kept': 'yes'})
        response = rewrite_response_for_storefront(response)
    finally:
        context.pop()
    assert response.headers['Location'] == expected
    assert response.status_code == 302 and response.headers['X-Kept'] == 'yes'


def test_hx_redirect_header_rewrite(app, onion):
    context = in_onion_request(app)
    try:
        response = rewrite_response_for_storefront(Response('', headers={'HX-Redirect': f'https://{CANONICAL}/x'}))
    finally:
        context.pop()
    assert response.headers['HX-Redirect'] == f'http://{ONION}/x'


def test_location_rewritten(app, world):
    founder, author, community, post, image = world
    target = quote(f'https://{CANONICAL}/c/general?x=1#top', safe='')
    # the view redirects to its ?redirect= target as given: an absolute URL on the canonical host
    response = post_on_onion(app, founder, f'/u/author@{CANONICAL}/block_instance?redirect={target}')
    assert response.status_code == 302
    assert response.headers['Location'] == f'http://{ONION}/c/general@{CANONICAL}?x=1#top'


def test_rewrite_keeps_status_and_headers(app, onion):
    context = in_onion_request(app)
    try:
        response = Response(f'<a href="https://{CANONICAL}/x">x</a>', status=404, mimetype='text/html',
                            headers={'X-Kept': 'yes'})
        response = rewrite_response_for_storefront(response)
    finally:
        context.pop()
    assert response.status_code == 404 and response.headers['X-Kept'] == 'yes'
    assert response.get_data(as_text=True) == f'<a href="http://{ONION}/x">x</a>'
    assert int(response.headers['Content-Length']) == len(response.get_data())


def test_rewrite_ignores_other_content_types(app, onion):
    context = in_onion_request(app)
    try:
        body = f'{{"url": "https://{CANONICAL}/x"}}'
        response = rewrite_response_for_storefront(Response(body, mimetype='application/json'))
    finally:
        context.pop()
    assert response.get_data(as_text=True) == body


def test_rewrite_never_reads_an_encoded_body(app, onion):
    """An encoded body (gzip from an upstream or a view) is bytes, not markup: it is passed on as it is, never
    decoded or rewritten. Its redirect header is still moved."""
    body = gzip.compress(f'<a href="https://{CANONICAL}/x">x</a>'.encode())
    context = in_onion_request(app)
    try:
        response = rewrite_response_for_storefront(Response(body, mimetype='text/html', headers={
            'Content-Encoding': 'gzip', 'Location': f'https://{CANONICAL}/y'}))
    finally:
        context.pop()
    assert response.get_data() == body
    assert response.headers['Location'] == f'http://{ONION}/y'


def test_clearnet_response_untouched(app, world):
    founder, author, community, post, image = world
    post.body_html = f'<p><a href="http://{ONION}/post/1">x</a></p>'   # an old row: rows are never edited
    db.session.commit()
    with patch.object(storefront, 'rewrite_links') as rewrite:
        response = on_onion(app).get(f'/post/{post.id}', headers={'Host': CANONICAL})
    assert response.status_code == 200
    rewrite.assert_not_called()
    assert f'href="http://{ONION}/post/1"' in response.get_data(as_text=True)
    assert db.session.get(type(post), post.id).body_html == f'<p><a href="http://{ONION}/post/1">x</a></p>'
    with app.test_request_context(base_url=f'https://{CANONICAL}'):
        app.preprocess_request()
        redirect = Response('', status=302, headers={'Location': f'http://{ONION}/x'})
        assert rewrite_response_for_storefront(redirect).headers['Location'] == f'http://{ONION}/x'


def test_sse_not_buffered(app, onion):
    pulled = []

    def stream():
        pulled.append(1)
        yield f'<a href="https://{CANONICAL}/x">x</a>'

    context = in_onion_request(app)
    try:
        streamed = Response(stream(), mimetype='text/html')
        assert rewrite_response_for_storefront(streamed) is streamed
        passthrough = Response(f'<a href="https://{CANONICAL}/x">x</a>', mimetype='text/html')
        passthrough.direct_passthrough = True
        assert rewrite_response_for_storefront(passthrough).response == [
            f'<a href="https://{CANONICAL}/x">x</a>'.encode()]
    finally:
        context.pop()
    assert pulled == []


# --- RSS item bodies ----------------------------------------------------------------------------------------------

class _Post:
    content_warning = None
    body_html = f'<p><a href="https://{CANONICAL}/c/general">there</a></p>'


def test_feed_entry_body_on_storefront(app, onion):
    context = in_onion_request(app)
    try:
        assert current_storefront().domain == ONION
        assert feed_entry_body(_Post()) == f'<p><a href="http://{ONION}/c/general@{CANONICAL}">there</a></p>'
    finally:
        context.pop()
    with app.test_request_context(base_url=f'https://{CANONICAL}'):
        app.preprocess_request()
        assert feed_entry_body(_Post()) == _Post.body_html


def test_storefront_html_passes_none(app, onion):
    context = in_onion_request(app)
    try:
        assert storefront_html(None) is None
    finally:
        context.pop()


# A bare name on the onion is the onion's own (plan C1): hell's rows are named by their handle there
@pytest.mark.parametrize('path', ['/index/feed', '/d/{domain}/feed', '/tag/pics/feed', f'/u/author@{CANONICAL}/feed',
                                  f'/community/general@{CANONICAL}/feed'])
def test_feed_item_bodies_on_storefront(app, world, path):
    founder, author, community, post, image = world
    domain = make_domain('pics.example')
    tag = Tag(name='pics', display_as='Pics')
    db.session.add(tag)
    if path == '/index/feed':   # Local lists the visited domain's own communities, so the onion's holds this one
        community.site_domain_id = SiteDomain.query.filter_by(domain=ONION).one().id
    post.domain_id = domain.id
    post.tags.append(tag)
    post.body_html = f'<p><a href="https://{CANONICAL}/c/general">there</a></p>'
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    response = on_onion(app).get(path.format(domain=domain.id), headers={'Host': ONION})
    assert response.status_code == 200
    item = ET.fromstring(response.data).find('channel').find('item')
    assert f'href="http://{ONION}/c/general@{CANONICAL}"' in item.findtext('description')
    assert item.findtext('guid') == post.ap_id   # ids stay canonical
