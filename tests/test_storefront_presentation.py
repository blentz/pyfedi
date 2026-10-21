"""What a visitor sees follows the storefront their request named (onion storefront spec §3.4).

Pages, media URLs, RSS feeds and the emails sent during a request use `storefront_url()`. Federation
(ActivityPub ids and the JSON a peer reads) stays on the canonical SERVER_URL, and a background job, which has no
request, uses the default storefront.
"""
import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET  # parses only this app's own feed

import pytest
from bs4 import BeautifulSoup
from flask import current_app, g, session as flask_session, url_for
from flask_login import login_user
from flask_wtf.csrf import generate_csrf

from app import cache, cli, db
from app.activitypub.util import served_by_us
from app.constants import NOTIF_POST, POST_TYPE_ARTICLE, POST_TYPE_IMAGE, POST_TYPE_LINK, SRC_WEB
from app.email import send_topic_suggestion
from app.models import File, Notification, Site, SiteDomain, Tag, UserExtraField
from app.request_hooks import OnUse
from app.shared.community import invite_with_email
import app.storefront as storefront
from app.storefront import clear_domain_cache, domain_for_host, is_local_host
from app.utils import community_link_to_href, feed_link_to_href, person_link_to_href, utcnow
from tests.factories import (grant_permission, make_community, make_community_member, make_domain, make_instance,
                             make_post, make_user, make_user_registration)
from tests.podcast_fixtures import episode_post  # noqa: F401  (a fixture)

ONION = 'abc.onion'
CANONICAL = 'test.piefed.local'
MEDIA = 'app/static/media/posts/ab/cd/abcdefghijklmno.jpg'
THUMB = 'app/static/media/posts/ab/cd/abcdefghijklmno_t.webp'

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def onion(db_session):
    if SiteDomain.query.filter_by(domain=CANONICAL).first() is None:   # the canonical host stays served
        db.session.add(SiteDomain(domain=CANONICAL, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    cache.clear()
    yield ONION
    clear_domain_cache()


@pytest.fixture
def world(app, onion):
    site = db.session.get(Site, 1)
    site.private_instance = False
    local = make_instance(CANONICAL, software='piefed')
    founder = make_user(local, 'founder', local=True)
    author = make_user(local, 'author', local=True)
    author.ap_profile_id = f'https://{CANONICAL}/u/author'
    author.ap_public_url = f'https://{CANONICAL}/u/author'
    community = make_community('general')
    make_community_member(author, community)
    image = File(file_path=MEDIA, thumbnail_path=THUMB, source_url=f'https://{CANONICAL}/static/media/posts/ab/cd/'
                                                                    'abcdefghijklmno.jpg')
    db.session.add(image)
    db.session.commit()
    post = make_post(community, author, f'https://{CANONICAL}/post/1', title='a picture')
    post.type = POST_TYPE_IMAGE
    post.image_id = image.id
    post.url = image.source_url
    post.body_html = '<p>the body</p>'
    author.post_count = 1
    db.session.commit()
    return founder, author, community, post, image


def on_onion(app, user=None):
    # the suite's requests share the fixture's app context, so flask.g (and Flask-Login's user in it) carries over
    g.pop('_login_user', None)
    client = app.test_client()
    if user is not None:
        with client.session_transaction(base_url=f'https://{ONION}') as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True
    return client


def absolute_urls(html):
    """Every absolute or scheme-relative URL in an href, src, srcset, action or meta content attribute."""
    soup = BeautifulSoup(html, 'html.parser')
    found = []
    for tag in soup.find_all(True):
        values = [tag.get(name) for name in ('href', 'src', 'action')]
        if tag.name == 'meta':
            values.append(tag.get('content'))
        srcset = tag.get('srcset')
        if srcset:
            values.extend(candidate.strip().split(' ')[0] for candidate in srcset.split(','))
        for value in values:
            if isinstance(value, str) and (value.startswith('//') or '://' in value[:12]):
                found.append(value)
    return found


def test_page_urls_stay_on_storefront(app, world):
    founder, author, community, post, image = world
    # canonical sources (ActivityPub ids, stored content) are moved onto the storefront by the response rewrite
    pages = {'/': None, f'/c/general@{CANONICAL}': None, f'/post/{post.id}': None, f'/u/author@{CANONICAL}': None, '/auth/login': None,
             '/auth/register': None, '/user/settings': founder, '/search?q=x': None,
             f'/post/{post.id}/embed_code': None}
    offenders = []
    for path, user in pages.items():
        with patch.dict(app.config, {'WTF_CSRF_ENABLED': True}):   # the login and register forms render their token
            response = on_onion(app, user).get(path, base_url=f'https://{ONION}')  # https: the session cookie is Secure
        assert response.status_code == 200, (path, response.status_code, response.headers.get('Location'))
        text = response.get_data(as_text=True)
        # the embed code is markup inside a <textarea>, to be pasted elsewhere: its URLs are checked as markup too
        for textarea in BeautifulSoup(text, 'html.parser').find_all('textarea'):
            text += textarea.get_text()
        for url in absolute_urls(text):
            parts = urlsplit(url)
            if parts.hostname == ONION:
                if parts.scheme != 'http':   # the alias's configured scheme: an http onion serves no TLS
                    offenders.append((path, url))
            elif is_local_host(parts.hostname):   # any other host of ours is a different storefront
                offenders.append((path, url))
    assert offenders == [], offenders


def test_page_urls_on_the_canonical_host_are_unchanged(app, world):
    response = on_onion(app).get('/c/general', headers={'Host': CANONICAL})
    assert f'https://{CANONICAL}/community/general/feed' in response.get_data(as_text=True)


def test_media_url_on_storefront(app, onion):
    image = File(file_path=MEDIA, thumbnail_path=THUMB)
    with app.test_request_context(base_url=f'http://{ONION}'):
        app.preprocess_request()
        assert image.view_url() == f'http://{ONION}/static/media/posts/ab/cd/abcdefghijklmno.jpg'
        assert image.medium_url() == f'http://{ONION}/static/media/posts/ab/cd/abcdefghijklmno.jpg'
        assert image.thumbnail_url() == f'http://{ONION}/static/media/posts/ab/cd/abcdefghijklmno_t.webp'
        assert image.view_url(canonical=True) == f'https://{CANONICAL}/static/media/posts/ab/cd/abcdefghijklmno.jpg'
        assert image.medium_url(canonical=True) == f'https://{CANONICAL}/static/media/posts/ab/cd/abcdefghijklmno.jpg'
        assert image.thumbnail_url(canonical=True) == \
            f'https://{CANONICAL}/static/media/posts/ab/cd/abcdefghijklmno_t.webp'
        assert File(thumbnail_path=THUMB).medium_url(canonical=True) == \
            f'https://{CANONICAL}/static/media/posts/ab/cd/abcdefghijklmno_t.webp'


def test_served_by_us_knows_the_canonical_media_urls(app, onion):
    image = File(file_path=MEDIA, thumbnail_path=THUMB)
    with app.test_request_context(base_url=f'http://{ONION}'):
        app.preprocess_request()
        # a peer echoes back the canonical URL it was sent, never the storefront's
        assert served_by_us(f'https://{CANONICAL}/static/media/posts/ab/cd/abcdefghijklmno.jpg', image)


def test_post_ap_json_image_is_canonical(app, world):
    founder, author, community, post, image = world
    response = on_onion(app).get(f'/post/{post.id}', headers={'Host': ONION, 'Accept': 'application/activity+json'})
    assert response.status_code == 200
    assert urlsplit(response.get_json()['image']['url']).hostname == CANONICAL


def test_feed_on_storefront(app, onion, episode_post):
    db.session.get(Site, 1).private_instance = False
    db.session.commit()
    response = on_onion(app).get(f'/community/pod@{CANONICAL}/feed', headers={'Host': ONION})   # hell's, by handle (C1)
    assert response.status_code == 200
    channel = ET.fromstring(response.data).find('channel')
    assert urlsplit(channel.findtext('link')).hostname == ONION
    item = channel.find('item')
    assert urlsplit(item.findtext('link')).hostname == ONION
    assert urlsplit(item.find('enclosure').get('url')).hostname == ONION


# A bare name on the onion is the onion's own (plan C1): hell's rows are named by their handle there
@pytest.mark.parametrize('path', ['/index/feed', '/d/{domain}/feed', '/tag/pics/feed', f'/u/author@{CANONICAL}/feed',
                                  f'/community/general@{CANONICAL}/feed'])
def test_other_feeds_link_to_the_storefront(app, world, path):
    founder, author, community, post, image = world
    domain = make_domain('pics.example')
    tag = Tag(name='pics', display_as='Pics')
    db.session.add(tag)
    if path == '/index/feed':   # Local lists the visited domain's own communities, so the onion's holds this one
        onion = SiteDomain.query.filter_by(domain=ONION).one()
        community.site_domain_id, community.instance_id = onion.id, domain_for_host(ONION).instance_id
    post.domain_id = domain.id
    post.tags.append(tag)
    db.session.commit()
    response = on_onion(app).get(path.format(domain=domain.id), headers={'Host': ONION})
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert f'http://{ONION}/post/{post.id}' in text
    assert f'<link>https://{CANONICAL}' not in text   # the guid stays the post's ActivityPub id
    assert f'"https://{CANONICAL}/static' not in text


def test_reset_email_uses_the_accounts_home_domain(app, world):
    founder = world[0]
    founder.site_domain_id = SiteDomain.query.filter_by(domain=ONION).one().id   # an onion account (spec §3.2)
    db.session.commit()
    with patch('app.email.send_email') as send:
        response = on_onion(app).post('/auth/reset_password_request', data={'email': founder.email},
                                      headers={'Host': ONION})
    assert response.status_code == 302
    kwargs = send.call_args.kwargs
    for body in (kwargs['text_body'], kwargs['html_body']):
        assert f'http://{ONION}/' in body
        assert CANONICAL not in body


def test_background_email_uses_default(app, world):
    founder, author, community, post, image = world
    author.email_unread = True
    author.email_unread_sent = False
    author.last_seen = utcnow() - timedelta(days=2)
    post.posted_at = utcnow()
    post.slug = '/c/general/p/1/a-picture'
    db.session.add(Notification(user_id=author.id, title='a notification', url='/x', notif_type=NOTIF_POST,
                                created_at=utcnow() - timedelta(hours=1)))
    db.session.commit()
    sent = []
    with patch('app.cli.send_email', lambda *a, **k: sent.append(k['html_body'])):
        cli.register(current_app)
        result = current_app.test_cli_runner().invoke(args=['send_missed_notifs'])
    assert result.exception is None, result.exception
    assert len(sent) == 1
    assert f'https://{CANONICAL}/c/general/p/1/a-picture' in sent[0]
    assert f'href="https://{CANONICAL}/"' in sent[0]
    assert ONION not in sent[0]


def test_link_filters_use_the_storefront(app, onion):
    with app.test_request_context(base_url=f'http://{ONION}'):
        app.preprocess_request()
        assert f'href="http://{ONION}/community/lookup/' in community_link_to_href('!a@b.example')
        assert f'href="http://{ONION}/feed/lookup/' in feed_link_to_href('~a@b.example')
        assert f'href="http://{ONION}/user/lookup/' in person_link_to_href('@a@b.example')
        assert f'href="https://x.example/community/lookup/' in community_link_to_href('!a@b.example', 'x.example')


def test_storefront_link_moves_only_our_urls(app, onion):
    with app.test_request_context(base_url=f'http://{ONION}'):
        app.preprocess_request()
        assert storefront.storefront_host() == ONION
        assert storefront.storefront_link(f'https://{CANONICAL}/static/a.mp3?x=1#t') == f'http://{ONION}/static/a.mp3?x=1#t'
        assert storefront.storefront_link(f'//{CANONICAL}/a') == f'http://{ONION}/a'
        assert storefront.storefront_link('https://cdn.example/a.mp3') == 'https://cdn.example/a.mp3'
        assert storefront.storefront_link(None) is None
        assert storefront.canonical_link(f'http://{ONION}/static/a.png') == f'https://{CANONICAL}/static/a.png'
        assert storefront.canonical_link('https://cdn.example/a.png') == 'https://cdn.example/a.png'
    with app.app_context():
        assert storefront.storefront_host() == CANONICAL
        assert storefront.storefront_link(f'http://{ONION}/a') == f'https://{CANONICAL}/a'


def test_rsl_link_header_and_manifest_on_storefront(app, world):
    client = on_onion(app)
    with patch.dict(current_app.config, {'ALLOW_AI_CRAWLERS': False}):
        response = client.get('/', headers={'Host': ONION})
    assert f'<http://{ONION}/rsl.xml>; rel="license"; type="application/rsl+xml"' in response.headers.getlist('Link')
    manifest = client.get('/manifest.json', headers={'Host': ONION})
    assert manifest.get_json()['id'] == f'http://{ONION}'


def test_oembed_on_storefront(app, world):
    post = world[3]
    response = on_onion(app).get(f'/post/{post.id}/oembed', headers={'Host': ONION})
    data = response.get_json()
    assert data['provider_url'] == f'http://{ONION}'
    assert f"src='http://{ONION}/static/js/embed.js'" in data['html']


def test_template_storefront_values_are_computed_only_on_use():
    calls = []

    def compute():
        calls.append(1)
        return 'http://abc.onion'

    value = OnUse(compute)
    assert calls == []
    assert f'{value}/x' == 'http://abc.onion/x'
    assert value + '/p/1' == 'http://abc.onion/p/1'
    assert bool(value) and not OnUse(lambda: '')
    assert len(calls) == 3


# --- mail to someone other than the requester follows the default storefront (fix round 1, I1) ---------------------

def post_on_onion(app, user, path, data=None, headers=None):
    """A logged-in POST on the onion, carrying the CSRF token login_required checks."""
    client = on_onion(app, user)
    with app.test_request_context(base_url=f'https://{ONION}'):
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction(base_url=f'https://{ONION}') as session:
        session['csrf_token'] = raw
    return client.post(path, base_url=f'https://{ONION}', data={**(data or {}), 'csrf_token': token},
                       headers={**(headers or {}), 'X-CSRFToken': token})


def _assert_on_default(bodies):
    assert any(f'https://{CANONICAL}/' in body for body in bodies)
    assert not any(ONION in body for body in bodies)


def test_default_storefront_renders_under_the_default_and_restores(app, onion):
    with app.test_request_context(base_url=f'http://{ONION}'):
        app.preprocess_request()
        with storefront.default_storefront():
            assert storefront.storefront_url() == f'https://{CANONICAL}'
            assert url_for('main.index', _external=True) == f'https://{CANONICAL}/home'
        assert storefront.storefront_url() == f'http://{ONION}'
        assert url_for('main.index', _external=True) == f'http://{ONION}/home'
    with app.test_request_context(base_url=f'http://{ONION}'):
        g.pop('storefront', None)   # a request whose storefront was never resolved
        with storefront.default_storefront():
            assert g.storefront.domain == CANONICAL
        assert 'storefront' not in g
    with app.app_context():
        with storefront.default_storefront():   # outside a request it is already the default
            assert storefront.storefront_url() == f'https://{CANONICAL}'


def test_newsletter_sent_from_the_onion_links_to_the_default(app, world):
    founder = world[0]
    grant_permission(founder, 'change instance settings')
    sent = []
    with patch('app.admin.util.send_email', side_effect=lambda **kwargs: sent.append(kwargs)):
        response = post_on_onion(app, founder, '/admin/newsletter',
                                 {'subject': 'Hi', 'body_text': 'text', 'body_html': '<p>html</p>'})
    assert response.status_code == 302
    _assert_on_default([body for kwargs in sent for body in (kwargs['text_body'], kwargs['html_body'])])


def test_registration_approved_from_the_onion_links_to_the_default(app, world):
    founder = world[0]
    grant_permission(founder, 'approve registrations')
    newcomer = make_user(None, 'newcomer', local=True)
    make_user_registration(newcomer)
    with patch('app.email.send_email') as send:
        response = post_on_onion(app, founder, f'/admin/approve_registrations/{newcomer.id}/approve')
    assert response.status_code == 302
    _assert_on_default([send.call_args.kwargs['html_body']])


def test_verification_resent_by_an_admin_on_the_onion_links_to_the_default(app, world):
    founder, author = world[0], world[1]
    grant_permission(founder, 'administer all users')
    with patch('app.email.send_email') as send:
        response = post_on_onion(app, founder, f'/admin/user/{author.id}/resend_email', headers={'HX-Request': 'true'})
    assert response.status_code == 200
    _assert_on_default([send.call_args.kwargs['text_body'], send.call_args.kwargs['html_body']])


def test_community_invite_from_the_onion_links_to_the_default(app, world):
    author, community = world[1], world[2]
    with app.test_request_context(base_url=f'http://{ONION}'):
        app.preprocess_request()
        login_user(author)
        with patch('app.shared.community.send_email') as send:
            assert invite_with_email(community.id, 'friend@example.com', SRC_WEB) == 1
    _assert_on_default([send.call_args.args[3], send.call_args.args[4]])


def test_topic_suggestion_from_the_onion_links_to_the_default(app, world):
    author = world[1]
    with app.test_request_context(base_url=f'http://{ONION}'):
        app.preprocess_request()
        with patch('app.email.send_email') as send:
            send_topic_suggestion('general', author, ['admin@example.com'], 'A topic', 'Pies')
    _assert_on_default([send.call_args.kwargs['html_body']])


def test_community_move_request_from_the_onion_links_to_the_default(app, world):
    author = world[1]
    remote = make_community('elsewhere', host='remote.example')
    remote.ap_id = 'elsewhere@remote.example'
    db.session.commit()
    with patch('app.community.routes.send_email') as send:
        response = post_on_onion(app, author, '/community/elsewhere@remote.example/move',
                                 {'old_community_locked': 'y', 'post_link': 'https://remote.example/post/1'})
    assert response.status_code == 200
    _assert_on_default([send.call_args.args[3], send.call_args.args[4]])


# --- page snippets that name the storefront's origin (fix round 1, I2) ---------------------------------------------

def test_embed_code_names_the_storefront_origin(app, world):
    post = world[3]
    response = on_onion(app).get(f'/post/{post.id}/embed_code', base_url=f'https://{ONION}')
    text = response.get_data(as_text=True)
    assert f'<script src="http://{ONION}/static/js/embed.js"' in text
    assert f'https://{ONION}' not in text


def test_share_bookmarklet_names_the_storefront_origin(app, world):
    author, post = world[1], world[3]
    post.type, post.url, post.image_id = POST_TYPE_ARTICLE, None, None   # an image post's edit page HEADs its url
    db.session.commit()
    with patch.dict(app.config, {'WTF_CSRF_ENABLED': True}):   # the edit form renders its token
        response = on_onion(app, author).get(f'/post/{post.id}/edit', base_url=f'https://{ONION}')
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert f"window.open('http://{ONION}/share?url=" in text
    assert f'https://{ONION}' not in text


# --- JSON-LD page metadata follows the storefront (final review item 9) -------------------------------------------

def _json_ld_urls(html):
    """Every string value in the page's JSON-LD blocks that is an absolute or scheme-relative URL."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and (value.startswith('//') or '://' in value[:12]):
            found.append(value)

    for script in BeautifulSoup(html, 'html.parser').find_all('script', type='application/ld+json'):
        walk(json.loads(script.string))
    return found


@pytest.mark.parametrize('host, origin', [(ONION, f'http://{ONION}'), (CANONICAL, f'https://{CANONICAL}')])
def test_json_ld_urls_name_the_storefront(app, world, host, origin):
    founder, author, community, post, image = world
    post.type = POST_TYPE_LINK   # a link post's image is in the JSON-LD
    author.indexable = author.searchable = post.indexable = True
    author.extra_fields.append(UserExtraField(label='home', text=f'https://{CANONICAL}/u/author'))
    community.ap_profile_id = None   # the fallback URL, built for the page rather than read from the row
    db.session.commit()
    for path in (f'/post/{post.id}', f'/u/author@{CANONICAL}'):   # a hosted handle: the author is on the default domain
        response = on_onion(app).get(path, base_url=f'https://{host}')
        assert response.status_code == 200, (path, response.status_code)
        urls = _json_ld_urls(response.get_data(as_text=True))
        ours = [url for url in urls if is_local_host(urlsplit(url).hostname)]
        assert ours, (path, urls)
        assert all(url.startswith(f'{origin}/') or url == origin for url in ours), (path, ours)
