"""A domain's uploads are linked on that domain (plan C3, task 6).

shop.example federates beside the first domain (the test app's SERVER_NAME). A new upload's stored URL is on its uploader's
domain (a community's icon or banner on the community's), and what a peer receives names a local file on the domain
that federates it: the post's, or for an emoji, the domain serving the document. Owner ruling: nothing a sister domain
federates names the first domain. The same files are served on every one of our hosts (Caddy imports piefed_routes in every
site block), so a shop URL resolves. URLs stored before this are never rewritten, and the first domain's are built from
SERVER_URL as before (golden strings below)."""
import io
import json
import os
import re

from unittest.mock import patch

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

import app.api.alpha.utils.user as user_utils_module
import app.api.alpha.utils.private_message as private_message_module
import app.chat.util as chat_util
import app.shared.community as community_module
import app.shared.reply as reply_module
import app.shared.tasks.pages as pages_module
from app import db
from app.activitypub.util import comment_model_to_json, post_to_page
from app.api.alpha.utils.upload import post_upload_community_image
from app.api.alpha.utils.private_message import put_private_message
from app.api.alpha.utils.user import put_user_save_user_settings
from app.constants import COMMUNITY_KIND_VIDEO_CHANNEL, POST_STATUS_PUBLISHED, POST_TYPE_ARTICLE, POST_TYPE_IMAGE, \
    POST_TYPE_LINK, POST_TYPE_VIDEO, SRC_API
from app.models import Community, CommunityWikiPage, Conversation, Emoji, File, Language, Post, PostReply, Site, User, \
    media_local_path, user_file
from app.shared.community import edit_community
from app.shared.media import media_of, store_uploaded_media
from app.shared.post import edit_post
from app.shared.reply import edit_reply, make_reply
from app.shared.tasks import media as media_tasks
from app.shared.tasks.media import probe_media
from app.shared.tasks.pages import send_post
from app.shared.upload import process_file_delete, process_upload
from app.shared.feed import store_icon_and_banner
from app.storefront import canonicalise_links, canonicalise_text
from app.utils import set_setting
from tests.factories import grant_permission, make_community_member
from tests.test_admin_user_administration import _add_payload, _roles
from tests.test_community_lifecycle import _add_payload as _community_payload, able_to_create, csrf
from tests.test_per_domain_actor_documents import site_named_first  # noqa: F401  (fixture)
from tests.test_per_domain_object_ids import FIRST_BASE, SHOP, SHOP_BASE, a_post, a_reply, captured, no_first, \
    sites  # noqa: F401  (fixture)
from tests.test_shared_post_edit import _api_input
from tests.two_domains import FIRST, ONION, request_on, two_domains  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('site', 'private_static_tree', 'site_named_first')

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')
NAME = r'[0-9A-Za-z]{2}/[0-9A-Za-z]{2}/[0-9A-Za-z]{15}'
DEFAULT_BASE = 'https://test.piefed.local'   # SERVER_URL itself: what a local-only owner's URLs are stored on


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


def test_a_first_users_upload_is_unchanged(app, sites):
    """Golden string: SERVER_URL then the served path, as before C3."""
    url = process_upload(an_image(), user=sites.hal)
    assert re.fullmatch(rf'https://test\.piefed\.local/static/media/posts/{NAME}\.\w+', url), url


def test_an_upload_no_one_is_named_for_is_the_acting_domains(app, sites):
    """The API's community and account image uploads name no user: the file is the caller's domain's."""
    with request_on(app, sites.shop):
        assert process_upload(an_image()).startswith(f'{SHOP_BASE}/static/media/posts/')
    assert process_upload(an_image()).startswith(f'{FIRST_BASE}/static/media/posts/')   # no request: the default


def test_the_api_community_image_upload_is_the_callers_domains(app, sites, monkeypatch):
    monkeypatch.setattr('app.api.alpha.utils.upload.authorise_api_user', lambda auth: sites.sam.id)
    with request_on(app, sites.shop):
        url = post_upload_community_image('token', an_image())['url']
    assert url.startswith(f'{SHOP_BASE}/static/media/communities/')


def test_a_feeds_upload_is_its_domains(app, sites):
    """A feed's images are stored on the feed's home domain (per-domain feeds), wherever the upload is made."""
    with request_on(app, sites.first):
        shop_icon, shop_banner = store_icon_and_banner(an_image(), an_image(), sites.shop)
        first_icon, first_banner = store_icon_and_banner(an_image(), an_image(), sites.first)
    assert shop_icon.startswith(f'{SHOP_BASE}/static/media/feeds/') and shop_banner.startswith(f'{SHOP_BASE}/static/media/feeds/')
    assert first_icon.startswith(f'{FIRST_BASE}/static/media/feeds/') and first_banner.startswith(f'{FIRST_BASE}/static/media/feeds/')


def test_a_shop_users_own_upload_can_be_deleted(app, sites):
    url = process_upload(an_image(), user=sites.sam)
    path = 'app' + url[len(SHOP_BASE):]
    assert os.path.isfile(path)
    process_file_delete(url.replace(SHOP_BASE, FIRST_BASE), sites.sam.id)   # as the media library on the first domain shows it
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
    assert no_first(page), json.dumps(page)
    assert page['attachment'] == [{'type': 'Image', 'url': post.image.source_url, 'name': post.image.alt_text}]
    assert page['image']['url'] == post.image.source_url
    sent = captured(monkeypatch, pages_module)
    send_post(post.id, edit=True, session=db.session)
    assert sent and all(no_first(body) for _, body, _ in sent), sent
    assert page_sent(sent)['attachment'][0]['url'] == post.image.source_url


def test_an_image_a_first_admin_uploads_into_a_shop_post_is_the_posts(app, sites, http_mock):
    """A moderator's or admin's upload while editing someone else's post is the post's: the shop post federates it
    (ruling 4), so it is stored on the shop."""
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    post = a_post(sites.wares, sites.sam)
    edit_post(_api_input(), post, POST_TYPE_IMAGE, SRC_API, user=sites.hal, uploaded_file=an_image())
    db.session.refresh(post)
    assert re.fullmatch(rf'{SHOP_BASE}/static/media/posts/{NAME}\.\w+', post.image.source_url)
    assert no_first(post_to_page(post))


def test_a_first_posts_uploaded_image_is_unchanged(app, sites, http_mock):
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    post = a_post(sites.pit, sites.hal)
    edit_post(_api_input(), post, POST_TYPE_IMAGE, SRC_API, user=sites.hal, uploaded_file=an_image())
    db.session.refresh(post)
    assert re.fullmatch(rf'https://test\.piefed\.local/static/media/posts/{NAME}\.\w+', post.image.source_url)


def test_an_image_link_a_shop_user_read_on_first_is_stored_on_the_shop(app, sites, http_mock):
    """An API client uploads, is shown the URL on the storefront it asked (here the first domain), then posts it."""
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    post = a_post(sites.wares, sites.sam)
    edit_post(_api_input(url=f'{FIRST_BASE}/static/media/posts/ab/cd/abcdefghijklmno.png'), post, POST_TYPE_IMAGE,
              SRC_API, user=sites.sam)
    db.session.refresh(post)
    assert post.url == f'{SHOP_BASE}/static/media/posts/ab/cd/abcdefghijklmno.png'
    assert post.image.source_url == post.url


@pytest.mark.parametrize('author, typed, stored', [
    ('sam', f'{FIRST_BASE}/post/5', f'{FIRST_BASE}/post/5'),   # The first domain's post, named on the first domain: the shop would 404 it
    ('sam', f'{FIRST_BASE}/c/pit', f'{FIRST_BASE}/c/pit'),
    ('sam', f'{FIRST_BASE}/u/hal', f'{FIRST_BASE}/u/hal'),
    ('hal', f'{SHOP_BASE}/post/5', f'{SHOP_BASE}/post/5'),
    ('hal', f'{SHOP_BASE}/c/wares', f'{SHOP_BASE}/c/wares'),
    ('ollie', f'{FIRST_BASE}/post/5', f'{FIRST_BASE}/post/5'),
    ('ollie', f'http://{ONION}/post/5', f'{DEFAULT_BASE}/post/5'),   # the onion's host is never stored
    ('sam', f'http://{ONION}/post/5', f'{DEFAULT_BASE}/post/5'),
    ('sam', f'http://{ONION}/static/media/posts/ab/cd/abcdefghijklmno.png',
     f'{SHOP_BASE}/static/media/posts/ab/cd/abcdefghijklmno.png'),   # a file of ours: the post's to federate
])
def test_a_pasted_link_keeps_the_federating_host_typed(app, onion_rows, http_mock, author, typed, stored):
    """Only a file of ours moves (onto the post's domain), and a local-only host (onto the default domain's base);
    a link to any federating domain of ours names that domain's object and keeps its host (ruling 2)."""
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    user = getattr(onion_rows, author)
    community = {'sam': onion_rows.wares, 'hal': onion_rows.pit, 'ollie': onion_rows.pit}[author]
    post = a_post(community, user)
    edit_post(_api_input(url=typed), post, POST_TYPE_LINK, SRC_API, user=user)
    db.session.refresh(post)
    assert post.url == stored


def test_a_stored_image_link_sent_back_as_read_on_the_shop_is_kept(app, sites, http_mock):
    """A shop post whose image was stored on the first domain before C3, edited with its URL as the shop showed it: the same
    file of ours, so it is kept as stored, not deleted and re-made."""
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    stored = f'{FIRST_BASE}/static/media/posts/ab/cd/abcdefghijklmno.png'
    post = a_post(sites.wares, sites.sam)
    post.type, post.url = POST_TYPE_IMAGE, stored
    post.image = File(source_url=stored)
    db.session.commit()
    image_id = post.image_id
    edit_post(_api_input(url=stored.replace(FIRST_BASE, SHOP_BASE)), post, POST_TYPE_IMAGE, SRC_API, user=sites.sam)
    db.session.refresh(post)
    assert (post.url, post.image_id, post.image.source_url) == (stored, image_id, stored)


@pytest.mark.parametrize('where', ['first', 'shop'])
def test_a_communitys_icon_and_banner_from_the_api_are_stored_on_its_domain(app, sites, monkeypatch, where):
    community, base = (sites.pit, FIRST_BASE) if where == 'first' else (sites.wares, SHOP_BASE)
    make_community_member(sites.sam if where == 'shop' else sites.hal, community, is_moderator=True)
    monkeypatch.setattr(community_module, 'authorise_api_user',
                        lambda auth, return_type=None: sites.sam if where == 'shop' else sites.hal)
    monkeypatch.setattr(community_module, 'is_image_url', lambda url: True)
    monkeypatch.setattr(community_module, 'make_image_sizes', lambda *args: None)
    read_on = SHOP_BASE if where == 'first' else FIRST_BASE   # the client was shown it on the other storefront
    data = {'title': 'Wares', 'description': '', 'rules': '', 'nsfw': False, 'restricted_to_mods': False,
            'local_only': False, 'discussion_languages': [], 'question_answer': False,
            'icon_url': f'{read_on}/static/media/communities/ab/cd/icon.png',
            'banner_url': f'{read_on}/static/media/communities/ab/cd/banner.png'}
    edit_community(data, community, SRC_API, auth='token')
    assert community.icon.source_url == f'{base}/static/media/communities/ab/cd/icon.png'
    assert community.image.source_url == f'{base}/static/media/communities/ab/cd/banner.png'


def test_a_communitys_icon_stored_on_first_sent_back_as_read_on_the_shop_is_kept(app, sites, monkeypatch):
    community = sites.wares
    make_community_member(sites.sam, community, is_moderator=True)
    stored = [f'{FIRST_BASE}/static/media/communities/ab/cd/{name}.png' for name in ('icon', 'banner')]
    community.icon, community.image = File(source_url=stored[0]), File(source_url=stored[1])
    db.session.commit()
    ids = (community.icon_id, community.image_id)
    monkeypatch.setattr(community_module, 'authorise_api_user', lambda auth, return_type=None: sites.sam)
    data = {'title': 'Wares', 'description': '', 'rules': '', 'nsfw': False, 'restricted_to_mods': False,
            'local_only': False, 'discussion_languages': [], 'question_answer': False,
            'icon_url': stored[0].replace(FIRST_BASE, SHOP_BASE), 'banner_url': stored[1].replace(FIRST_BASE, SHOP_BASE)}
    edit_community(data, community, SRC_API, auth='token')
    assert (community.icon_id, community.image_id) == ids
    assert [community.icon.source_url, community.image.source_url] == stored


def test_a_stored_url_is_not_rewritten(app, sites):
    """A shop post whose image was stored on the first domain before C3 keeps that URL."""
    post = a_post(sites.wares, sites.sam)
    post.type = POST_TYPE_IMAGE
    post.image = File(source_url=f'{FIRST_BASE}/static/media/posts/ab/cd/old.png')
    db.session.commit()
    assert post_to_page(post)['attachment'][0]['url'] == f'{FIRST_BASE}/static/media/posts/ab/cd/old.png'


# --------------------------------------------------------------------------
# URLs of local files built when a document is serialised
# --------------------------------------------------------------------------

@pytest.mark.parametrize('where', ['first', 'shop'])
def test_a_posts_image_with_no_stored_url_is_named_on_the_posts_domain(app, sites, monkeypatch, where):
    community, author, base = (sites.pit, sites.hal, FIRST_BASE) if where == 'first' else \
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


@pytest.mark.parametrize('where', ['first', 'shop'])
def test_an_emojis_icon_is_named_on_the_posts_domain(app, sites, where):
    community, author, base = (sites.pit, sites.hal, FIRST_BASE) if where == 'first' else \
        (sites.wares, sites.sam, SHOP_BASE)
    db.session.add(Emoji(token=':gold:', url=f'{FIRST_BASE}/static/media/emoji/gold.png', instance_id=1))
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
        assert no_first(post_to_page(post)) and no_first(note)


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


@pytest.mark.parametrize('where', ['first', 'shop'])
def test_a_media_upload_and_its_thumbnail_are_on_the_uploaders_domain(app, channel, where):
    community, author, base = (channel.pit, channel.hal, FIRST_BASE) if where == 'first' else \
        (channel.wares, channel.sam, SHOP_BASE)
    if where == 'first':
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


def test_a_shop_media_post_on_first_storage_is_still_its_own(app, channel):
    """A shop post whose media was stored on the first domain's URL before C3 is still found on disk."""
    post = a_post(channel.wares, channel.sam)
    post.type = POST_TYPE_VIDEO
    assert store_uploaded_media(post, a_video(), 'video') is None
    post.url = post.url.replace(SHOP_BASE, FIRST_BASE)
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
        for host in (SHOP, FIRST):
            response = app.test_client().get('/static/media/posts/zq/zq/zqzqtask6probe.png', base_url=f'https://{host}')
            assert response.status_code == 200, host
            assert response.data[:8] == b'\x89PNG\r\n\x1a\n'
            response.close()
    finally:
        os.unlink(path)
        for empty in (directory, os.path.dirname(directory)):
            if not os.listdir(empty):
                os.rmdir(empty)


# --------------------------------------------------------------------------
# a local-only owner (the onion): its host is never stored (C1 I4, C2)
# --------------------------------------------------------------------------

@pytest.fixture
def onion_rows(sites, two_domains):
    """ollie, an onion account, and den, an onion community, beside the shop's and the first domain's."""
    ollie, den = two_domains.local_user('ollie', sites.onion), two_domains.local_community('den', sites.onion)
    ollie.private_key, ollie.public_key = sites.sam.private_key, sites.sam.public_key
    den.kind = COMMUNITY_KIND_VIDEO_CHANNEL
    db.session.commit()
    make_community_member(ollie, den, is_moderator=True)
    sites.ollie, sites.den = ollie, den
    return sites


def no_onion(text) -> bool:
    return ONION not in (text if isinstance(text, str) else json.dumps(text))


def test_an_onion_users_upload_is_stored_on_the_default_domain(app, onion_rows):
    url = process_upload(an_image(), user=onion_rows.ollie)
    assert re.fullmatch(rf'{DEFAULT_BASE}/static/media/posts/{NAME}\.\w+', url), url


@pytest.mark.parametrize('where', ['first', 'onion'])
def test_an_onion_users_post_and_its_image_name_no_onion_host(app, onion_rows, http_mock, where):
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    post = a_post(onion_rows.pit if where == 'first' else onion_rows.den, onion_rows.ollie)
    assert post.ap_id.startswith(f'{DEFAULT_BASE}/'), post.ap_id
    edit_post(_api_input(), post, POST_TYPE_IMAGE, SRC_API, user=onion_rows.ollie, uploaded_file=an_image())
    db.session.refresh(post)
    assert re.fullmatch(rf'{DEFAULT_BASE}/static/media/posts/{NAME}\.\w+', post.image.source_url)
    reply = a_reply(post, onion_rows.ollie)
    assert reply.ap_id == f'{DEFAULT_BASE}/comment/{reply.id}'
    if where == 'first':   # the author's own actor URL stays what C1 made it: an identity, not one of these
        assert no_onion(post.ap_id) and no_onion({key: value for key, value in post_to_page(post).items()
                                                   if key not in ('attributedTo', 'cc', 'to')})


def test_an_onion_users_pasted_upload_link_is_stored_on_the_default_domain(app, onion_rows, http_mock):
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    post = a_post(onion_rows.pit, onion_rows.ollie)
    edit_post(_api_input(url=f'http://{ONION}/static/media/posts/ab/cd/abcdefghijklmno.png'), post, POST_TYPE_IMAGE,
              SRC_API, user=onion_rows.ollie)
    db.session.refresh(post)
    assert post.url == post.image.source_url == f'{DEFAULT_BASE}/static/media/posts/ab/cd/abcdefghijklmno.png'


def test_an_onion_communitys_icon_from_the_api_is_stored_on_the_default_domain(app, onion_rows, monkeypatch):
    monkeypatch.setattr(community_module, 'authorise_api_user', lambda auth, return_type=None: onion_rows.ollie)
    monkeypatch.setattr(community_module, 'is_image_url', lambda url: True)
    monkeypatch.setattr(community_module, 'make_image_sizes', lambda *args: None)
    data = {'title': 'Den', 'description': '', 'rules': '', 'nsfw': False, 'restricted_to_mods': False,
            'local_only': True, 'discussion_languages': [], 'question_answer': False,
            'icon_url': f'http://{ONION}/static/media/communities/ab/cd/icon.png', 'banner_url': None}
    edit_community(data, onion_rows.den, SRC_API, auth='token')
    assert onion_rows.den.icon.source_url == f'{DEFAULT_BASE}/static/media/communities/ab/cd/icon.png'


@pytest.mark.parametrize('who, base', [('ollie', DEFAULT_BASE), ('sam', SHOP_BASE), ('hal', FIRST_BASE)])
def test_an_api_avatar_and_cover_are_stored_on_the_accounts_federating_domain(app, onion_rows, monkeypatch, who, base):
    user = getattr(onion_rows, who)
    monkeypatch.setattr(user_utils_module, 'authorise_api_user', lambda auth, return_type=None: user)
    monkeypatch.setattr(user_utils_module, 'make_image_sizes', lambda *args: None)
    read_on = f'http://{ONION}'   # the client was shown them on the onion
    put_user_save_user_settings('token', {'avatar': f'{read_on}/static/media/users/ab/cd/avatar.png',
                                          'cover': f'{read_on}/static/media/users/ab/cd/cover.png'})
    db.session.refresh(user)
    assert (user.avatar.source_url, user.cover.source_url) == \
        (f'{base}/static/media/users/ab/cd/avatar.png', f'{base}/static/media/users/ab/cd/cover.png')


def test_an_onion_media_upload_and_its_thumbnail_are_on_the_default_domain(app, onion_rows, monkeypatch):
    set_setting('allow_video_file_uploads', 'users')
    for name in ('task_selector', 'notify_about_post', 'make_image_sizes'):
        monkeypatch.setattr(media_tasks, name, lambda *args, **kwargs: None)
    post = a_post(onion_rows.den, onion_rows.ollie)
    post.type = POST_TYPE_VIDEO
    assert store_uploaded_media(post, a_video(), 'video') is None
    db.session.commit()
    assert re.fullmatch(rf'{DEFAULT_BASE}/static/media/posts/{NAME}\.mp4', post.url), post.url
    post_id = post.id
    probe_media(False, post_id)
    db.session.expire_all()
    post = db.session.get(Post, post_id)
    assert media_of(post)['status'] == 'ready'
    assert re.fullmatch(rf'{DEFAULT_BASE}/static/media/posts/{NAME}\.jpg', post.image.source_url)


@pytest.mark.parametrize('signed_in', [False, True])
def test_first_domains_api_and_rss_name_no_onion_host_for_an_onion_users_content(app, onion_rows, http_mock, monkeypatch,
                                                                         signed_in):
    http_mock.head(url__regex=r'.*').respond(200, headers={'Content-Type': 'image/png'})
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    post = a_post(onion_rows.pit, onion_rows.ollie)
    edit_post(_api_input(), post, POST_TYPE_IMAGE, SRC_API, user=onion_rows.ollie, uploaded_file=an_image())
    ollie = onion_rows.ollie
    ollie.avatar = File(source_url=process_upload(an_image(), destination='users', user=ollie))
    db.session.commit()
    client = app.test_client()
    headers = {'Authorization': f'Bearer {onion_rows.hal.encode_jwt_token()}'} if signed_in else {}
    if signed_in:
        with client.session_transaction() as session:
            session['_user_id'], session['_fresh'] = str(onion_rows.hal.id), True

    def served(path):
        response = client.get(path, headers=headers, base_url=f'https://{FIRST}')
        assert response.status_code == 200, path
        return response

    view = served(f'/api/alpha/post?id={post.id}').get_json()['post_view']
    media = [view['post'].get(field) for field in ('url', 'thumbnail_url', 'small_thumbnail_url')] + \
        [view['creator'].get('avatar')]
    assert all(url and no_onion(url) for url in media), media
    profile = client.get(f'/api/alpha/user?person_id={ollie.id}', headers=headers, base_url=f'https://{FIRST}')
    if profile.status_code == 200:   # signed in: shown; out: the onion's account is not the first domain's to show (400)
        person = profile.get_json()['person_view']['person']
        assert person['avatar'] and no_onion(person['avatar']), person
    else:
        assert profile.status_code == 400
    db.session.get(Site, 1).private_instance = False   # an RSS reader reads a public instance's feeds
    db.session.commit()
    rss = served('/community/pit/feed').get_data(as_text=True)
    # no URL in it names the onion (the author's handle, `ollie@<onion>`, is C1's identity, not a URL of ours)
    assert '/static/media/posts/' in rss and f'//{ONION}' not in rss


# --------------------------------------------------------------------------
# links in text: only a local-only host is rewritten, onto the author's federating base (ruling 3)
# --------------------------------------------------------------------------

def test_canonicalisation_rewrites_only_a_local_only_host(app, onion_rows):
    text = f'see {SHOP_BASE}/post/5, {FIRST_BASE}/post/6 and http://{ONION}/post/7'
    html = f'<p><a href="{SHOP_BASE}/post/5">a</a><a href="{FIRST_BASE}/post/6">b</a><a href="http://{ONION}/post/7">c</a></p>'
    for base in (SHOP_BASE, DEFAULT_BASE):
        assert canonicalise_text(text, base) == f'see {SHOP_BASE}/post/5, {FIRST_BASE}/post/6 and {base}/post/7'
        assert canonicalise_links(html, base) == html.replace(f'http://{ONION}', base)
    assert canonicalise_text(text) == canonicalise_text(text, DEFAULT_BASE)   # no owner known: the default's


@pytest.mark.parametrize('author, base', [('sam', SHOP_BASE), ('hal', FIRST_BASE), ('ollie', DEFAULT_BASE)])
def test_a_body_is_stored_with_its_onion_links_on_the_authors_federating_base(app, onion_rows, author, base):
    user = getattr(onion_rows, author)
    post = a_post(onion_rows.wares if author == 'sam' else onion_rows.pit, user)
    body = f'mine {SHOP_BASE}/post/5 theirs {FIRST_BASE}/post/6 hidden http://{ONION}/post/7'
    edit_post(_api_input(body=body), post, POST_TYPE_ARTICLE, SRC_API, user=user)
    db.session.refresh(post)
    assert post.body == f'mine {SHOP_BASE}/post/5 theirs {FIRST_BASE}/post/6 hidden {base}/post/7'
    assert f'href="{SHOP_BASE}/post/5"' in post.body_html and f'href="{FIRST_BASE}/post/6"' in post.body_html
    assert f'href="{base}/post/7"' in post.body_html and no_onion(post.body_html)


def test_a_reply_and_its_edit_are_stored_on_the_authors_federating_base(app, onion_rows, monkeypatch):
    monkeypatch.setattr(reply_module, 'authorise_api_user', lambda auth, **kwargs: onion_rows.sam)
    monkeypatch.setattr(reply_module, 'task_selector', lambda *args, **kwargs: None)
    post = a_post(onion_rows.wares, onion_rows.sam)
    body = f'see {FIRST_BASE}/post/6 and http://{ONION}/post/7'
    _, reply = make_reply({'body': body, 'notify_author': False, 'language_id': None}, post, None, SRC_API, auth='token')
    expected = f'see {FIRST_BASE}/post/6 and {SHOP_BASE}/post/7'
    assert reply.body == expected and f'href="{SHOP_BASE}/post/7"' in reply.body_html and no_onion(reply.body_html)
    edit_reply({'body': body + '!', 'notify_author': False, 'language_id': None, 'distinguished': False}, reply, post,
               SRC_API, auth='token')
    assert reply.body == expected + '!' and no_onion(reply.body_html)


def test_a_communitys_description_and_rules_and_an_accounts_about_are_on_their_base(app, onion_rows, monkeypatch):
    make_community_member(onion_rows.sam, onion_rows.wares, is_moderator=True)
    monkeypatch.setattr(community_module, 'authorise_api_user', lambda auth, return_type=None: onion_rows.sam)
    typed = f'see http://{ONION}/post/7 and {FIRST_BASE}/post/6'
    data = {'title': 'Wares', 'description': typed, 'rules': typed, 'nsfw': False, 'restricted_to_mods': False,
            'local_only': False, 'discussion_languages': [], 'question_answer': False, 'icon_url': None,
            'banner_url': None}
    edit_community(data, onion_rows.wares, SRC_API, auth='token')
    expected = f'see {SHOP_BASE}/post/7 and {FIRST_BASE}/post/6'
    assert (onion_rows.wares.description, onion_rows.wares.rules) == (expected, expected)
    assert no_onion(onion_rows.wares.description_html)
    monkeypatch.setattr(user_utils_module, 'authorise_api_user', lambda auth, return_type=None: onion_rows.sam)
    put_user_save_user_settings('token', {'bio': typed})
    assert onion_rows.sam.about == expected and no_onion(onion_rows.sam.about_html)


def test_an_onion_senders_message_id_is_ours_and_opens_its_conversation(app, onion_rows, monkeypatch):
    """The id is stored on the default base (a local-only domain's host is never stored); the route must still know
    it as ours, so its reader is sent to the conversation rather than told it does not exist."""
    conversation = Conversation(user_id=onion_rows.ollie.id)
    conversation.members.append(onion_rows.ollie)
    conversation.members.append(onion_rows.hal)
    db.session.add(conversation)
    db.session.commit()
    captured(monkeypatch, chat_util)
    message = chat_util.send_message('hello', conversation.id, user=onion_rows.ollie)
    assert message.ap_id == f'{DEFAULT_BASE}/private_message/{message.id}'
    client = app.test_client()
    with client.session_transaction(base_url=f'http://{ONION}') as session:
        session['_user_id'], session['_fresh'] = str(onion_rows.hal.id), True
    # on the onion: off it a conversation with an onion account answers as one nobody holds (follow-up F6)
    response = client.get(f'/private_message/{message.id}', base_url=f'http://{ONION}')
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/chat/{conversation.id}')


# --------------------------------------------------------------------------
# the rest of the stored texts: a local-only host's links go onto the owner's federating base (ruling 3, M-a)
# --------------------------------------------------------------------------

ONION_LINK = f'http://{ONION}/post/7'


def signed_in(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'], session['_fresh'] = str(user.id), True
    return client, csrf(app, client)


def test_an_inline_web_reply_is_stored_on_its_authors_base(app, onion_rows, monkeypatch):
    monkeypatch.setattr('app.post.routes.task_selector', lambda *args, **kwargs: None)
    post = a_post(onion_rows.wares, onion_rows.sam)
    parent = a_reply(post, onion_rows.sam)
    client, token = signed_in(app, onion_rows.sam)
    response = client.post(f'/post/{post.id}/comment/{parent.id}/reply_inline/abc',
                           data={'body': f'see {ONION_LINK}', 'csrf_token': token})
    assert response.status_code == 200, response.headers.get('Location')
    made = PostReply.query.filter_by(parent_id=parent.id).one()
    assert made.body == f'see {SHOP_BASE}/post/7' and no_onion(made.body_html)


def test_a_private_message_and_its_api_edit_are_stored_on_the_senders_base(app, onion_rows, monkeypatch):
    conversation = Conversation(user_id=onion_rows.sam.id)
    conversation.members.append(onion_rows.sam)
    conversation.members.append(onion_rows.hal)
    db.session.add(conversation)
    db.session.commit()
    captured(monkeypatch, chat_util)
    message = chat_util.send_message(f'see {ONION_LINK}', conversation.id, user=onion_rows.sam)
    assert message.body == f'see {SHOP_BASE}/post/7' and no_onion(message.body_html)
    monkeypatch.setattr(private_message_module, 'authorise_api_user', lambda auth, **kwargs: onion_rows.sam.id)
    put_private_message('token', {'private_message_id': message.id, 'content': f'now {ONION_LINK}'})
    db.session.refresh(message)
    assert message.body == f'now {SHOP_BASE}/post/7' and no_onion(message.body_html)


def test_an_admins_edit_of_a_shop_communitys_texts_is_on_the_shop(app, onion_rows):
    db.session.add(Language(code='und', name='Undetermined'))
    grant_permission(onion_rows.hal, 'administer all communities')
    db.session.commit()
    client, token = signed_in(app, onion_rows.hal)
    data = {'csrf_token': token, 'url': 'wares', 'title': 'Wares', 'description': f'see {ONION_LINK}',
            'rules': f'see {ONION_LINK}', 'content_retention': -1, 'topic': -1, 'default_layout': '',
            'posting_warning': '', 'downvote_accept_mode': 0}
    assert client.post(f'/admin/community/{onion_rows.wares.id}/edit', data=data).status_code == 302
    db.session.refresh(onion_rows.wares)
    assert onion_rows.wares.description == onion_rows.wares.rules == f'see {SHOP_BASE}/post/7'
    assert no_onion(onion_rows.wares.description_html)


def test_an_account_an_admin_adds_on_the_shop_has_its_about_on_the_shop(app, onion_rows):
    _roles()
    grant_permission(onion_rows.hal, 'administer all users')
    client, token = signed_in(app, onion_rows.hal)
    with patch('app.admin.routes.render_template', return_value='rendered'), \
            patch('app.admin.routes.finalize_user_setup'):
        response = client.post('/admin/users/add', content_type='multipart/form-data',
                               data=_add_payload(token, about=f'see {ONION_LINK}', site_domain=str(onion_rows.shop.id)))
    assert response.status_code == 302
    added = User.query.filter_by(user_name='newbie').one()
    assert added.about == f'see {SHOP_BASE}/post/7' and no_onion(added.about_html)


def test_a_community_made_on_the_shop_and_its_wiki_are_on_the_shop(app, onion_rows, monkeypatch):
    sam = able_to_create(onion_rows.sam)
    db.session.add_all([Language(code='und', name='Undetermined'), Language(code='en', name='English')])
    db.session.get(Site, 1).community_creation_admin_only = False
    db.session.commit()
    client, token = signed_in(app, sam)
    with patch('app.community.routes.render_template', return_value='rendered'), \
            patch('app.community.routes.task_selector'):
        response = client.post('/community/add_local', content_type='multipart/form-data',
                               data=_community_payload(token, url='stall', description=f'see {ONION_LINK}'))
        assert response.status_code == 302, response.status_code
        assert Community.query.filter_by(name='stall').first(), response.headers.get('Location')
        stall = Community.query.filter_by(name='stall').one()
        assert stall.description == f'see {SHOP_BASE}/post/7' and no_onion(stall.description_html)
        wiki = {'title': 'Hours', 'slug': 'hours', 'body': f'see {ONION_LINK}', 'who_can_edit': '0',
                'submit': 'Save', 'csrf_token': token}
        client.post(f'/community/stall@{SHOP}/moderate/wiki/add', data=wiki)
        page = CommunityWikiPage.query.filter_by(community_id=stall.id).one()
        assert page.body == f'see {SHOP_BASE}/post/7' and no_onion(page.body_html)
        client.post(f'/community/stall@{SHOP}/moderate/wiki/{page.id}/edit', data={**wiki, 'body': f'now {ONION_LINK}'})
    db.session.refresh(page)
    assert page.body == f'now {SHOP_BASE}/post/7' and no_onion(page.body_html)
