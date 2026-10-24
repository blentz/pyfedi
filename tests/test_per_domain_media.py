"""A domain's uploads are linked on that domain (plan C3, task 6).

shop.example federates beside hell.cloud (the test app's SERVER_NAME). A new upload's stored URL is on its uploader's
domain (a community's icon or banner on the community's), and what a peer receives names a local file on the domain
that federates it: the post's, or for an emoji, the domain serving the document. Owner ruling: nothing a sister domain
federates names hell.cloud. The same files are served on every one of our hosts (Caddy imports piefed_routes in every
site block), so a shop URL resolves. URLs stored before this are never rewritten, and hell.cloud's are built from
SERVER_URL as before (golden strings below)."""
import io
import json
import os
import re

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

import app.shared.community as community_module
import app.shared.tasks.pages as pages_module
from app import db
from app.activitypub.util import comment_model_to_json, post_to_page
from app.api.alpha.utils.upload import post_upload_community_image
from app.constants import COMMUNITY_KIND_VIDEO_CHANNEL, POST_STATUS_PUBLISHED, POST_TYPE_IMAGE, POST_TYPE_VIDEO, \
    SRC_API
from app.models import Emoji, File, Post, media_local_path, user_file
from app.shared.community import edit_community
from app.shared.media import media_of, store_uploaded_media
from app.shared.post import edit_post
from app.shared.tasks import media as media_tasks
from app.shared.tasks.media import probe_media
from app.shared.tasks.pages import send_post
from app.shared.upload import process_file_delete, process_upload
from app.storefront import default_domain
from app.utils import set_setting
from tests.factories import make_community_member
from tests.test_per_domain_actor_documents import site_named_hell  # noqa: F401  (fixture)
from tests.test_per_domain_object_ids import HELL_BASE, SHOP, SHOP_BASE, a_post, a_reply, captured, no_hell, \
    sites  # noqa: F401  (fixture)
from tests.test_shared_post_edit import _api_input
from tests.two_domains import HELL, request_on, two_domains  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('site', 'private_static_tree', 'site_named_hell')

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')
NAME = r'[0-9A-Za-z]{2}/[0-9A-Za-z]{2}/[0-9A-Za-z]{15}'


def an_image(filename='pic.png'):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), (10, 20, 30)).save(buffer, format='PNG')
    buffer.seek(0)
    return FileStorage(stream=buffer, filename=filename, content_type='image/png')


def page_sent(sent):
    """The post in the first activity send_post sent: inside its Create or Update, inside the community's Announce."""
    body = sent[0][1]
    while body['type'] in ('Announce', 'Create', 'Update'):
        body = body['object']
    return body


def a_video():
    with open(os.path.join(FIX, 'video_h264.mp4'), 'rb') as f:
        return FileStorage(stream=io.BytesIO(f.read()), filename='upload.bin')


# --------------------------------------------------------------------------
# stored URLs of new uploads
# --------------------------------------------------------------------------

def test_a_shop_users_upload_is_stored_on_the_shop(app, sites):
    url = process_upload(an_image(), user=sites.sam)
    assert re.fullmatch(rf'{SHOP_BASE}/static/media/posts/{NAME}\.\w+', url), url
    assert File.query.filter_by(source_url=url).one()


def test_a_hell_users_upload_is_unchanged(app, sites):
    """Golden string: SERVER_URL then the served path, as before C3."""
    url = process_upload(an_image(), user=sites.hal)
    assert re.fullmatch(rf'https://test\.piefed\.local/static/media/posts/{NAME}\.\w+', url), url


def test_an_upload_no_one_is_named_for_is_the_acting_domains(app, sites):
    """The API's community and account image uploads name no user: the file is the caller's domain's."""
    with request_on(app, sites.shop):
        assert process_upload(an_image()).startswith(f'{SHOP_BASE}/static/media/posts/')
    assert process_upload(an_image()).startswith(f'{HELL_BASE}/static/media/posts/')   # no request: the default


def test_the_api_community_image_upload_is_the_callers_domains(app, sites, monkeypatch):
    monkeypatch.setattr('app.api.alpha.utils.upload.authorise_api_user', lambda auth: sites.sam.id)
    with request_on(app, sites.shop):
        url = post_upload_community_image('token', an_image())['url']
    assert url.startswith(f'{SHOP_BASE}/static/media/communities/')


def test_a_feeds_upload_is_the_default_domains(app, sites):
    """Feeds are the default domain's (C1 ruling), whoever uploads their images."""
    with request_on(app, sites.shop):
        url = process_upload(an_image(), destination='feeds', owner=default_domain())
    assert url.startswith(f'{HELL_BASE}/static/media/feeds/')


def test_a_shop_users_own_upload_can_be_deleted(app, sites):
    url = process_upload(an_image(), user=sites.sam)
    path = 'app' + url[len(SHOP_BASE):]
    assert os.path.isfile(path)
    process_file_delete(url.replace(SHOP_BASE, HELL_BASE), sites.sam.id)   # as the media library on hell shows it
    assert not os.path.exists(path)
    assert File.query.filter_by(source_url=url).first() is None
    assert db.session.query(user_file).filter_by(user_id=sites.sam.id).count() == 0


def test_a_shop_posts_uploaded_image_is_the_shops_and_federates_on_the_shop(app, sites, http_mock, monkeypatch):
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    post = a_post(sites.wares, sites.sam)
    edit_post(_api_input(), post, POST_TYPE_IMAGE, SRC_API, user=sites.sam, uploaded_file=an_image())
    db.session.refresh(post)
    assert re.fullmatch(rf'{SHOP_BASE}/static/media/posts/{NAME}\.\w+', post.image.source_url)
    page = post_to_page(post)
    assert no_hell(page), json.dumps(page)
    assert page['attachment'] == [{'type': 'Image', 'url': post.image.source_url, 'name': post.image.alt_text}]
    assert page['image']['url'] == post.image.source_url
    sent = captured(monkeypatch, pages_module)
    send_post(post.id, edit=True, session=db.session)
    assert sent and all(no_hell(body) for _, body, _ in sent), sent
    assert page_sent(sent)['attachment'][0]['url'] == post.image.source_url


def test_a_hell_posts_uploaded_image_is_unchanged(app, sites, http_mock):
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    post = a_post(sites.pit, sites.hal)
    edit_post(_api_input(), post, POST_TYPE_IMAGE, SRC_API, user=sites.hal, uploaded_file=an_image())
    db.session.refresh(post)
    assert re.fullmatch(rf'https://test\.piefed\.local/static/media/posts/{NAME}\.\w+', post.image.source_url)


def test_an_image_link_a_shop_user_read_on_hell_is_stored_on_the_shop(app, sites, http_mock):
    """An API client uploads, is shown the URL on the storefront it asked (here hell.cloud), then posts it."""
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    post = a_post(sites.wares, sites.sam)
    edit_post(_api_input(url=f'{HELL_BASE}/static/media/posts/ab/cd/abcdefghijklmno.png'), post, POST_TYPE_IMAGE,
              SRC_API, user=sites.sam)
    db.session.refresh(post)
    assert post.url == f'{SHOP_BASE}/static/media/posts/ab/cd/abcdefghijklmno.png'
    assert post.image.source_url == post.url


def test_a_stored_image_link_sent_back_as_read_on_the_shop_is_kept(app, sites, http_mock):
    """A shop post whose image was stored on hell.cloud before C3, edited with its URL as the shop showed it: the same
    file of ours, so it is kept as stored, not deleted and re-made."""
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    stored = f'{HELL_BASE}/static/media/posts/ab/cd/abcdefghijklmno.png'
    post = a_post(sites.wares, sites.sam)
    post.type, post.url = POST_TYPE_IMAGE, stored
    post.image = File(source_url=stored)
    db.session.commit()
    image_id = post.image_id
    edit_post(_api_input(url=stored.replace(HELL_BASE, SHOP_BASE)), post, POST_TYPE_IMAGE, SRC_API, user=sites.sam)
    db.session.refresh(post)
    assert (post.url, post.image_id, post.image.source_url) == (stored, image_id, stored)


@pytest.mark.parametrize('where', ['hell', 'shop'])
def test_a_communitys_icon_and_banner_from_the_api_are_stored_on_its_domain(app, sites, monkeypatch, where):
    community, base = (sites.pit, HELL_BASE) if where == 'hell' else (sites.wares, SHOP_BASE)
    make_community_member(sites.sam if where == 'shop' else sites.hal, community, is_moderator=True)
    monkeypatch.setattr(community_module, 'authorise_api_user',
                        lambda auth, return_type=None: sites.sam if where == 'shop' else sites.hal)
    monkeypatch.setattr(community_module, 'is_image_url', lambda url: True)
    monkeypatch.setattr(community_module, 'make_image_sizes', lambda *args: None)
    read_on = SHOP_BASE if where == 'hell' else HELL_BASE   # the client was shown it on the other storefront
    data = {'title': 'Wares', 'description': '', 'rules': '', 'nsfw': False, 'restricted_to_mods': False,
            'local_only': False, 'discussion_languages': [], 'question_answer': False,
            'icon_url': f'{read_on}/static/media/communities/ab/cd/icon.png',
            'banner_url': f'{read_on}/static/media/communities/ab/cd/banner.png'}
    edit_community(data, community, SRC_API, auth='token')
    assert community.icon.source_url == f'{base}/static/media/communities/ab/cd/icon.png'
    assert community.image.source_url == f'{base}/static/media/communities/ab/cd/banner.png'


def test_a_communitys_icon_stored_on_hell_sent_back_as_read_on_the_shop_is_kept(app, sites, monkeypatch):
    community = sites.wares
    make_community_member(sites.sam, community, is_moderator=True)
    stored = [f'{HELL_BASE}/static/media/communities/ab/cd/{name}.png' for name in ('icon', 'banner')]
    community.icon, community.image = File(source_url=stored[0]), File(source_url=stored[1])
    db.session.commit()
    ids = (community.icon_id, community.image_id)
    monkeypatch.setattr(community_module, 'authorise_api_user', lambda auth, return_type=None: sites.sam)
    data = {'title': 'Wares', 'description': '', 'rules': '', 'nsfw': False, 'restricted_to_mods': False,
            'local_only': False, 'discussion_languages': [], 'question_answer': False,
            'icon_url': stored[0].replace(HELL_BASE, SHOP_BASE), 'banner_url': stored[1].replace(HELL_BASE, SHOP_BASE)}
    edit_community(data, community, SRC_API, auth='token')
    assert (community.icon_id, community.image_id) == ids
    assert [community.icon.source_url, community.image.source_url] == stored


def test_a_stored_url_is_not_rewritten(app, sites):
    """A shop post whose image was stored on hell.cloud before C3 keeps that URL."""
    post = a_post(sites.wares, sites.sam)
    post.type = POST_TYPE_IMAGE
    post.image = File(source_url=f'{HELL_BASE}/static/media/posts/ab/cd/old.png')
    db.session.commit()
    assert post_to_page(post)['attachment'][0]['url'] == f'{HELL_BASE}/static/media/posts/ab/cd/old.png'


# --------------------------------------------------------------------------
# URLs of local files built when a document is serialised
# --------------------------------------------------------------------------

@pytest.mark.parametrize('where', ['hell', 'shop'])
def test_a_posts_image_with_no_stored_url_is_named_on_the_posts_domain(app, sites, monkeypatch, where):
    community, author, base = (sites.pit, sites.hal, HELL_BASE) if where == 'hell' else \
        (sites.wares, sites.sam, SHOP_BASE)
    post = a_post(community, author)
    post.image = File(file_path='app/static/media/posts/ab/cd/sized.webp')
    db.session.commit()
    assert post_to_page(post)['image']['url'] == f'{base}/static/media/posts/ab/cd/sized.webp'
    sent = captured(monkeypatch, pages_module)
    send_post(post.id, session=db.session)
    assert page_sent(sent)['image']['url'] == f'{base}/static/media/posts/ab/cd/sized.webp'
    post.image.file_path = None
    post.image.thumbnail_path = 'app/static/media/posts/ab/cd/thumb.webp'
    db.session.commit()
    sent.clear()
    send_post(post.id, session=db.session)
    assert page_sent(sent)['image']['url'] == f'{base}/static/media/posts/ab/cd/thumb.webp'


@pytest.mark.parametrize('where', ['hell', 'shop'])
def test_an_emojis_icon_is_named_on_the_posts_domain(app, sites, where):
    community, author, base = (sites.pit, sites.hal, HELL_BASE) if where == 'hell' else \
        (sites.wares, sites.sam, SHOP_BASE)
    db.session.add(Emoji(token=':gold:', url=f'{HELL_BASE}/static/media/emoji/gold.png', instance_id=1))
    db.session.add(Emoji(token=':far:', url='https://peer.example/emoji/far.png', instance_id=1))
    post = a_post(community, author)
    post.body = 'shiny :gold: and :far:'
    reply = a_reply(post, author)
    reply.body = 'also :gold:'
    db.session.commit()
    icons = {tag['name']: tag['icon']['url'] for tag in post_to_page(post)['tag'] if tag['type'] == 'Emoji'}
    assert icons == {':gold:': f'{base}/static/media/emoji/gold.png', ':far:': 'https://peer.example/emoji/far.png'}
    note = comment_model_to_json(reply)
    [icon] = [tag['icon']['url'] for tag in note['tag'] if tag['type'] == 'Emoji']
    assert icon == f'{base}/static/media/emoji/gold.png'
    if where == 'shop':
        assert no_hell(post_to_page(post)) and no_hell(note)


# --------------------------------------------------------------------------
# media posts: the stored file and the thumbnail the Celery task makes
# --------------------------------------------------------------------------

@pytest.fixture
def channel(sites, monkeypatch):
    for community in (sites.pit, sites.wares):
        community.kind = COMMUNITY_KIND_VIDEO_CHANNEL
    db.session.commit()
    make_community_member(sites.sam, sites.wares, is_moderator=True)
    make_community_member(sites.hal, sites.pit, is_moderator=True)
    set_setting('allow_video_file_uploads', 'users')
    monkeypatch.setattr(media_tasks, 'task_selector', lambda key, **kw: None)
    monkeypatch.setattr(media_tasks, 'notify_about_post', lambda post: None)
    monkeypatch.setattr(media_tasks, 'make_image_sizes', lambda *args: None)
    return sites


@pytest.mark.parametrize('where', ['hell', 'shop'])
def test_a_media_upload_and_its_thumbnail_are_on_the_uploaders_domain(app, channel, where):
    community, author, base = (channel.pit, channel.hal, HELL_BASE) if where == 'hell' else \
        (channel.wares, channel.sam, SHOP_BASE)
    if where == 'hell':
        base = 'https://test.piefed.local'   # golden: SERVER_URL itself
    post = a_post(community, author)
    post.type = POST_TYPE_VIDEO
    assert store_uploaded_media(post, a_video(), 'video') is None
    db.session.commit()
    assert re.fullmatch(rf'{base}/static/media/posts/{NAME}\.mp4', post.url), post.url
    assert os.path.isfile(media_local_path(post))
    post_id = post.id
    probe_media(False, post_id)   # as the worker runs it: no request
    db.session.expire_all()
    post = db.session.get(Post, post_id)
    assert media_of(post)['status'] == 'ready' and post.status == POST_STATUS_PUBLISHED
    assert re.fullmatch(rf'{base}/static/media/posts/{NAME}\.jpg', post.image.source_url), post.image.source_url
    assert os.path.isfile(post.image.local_path_for_url(post.image.source_url))


def test_a_shop_media_post_on_hell_storage_is_still_its_own(app, channel):
    """A shop post whose media was stored on hell.cloud's URL before C3 is still found on disk."""
    post = a_post(channel.wares, channel.sam)
    post.type = POST_TYPE_VIDEO
    assert store_uploaded_media(post, a_video(), 'video') is None
    post.url = post.url.replace(SHOP_BASE, HELL_BASE)
    db.session.commit()
    assert os.path.isfile(media_local_path(post))


# --------------------------------------------------------------------------
# the media route on a sister host
# --------------------------------------------------------------------------

def test_the_media_route_serves_a_file_on_the_shop_host(app, sites):
    directory = os.path.join(app.static_folder, 'media', 'posts', 'zq', 'zq')
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, 'zqzqtask6probe.png')
    with open(path, 'wb') as out:
        out.write(an_image().stream.read())
    try:
        for host in (SHOP, HELL):
            response = app.test_client().get('/static/media/posts/zq/zq/zqzqtask6probe.png', base_url=f'https://{host}')
            assert response.status_code == 200, host
            assert response.data[:8] == b'\x89PNG\r\n\x1a\n'
            response.close()
    finally:
        os.unlink(path)
        for empty in (directory, os.path.dirname(directory)):
            if not os.listdir(empty):
                os.rmdir(empty)
