"""Off-site references: the scanner, the onion's render guard and its Content-Security-Policy (project 4, Task 4).

On the onion, branding never makes a visitor's browser fetch anything from outside the onion (spec §3.4).
`offsite_references` finds every reference a browser would fetch from another host; `onion_safe` drops each one from
the onion's effective branding as a page renders it (`current_branding`); the onion's CSP is the browser's backstop.
The onion media proxy is served on the onion, so a proxied remote image is on-site."""
import logging
import re

import pytest
from bs4 import BeautifulSoup
from flask import g

from app import cache, db
from app.branding import Branding, current_branding, offsite_references, onion_safe
from app.constants import POST_TYPE_IMAGE
from app.models import DomainBranding, File, Site
from app.onion_media import cache_name, proxy_url
from app.storefront import clear_domain_cache
from tests.conftest import forget_login
from tests.factories import make_instance, make_post, make_user
from tests.two_domains import HELL, ONION, default_request, onion_request, two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('site')

PROXY = '10.89.0.2'
ONION_HEADERS = {'X-PieFed-Via': 'onion', 'X-Forwarded-For': '10.89.0.7'}
ELSEWHERE = 'https://elsewhere.example.org'
REMOTE_HOST = 'remote.example.org'
REMOTE_IMAGE = f'https://{REMOTE_HOST}/pictrs/image/cat.webp'
REMOTE_AVATAR = f'https://{REMOTE_HOST}/pictrs/image/face.png'
SANDBOX = "default-src 'none'; sandbox"
SCRIPT_POLICY = "script-src 'self' 'nonce-{nonce}' 'strict-dynamic'; object-src 'none'; base-uri 'none';"
ONION_POLICY = SCRIPT_POLICY + " img-src 'self' data: blob:; font-src 'self' data:; style-src 'self' 'unsafe-inline';" \
                               " connect-src 'self'; media-src 'self';"


# ---------------------------------------------------------------------------------------------------------------------
# The scanner

FOUND = [
    # CSS url(), quoted, unquoted and //-relative, and @import
    ({'css': f'a {{ background: url({ELSEWHERE}/a.png) }}'}, [f'{ELSEWHERE}/a.png']),
    ({'css': f'a {{ background: url("{ELSEWHERE}/b.png") }}'}, [f'{ELSEWHERE}/b.png']),
    ({'css': f"a {{ background: url('{ELSEWHERE}/c.png') }}"}, [f'{ELSEWHERE}/c.png']),
    ({'css': 'a { background: URL( //elsewhere.example.org/d.png ) }'}, ['//elsewhere.example.org/d.png']),
    ({'css': f'@import "{ELSEWHERE}/e.css";'}, [f'{ELSEWHERE}/e.css']),
    ({'css': f"@import url({ELSEWHERE}/f.css) screen;"}, [f'{ELSEWHERE}/f.css']),
    ({'css': f'@font-face {{ src: \\75 rl({ELSEWHERE}/g.woff2) }}'}, [f'{ELSEWHERE}/g.woff2']),   # an escaped url(
    ({'css': f'a {{ background: image-set("{ELSEWHERE}/h.png" 1x) }}'}, [f'{ELSEWHERE}/h.png']),
    ({'css': 'a { background: url(https://ELSEWHERE.example.org./i.png) }'},
     ['https://ELSEWHERE.example.org./i.png']),
    # HTML auto-loading attributes and inline style
    ({'html': f'<p><img src="{ELSEWHERE}/j.png" alt="j"></p>'}, [f'{ELSEWHERE}/j.png']),
    ({'html': f'<img srcset="/static/k.png 1x, {ELSEWHERE}/k2.png 2x">'}, [f'{ELSEWHERE}/k2.png']),
    ({'html': f'<video poster="{ELSEWHERE}/l.jpg"><source src="{ELSEWHERE}/l.mp4"></video>'},
     [f'{ELSEWHERE}/l.jpg', f'{ELSEWHERE}/l.mp4']),
    ({'html': f'<link rel="stylesheet" href="{ELSEWHERE}/m.css">'}, [f'{ELSEWHERE}/m.css']),
    ({'html': '<link rel="icon" href="//elsewhere.example.org/n.ico">'}, ['//elsewhere.example.org/n.ico']),
    ({'html': f'<link rel="Shortcut Icon" href="{ELSEWHERE}/n2.ico"><link rel="preconnect" href="{ELSEWHERE}/">'},
     [f'{ELSEWHERE}/n2.ico', f'{ELSEWHERE}/']),
    ({'html': f'<div style="background-image: url({ELSEWHERE}/o.png)">o</div>'}, [f'{ELSEWHERE}/o.png']),
    ({'html': f'<style>@import "{ELSEWHERE}/p.css";</style>'}, [f'{ELSEWHERE}/p.css']),
    ({'html': '<img src="https:\\\\elsewhere.example.org/q.png">'}, ['https:\\\\elsewhere.example.org/q.png']),
    # each of `urls`
    ({'urls': ['/static/media/logo.png', f'{ELSEWHERE}/r.png', '']}, [f'{ELSEWHERE}/r.png']),
    # one reference found twice is named once, in order of first appearance
    ({'css': f'a {{ background: url({ELSEWHERE}/s.png) }}', 'urls': [f'{ELSEWHERE}/s.png', f'{ELSEWHERE}/t.png']},
     [f'{ELSEWHERE}/s.png', f'{ELSEWHERE}/t.png']),
]

NOT_FOUND = [
    {'css': 'a { background: url(/static/a.png) } b { background: url(b.png) }'},
    {'css': 'a { background: url(data:image/png;base64,AAAA) }'},
    {'css': f'a {{ background: url(http://{ONION}/c.png) }} b {{ background: url(//ABC.ONION/d.png) }}'},
    {'css': f'a::after {{ content: "see {ELSEWHERE}/e" }} /* url({ELSEWHERE}/f.png) */'},
    {'html': f'<p><a href="{ELSEWHERE}/g">a link is not a fetch</a></p>'},
    {'html': f'<link rel="canonical" href="{ELSEWHERE}/g"><link rel="alternate" href="{ELSEWHERE}/g"><link href="{ELSEWHERE}/g">'},
    {'html': f'<img src="/static/h.png" srcset="i.png 1x, http://{ONION}/j.png 2x"><img alt="no src">'},
    {'html': '<div style="color: red">k</div><p>text only</p><style>.a { background: url(/static/a.png) }</style>'},
    {'urls': ['/static/media/logo.png', f'http://{ONION}/static/l.png', '']},
    {},
]


@pytest.mark.parametrize('kwargs, expected', FOUND)
def test_scanner_finds_each_form(app, kwargs, expected):
    assert offsite_references(own_host=ONION, **kwargs) == expected


@pytest.mark.parametrize('kwargs', NOT_FOUND)
def test_scanner_leaves_on_site_and_relative_references(app, kwargs):
    assert offsite_references(own_host=ONION, **kwargs) == []


def test_scanner_with_no_own_host_finds_every_absolute_reference(app):
    assert offsite_references(urls=[f'http://{ONION}/a.png', '/b.png']) == [f'http://{ONION}/a.png']


# ---------------------------------------------------------------------------------------------------------------------
# onion_safe

def branding_with(**values) -> Branding:
    fields = dict.fromkeys(Branding._fields, '')
    fields.update(name='Name', logo='/static/media/logo.svg')
    fields.update(values)
    return Branding(**fields)


def test_onion_safe_drops_each_offsite_reference_and_logs_it(app, caplog):
    branding = branding_with(
        logo=f'{ELSEWHERE}/logo.svg', logo_180='/static/media/180.png', logo_152=f'{ELSEWHERE}/152.png',
        logo_32='//elsewhere.example.org/32.png', logo_16=f'{ELSEWHERE}/16.png',
        additional_css=f'@import "{ELSEWHERE}/x.css";\n.own {{ background: url(/static/own.png) }}\n'
                       f'.far {{ background: url({ELSEWHERE}/far.png) }}',
        sidebar=f'![far]({ELSEWHERE}/side.png) sidebar text',
        sidebar_html=f'<p>sidebar text <img src="{ELSEWHERE}/side.png" alt="far"> <a href="{ELSEWHERE}/l">link</a></p>',
        about_html=f'<p style="background: url({ELSEWHERE}/bg.png)">about</p>'
                   f'<img src="/static/own.png" srcset="/static/own.png 1x, {ELSEWHERE}/two.png 2x">'
                   f'<style>.s {{ background: url({ELSEWHERE}/s.png) }}</style>')
    with caplog.at_level(logging.INFO, logger=app.logger.name):
        safe = onion_safe(branding, ONION)
    assert (safe.logo, safe.logo_180, safe.logo_152, safe.logo_32, safe.logo_16) == \
        ('', '/static/media/180.png', '', '', '')
    assert safe.additional_css == '@import "";\n.own { background: url(/static/own.png) }\n.far { background: url() }'
    assert safe.sidebar == branding.sidebar   # Markdown is the page's description text: text is no fetch
    assert safe.sidebar_html == f'<p>sidebar text <img alt="far"/> <a href="{ELSEWHERE}/l">link</a></p>'
    assert safe.about_html == '<p style="background: url()">about</p><img src="/static/own.png"/>' \
                              '<style>.s { background: url() }</style>'
    assert offsite_references(css=safe.additional_css, html=safe.sidebar_html + safe.about_html,
                              urls=safe[1:7], own_host=ONION) == []
    assert (safe.name, safe.description, safe.default_theme) == (branding.name, branding.description,
                                                                branding.default_theme)
    dropped = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    for field, reference in [('logo', f'{ELSEWHERE}/logo.svg'), ('logo_152', f'{ELSEWHERE}/152.png'),
                             ('logo_32', '//elsewhere.example.org/32.png'), ('logo_16', f'{ELSEWHERE}/16.png'),
                             ('additional_css', f'{ELSEWHERE}/x.css'), ('additional_css', f'{ELSEWHERE}/far.png'),
                             ('sidebar_html', f'{ELSEWHERE}/side.png'), ('about_html', f'{ELSEWHERE}/bg.png'),
                             ('about_html', f'{ELSEWHERE}/two.png'), ('about_html', f'{ELSEWHERE}/s.png')]:
        assert any(field in message and reference in message and ONION in message for message in dropped), \
            (field, reference)
    assert not any('/static/' in message for message in dropped)


def test_onion_safe_returns_on_site_branding_unchanged(app, caplog):
    branding = branding_with(additional_css='.a { background: url(/static/a.png) }',
                             sidebar_html='<p>Sidebar &amp; <b>bold</b><br></p>', about_html='<p>About</p>')
    with caplog.at_level(logging.INFO, logger=app.logger.name):
        assert onion_safe(branding, ONION) is branding
    assert caplog.records == []


# ---------------------------------------------------------------------------------------------------------------------
# On the onion's pages

def visit(app, path, onion=True, user=None, client=None):
    forget_login()   # each visit is its own caller
    client = client or app.test_client()
    base = f'http://{ONION}' if onion else f'https://{HELL}'
    if user is not None:
        with client.session_transaction(base_url=base) as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
    if not onion:
        return client.get(path, base_url=base)
    response = None
    for _ in range(3):   # a cookieless onion request is sent out and back for its session cookie (app/onion_guard.py)
        response = client.get(path, base_url=base, headers=ONION_HEADERS, environ_base={'REMOTE_ADDR': PROXY})
        if response.status_code != 303:
            break
        path = response.headers['Location']
    return response


@pytest.fixture
def onion_site(app, two_domains, monkeypatch, tmp_path):
    """The onion beside the first domain, open to visitors, with a community of the first domain (a local-only one,
    the onion's own, is shown to members only) holding an image post by a remote account with a remote avatar."""
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    monkeypatch.setitem(app.config, 'MEDIA_PROXY_DIR', str(tmp_path))
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.show_inoculation_block = False
    site.registration_mode = 'Open'
    site.sidebar_html = '<p>The shared sidebar</p>'
    db.session.commit()
    community = two_domains.local_community('onionart', two_domains.domains[0])   # a local-only one is members-only
    remote = make_user(make_instance(REMOTE_HOST, software='lemmy'), 'faraway')
    remote.avatar = File(source_url=REMOTE_AVATAR)
    db.session.commit()
    # a link post to a remote image, as an image post from a remote server is stored, with the image in its body too
    post = make_post_of_image(community, remote)
    clear_domain_cache()
    cache.clear()
    yield two_domains, post
    cache.clear()


def make_post_of_image(community, author):
    post = make_post(community, author, f'https://{REMOTE_HOST}/post/1', title='a remote cat')
    post.type = POST_TYPE_IMAGE
    post.url = REMOTE_IMAGE
    post.image = File(source_url=REMOTE_IMAGE, width=640, height=480)
    post.body_html = f'<p>look</p><p><img src="{REMOTE_IMAGE}" alt="cat"></p>'
    db.session.commit()
    return post


def script_nonces_and_handlers(body: str):
    soup = BeautifulSoup(body, 'html.parser')
    scripts = [tag for tag in soup.find_all('script')
               if tag.get('type') not in ('application/json', 'application/ld+json')]
    handlers = [(tag.name, name) for tag in soup.find_all(True) for name in tag.attrs if name.startswith('on')]
    sse = [tag['data-sse-url'] for tag in soup.find_all(attrs={'data-sse-url': True}) if tag['data-sse-url']]
    return {tag.get('nonce') for tag in scripts}, handlers, sse


# Off-site loads an onion page makes today outside branding, which no CSP directive can stop: reported, not allowed.
# A post page names its link in `<link rel="preconnect">` (app/post/routes.py, base.html), so the browser opens a
# connection to the remote host of the post's URL. Pinned so the day it is fixed this list must shrink.
KNOWN_OUTSIDE_BRANDING = {'/post/{post}': [REMOTE_IMAGE]}


def assert_policy_holds(app, response, known=(), own_host=ONION):
    """Every fetch the page makes satisfies the onion's policy: nothing off the onion (the scanner, whose own tests
    are above) beyond the `known` loads outside branding, a nonce on every script (strict-dynamic ignores 'self'), no
    inline event handler (script-src has no 'unsafe-inline'), and live updates on the onion."""
    assert response.status_code == 200, response.headers.get('Location')
    nonce = re.fullmatch(r"script-src 'self' 'nonce-([^']+)' .*", response.headers['Content-Security-Policy'])[1]
    assert response.headers['Content-Security-Policy'] == ONION_POLICY.format(nonce=nonce)
    body = response.get_data(as_text=True)
    assert offsite_references(html=body, own_host=own_host) == list(known)
    nonces, handlers, sse = script_nonces_and_handlers(body)
    assert nonces == {nonce}
    assert handlers == []
    assert offsite_references(urls=sse, own_host=own_host) == []
    return body


PAGES = ['/', f'/c/onionart@{HELL}', '/post/{post}', f'/u/faraway@{REMOTE_HOST}', '/search?q=cat', '/auth/login',
         '/auth/register']


@pytest.mark.parametrize('path', PAGES)
def test_onion_key_pages_load_nothing_the_policy_refuses(app, onion_site, monkeypatch, path):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the login and registration forms render a token
    _domains, post = onion_site
    client = app.test_client()
    # A browser holds the session cookie a login page gave it. Search is session-first on the onion, and its own
    # cookieless round trip sets no cookie for a logged-out visitor (reported: not branding's)
    visit(app, '/auth/login', client=client)
    assert_policy_holds(app, visit(app, path.format(post=post.id), client=client), KNOWN_OUTSIDE_BRANDING.get(path, ()))


@pytest.mark.parametrize('path', ['/home/live/all', f'/c/onionart@{HELL}', '/post/{post}', f'/u/faraway@{REMOTE_HOST}',
                                  '/search?q=cat'])
def test_onion_key_pages_signed_in_load_nothing_the_policy_refuses(app, onion_site, monkeypatch, path):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # a post's reply form renders a token
    monkeypatch.setitem(app.config, 'NOTIF_SERVER', f'https://{HELL}')   # as deployed: the first domain's origin
    domains, post = onion_site
    reader = domains.local_user('reader', domains.domains[1])
    body = assert_policy_holds(app, visit(app, path.format(post=post.id), user=reader),
                               KNOWN_OUTSIDE_BRANDING.get(path, ()))
    if path == '/home/live/all':
        assert f'data-sse-url="http://{ONION}/live/stream' in body   # live updates stay on the onion


@pytest.mark.parametrize('signed_in', [False, True])
def test_onion_pages_send_the_csp_for_anonymous_and_logged_in(app, onion_site, signed_in):
    domains, _post = onion_site
    reader = domains.local_user('cspreader', domains.domains[1]) if signed_in else None
    response = visit(app, '/', user=reader)
    assert response.status_code == 200
    assert response.headers['Content-Security-Policy'] == ONION_POLICY.format(nonce=g.nonce)


def test_onion_htmx_fragment_sends_no_policy_of_its_own(app, onion_site):
    """A fragment is governed by the page it lands in."""
    client = app.test_client()
    assert 'Content-Security-Policy' in visit(app, '/', client=client).headers   # the session cookie is now held
    fragment = client.get('/', base_url=f'http://{ONION}', environ_base={'REMOTE_ADDR': PROXY},
                          headers={**ONION_HEADERS, 'HX-Request': 'true'})
    assert fragment.status_code == 200
    assert 'Content-Security-Policy' not in fragment.headers


def test_onion_csp_keeps_proxied_remote_media_working(app, onion_site, tmp_path):
    """An onion post with remote media renders its proxy URL, which is on the onion and so satisfies img-src 'self';
    the proxy's own sandbox policy (set by the view) still stands."""
    _domains, post = onion_site
    response = visit(app, f'/post/{post.id}')
    body = assert_policy_holds(app, response, KNOWN_OUTSIDE_BRANDING['/post/{post}'])
    proxied = proxy_url(REMOTE_IMAGE)
    assert f'src="{proxied}"' in body
    assert REMOTE_HOST not in ' '.join(re.findall(r'(?:src|srcset|poster)="([^"]*)"', body))
    (tmp_path / cache_name(REMOTE_IMAGE, 'image/webp')).write_bytes(b'webp')
    media = visit(app, proxied)
    assert media.status_code == 200
    assert media.headers['Content-Security-Policy'] == SANDBOX


def test_clearnet_csp_unchanged(app, onion_site):
    domains, _post = onion_site
    anonymous = visit(app, '/', onion=False)
    assert anonymous.status_code == 200
    assert 'Content-Security-Policy' not in anonymous.headers
    reader = domains.local_user('clearreader', domains.domains[0])
    signed_in = visit(app, '/', onion=False, user=reader)
    assert signed_in.status_code == 200
    assert signed_in.headers['Content-Security-Policy'] == SCRIPT_POLICY.format(nonce=g.nonce)


def test_clearnet_register_page_still_has_no_policy(app, onion_site, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    domains, _post = onion_site
    reader = domains.local_user('registerreader', domains.domains[0])
    response = visit(app, '/auth/register', onion=False, user=reader)
    assert 'Content-Security-Policy' not in response.headers


# ---------------------------------------------------------------------------------------------------------------------
# Review Focus 2: the onion inherits the first domain's sidebar, and the first domain later adds an off-site image

def test_inherited_offsite_image_is_stripped_on_onion_only(app, onion_site, caplog):
    site = db.session.get(Site, 1)
    site.sidebar_html = f'<p>The shared sidebar <img src="{ELSEWHERE}/badge.png" alt="badge"></p>'
    site.logo = f'{ELSEWHERE}/logo.svg'
    site.additional_css = f'.banner {{ background: url({ELSEWHERE}/banner.png) }}'
    db.session.commit()
    clear_domain_cache()
    with caplog.at_level(logging.INFO, logger=app.logger.name):
        onion_home = visit(app, '/').get_data(as_text=True)
    first_home = visit(app, '/', onion=False).get_data(as_text=True)
    assert 'The shared sidebar' in onion_home
    for reference in (f'{ELSEWHERE}/badge.png', f'{ELSEWHERE}/logo.svg', f'{ELSEWHERE}/banner.png'):
        assert reference not in onion_home
        assert proxy_url(reference) not in onion_home   # dropped, not merely proxied
        assert reference in first_home
        assert any(reference in record.getMessage() and record.levelno == logging.INFO for record in caplog.records)
    with onion_request(app):
        assert current_branding().sidebar_html == '<p>The shared sidebar <img alt="badge"/></p>'
        assert app.jinja_env.globals['current_branding']().logo == ''
    with default_request(app):
        assert current_branding().sidebar_html == site.sidebar_html


def test_the_onions_own_offsite_value_is_stripped_too(app, onion_site):
    domains, _post = onion_site
    db.session.add(DomainBranding(site_domain_id=domains.domains[1].id, logo_32=f'{ELSEWHERE}/32.png',
                                  about_html=f'<p>about <img src="{ELSEWHERE}/a.png"></p>'))
    db.session.commit()
    clear_domain_cache()
    with onion_request(app):
        assert (current_branding().logo_32, current_branding().about_html) == ('', '<p>about <img/></p>')
    assert f'{ELSEWHERE}/a.png' not in visit(app, '/about').get_data(as_text=True)
