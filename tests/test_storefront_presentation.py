"""What a visitor sees follows the storefront their request named (onion storefront spec §3.4).

Pages, media URLs, RSS feeds and the emails sent during a request use `storefront_url()`. Federation
(ActivityPub ids and the JSON a peer reads) stays on the canonical SERVER_URL, and a background job, which has no
request, uses the default storefront.
"""
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET  # parses only this app's own feed

import pytest
from bs4 import BeautifulSoup
from flask import current_app, g

from app import cache, cli, db
from app.activitypub.util import served_by_us
from app.constants import NOTIF_POST, POST_TYPE_IMAGE
from app.models import File, Notification, Site, SiteDomain, Tag
from app.request_hooks import OnUse
import app.storefront as storefront
from app.storefront import clear_domain_cache
from app.utils import community_link_to_href, feed_link_to_href, person_link_to_href, utcnow
from tests.factories import (make_community, make_community_member, make_domain, make_instance, make_post,
                             make_user)
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
    # Task 6 rewrites at render time: each of these comes from a source the Task 4a inventory classified canonical
    # (an ActivityPub id or url base) or from stored content (spec §3.5). Task 6's response rewrite moves them onto
    # the storefront; it deletes this set and asserts the offenders are [] with nothing subtracted.
    task_6_rewrites = {
        community.public_url(),     # Task 6 rewrites at render time: Community.public_url() (canonical <link>, hrefs)
        author.public_url(),        # Task 6 rewrites at render time: User.public_url() (canonical <link>, ap alternate)
        post.ap_id,                 # Task 6 rewrites at render time: Post.ap_id, the post page's <link rel=canonical>
        image.source_url,           # Task 6 rewrites at render time: File.source_url, stored (og:image, post.url)
        f'https://{CANONICAL}',     # Task 6 rewrites at render time: Instance.domain, stored (new-instances sidebar)
    }
    pages = {'/': None, '/c/general': None, f'/post/{post.id}': None, '/u/author': None, '/auth/login': None,
             '/auth/register': None, '/user/settings': founder, '/search?q=x': None}
    offenders, exempted = [], set()
    for path, user in pages.items():
        with patch.dict(app.config, {'WTF_CSRF_ENABLED': True}):   # the login and register forms render their token
            response = on_onion(app, user).get(path, base_url=f'https://{ONION}')  # https: the session cookie is Secure
        assert response.status_code == 200, (path, response.status_code, response.headers.get('Location'))
        for url in absolute_urls(response.get_data(as_text=True)):
            host = urlsplit(url).hostname
            if host == CANONICAL:
                if url in task_6_rewrites:
                    exempted.add(url)
                else:
                    offenders.append((path, url))
    assert offenders == [], offenders
    assert exempted == task_6_rewrites   # each exemption is still needed: the set stays minimal


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
    response = on_onion(app).get('/community/pod/feed', headers={'Host': ONION})
    assert response.status_code == 200
    channel = ET.fromstring(response.data).find('channel')
    assert urlsplit(channel.findtext('link')).hostname == ONION
    item = channel.find('item')
    assert urlsplit(item.findtext('link')).hostname == ONION
    assert urlsplit(item.find('enclosure').get('url')).hostname == ONION


@pytest.mark.parametrize('path', ['/index/feed', '/d/{domain}/feed', '/tag/pics/feed', '/u/author/feed',
                                  '/community/general/feed'])
def test_other_feeds_link_to_the_storefront(app, world, path):
    founder, author, community, post, image = world
    domain = make_domain('pics.example')
    tag = Tag(name='pics', display_as='Pics')
    db.session.add(tag)
    post.domain_id = domain.id
    post.tags.append(tag)
    db.session.commit()
    response = on_onion(app).get(path.format(domain=domain.id), headers={'Host': ONION})
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert f'http://{ONION}/post/{post.id}' in text
    assert f'<link>https://{CANONICAL}' not in text   # the guid stays the post's ActivityPub id
    assert f'"https://{CANONICAL}/static' not in text


def test_reset_email_uses_storefront(app, world):
    founder = world[0]
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
