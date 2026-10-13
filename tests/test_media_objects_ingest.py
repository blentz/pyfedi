"""C4: Post.new maps Audio, Image, Document and Video objects onto post types."""
import contextlib

import pytest
from bs4 import BeautifulSoup

from app import db
from app.activitypub.util import update_post_from_activity
from app.constants import POST_TYPE_IMAGE, POST_TYPE_LINK, POST_TYPE_VIDEO
from app.models import Post
from app.utils import utcnow
from tests.factories import make_community, make_community_member, make_instance, make_site, make_user

PEER = 'remote.test'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
AUDIO = 'https://tunes.example/api/v1/listen/abc/?upload=1'


@pytest.fixture
def world(app, db_session):
    make_site()
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'mediamaker')
    author.ap_profile_id = author.ap_public_url = f'https://{PEER}/u/mediamaker'
    make_community_member(author, community)
    db.session.commit()
    return community, author


def _create(obj):
    return {'id': obj['id'] + '/create', 'type': 'Create', 'to': [PUBLIC], 'object': obj}


def _media(n, kind, **fields):
    obj = {'id': f'https://{PEER}/m/{n}', 'type': kind, 'attributedTo': f'https://{PEER}/u/mediamaker',
           'to': [PUBLIC], 'cc': [], 'published': '2026-01-01T00:00:00Z'}
    obj.update(fields)
    return obj


def test_funkwhale_audio_becomes_a_link_post_with_the_marker(world):
    community, author = world
    obj = _media(1, 'Audio', name='A song', content='<p>liner notes</p>',
                 url=[{'type': 'Link', 'href': 'https://tunes.example/library/tracks/1', 'mediaType': 'text/html'},
                      {'type': 'Link', 'href': AUDIO, 'mediaType': 'audio/ogg'}])
    post = Post.new(author, community, _create(obj))
    assert (post.type, post.url, post.title) == (POST_TYPE_LINK, AUDIO, 'A song')
    assert post.extensions == {'media_type': 'audio'}
    assert 'liner notes' in post.body_html


def test_image_without_an_extension_is_an_image_post_with_alt_text(world):
    community, author = world
    post = Post.new(author, community, _create(_media(2, 'Image', name='Sunset', url='https://pics.example/media/9')))
    assert post.type == POST_TYPE_IMAGE and post.url == 'https://pics.example/media/9'
    assert post.image is not None and post.image.source_url == 'https://pics.example/media/9'
    assert post.image.alt_text == 'Sunset'


def test_document_is_a_link_post(world):
    community, author = world
    post = Post.new(author, community, _create(_media(3, 'Document', name='Paper',
                                                      url='https://docs.example/paper.pdf')))
    assert (post.type, post.url) == (POST_TYPE_LINK, 'https://docs.example/paper.pdf')
    assert not (post.extensions or {}).get('media_type')


def test_a_bare_media_object_is_titled_by_its_type(world):
    community, author = world
    assert Post.new(author, community, _create(_media(4, 'Document', url='https://docs.example/a.pdf'))).title \
        == 'Document'


def test_a_media_object_without_a_name_takes_the_body_title(world):
    community, author = world
    post = Post.new(author, community, _create(_media(5, 'Image', url='https://pics.example/1.png',
                                                      content='<p>Look at this. More text.</p>')))
    assert post.title.startswith('Look at this')
    assert post.url == 'https://pics.example/1.png'  # the media URL, not a link from the content


def test_a_media_object_whose_body_gives_no_title_is_titled_by_its_type(world):
    community, author = world
    post = Post.new(author, community, _create(_media(6, 'Audio', url=AUDIO, content='<p></p>')))
    assert post.title == 'Audio'


def test_no_usable_url_creates_nothing(world):
    community, author = world
    assert Post.new(author, community, _create(_media(7, 'Image', name='x', url='javascript:alert(1)'))) is None


def test_peertube_video_is_unchanged(world):
    community, author = world
    post = Post.new(author, community, _create(_media(8, 'Video', name='Clip', content='<p>desc</p>',
                                                      mediaType='text/markdown')))
    assert post.type == POST_TYPE_VIDEO and post.url == f'https://{PEER}/m/8'


def test_audio_ingest_fetches_nothing(world, monkeypatch):
    community, author = world
    calls = []
    monkeypatch.setattr('app.models.opengraph_parse', lambda *a, **k: calls.append(a), raising=False)
    monkeypatch.setattr('app.utils.opengraph_parse', lambda *a, **k: calls.append(a))
    Post.new(author, community, _create(_media(9, 'Audio', name='s', url=AUDIO)))
    Post.new(author, community, _create(_media(10, 'Document', name='d', url='https://docs.example/b.pdf')))
    assert calls == []


class _RedisLockOnlyDouble:
    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


def _stored(world, n, kind, **fields):
    community, author = world
    post = Post.new(author, community, _create(_media(n, kind, **fields)))
    db.session.add(post)
    db.session.commit()
    return post


def _update(post, n, kind, **fields):
    update_post_from_activity(post, {'id': f'u{n}', 'type': 'Update', 'object': _media(n, kind, **fields)})


def test_an_update_changes_the_audio_url_and_title(world, redis_lock_only_double):
    post = _stored(world, 20, 'Audio', name='Old', url=AUDIO)
    _update(post, 20, 'Audio', name='New', url=[{'href': 'https://tunes.example/2.ogg', 'mediaType': 'audio/ogg'}])
    assert (post.title, post.url, post.type) == ('New', 'https://tunes.example/2.ogg', POST_TYPE_LINK)
    assert post.extensions == {'media_type': 'audio'}


def test_an_update_without_a_usable_url_keeps_the_stored_one(world, redis_lock_only_double):
    post = _stored(world, 21, 'Document', name='Doc', url='https://docs.example/1.pdf')
    _update(post, 21, 'Document', name='Doc', url='javascript:alert(1)')
    assert post.url == 'https://docs.example/1.pdf'


def test_an_image_update_without_an_extension_stays_an_image_post(world, redis_lock_only_double):
    post = _stored(world, 22, 'Image', name='One', url='https://pics.example/media/1')
    _update(post, 22, 'Image', name='Two', url='https://pics.example/media/2')
    assert post.type == POST_TYPE_IMAGE and post.url == 'https://pics.example/media/2'
    assert post.image.source_url == 'https://pics.example/media/2'


def test_a_bare_media_update_is_titled_by_its_type(world, redis_lock_only_double):
    post = _stored(world, 23, 'Audio', name='Song', url=AUDIO)
    _update(post, 23, 'Audio', url=AUDIO)
    assert post.title == 'Audio'


def test_a_media_update_fetches_nothing(world, redis_lock_only_double, monkeypatch):
    post = _stored(world, 24, 'Document', name='d', url='https://docs.example/1.pdf')
    calls = []
    monkeypatch.setattr('app.activitypub.util.opengraph_parse', lambda *a, **k: calls.append(a))
    _update(post, 24, 'Document', name='d', url='https://docs.example/2.pdf')
    assert calls == [] and post.url == 'https://docs.example/2.pdf'


def test_a_nameless_media_update_with_a_body_link_and_no_usable_url_keeps_the_media_url(world, redis_lock_only_double):
    post = _stored(world, 25, 'Audio', name='Song', url=AUDIO)
    _update(post, 25, 'Audio', url='javascript:alert(1)', content='<h1><a href="https://elsewhere.example/x">see this one</a></h1>')
    assert post.url == AUDIO


def test_an_image_update_with_the_same_url_keeps_the_image_row_and_a_new_url_replaces_it(world, redis_lock_only_double):
    post = _stored(world, 26, 'Image', name='One', url='https://pics.example/media/1')
    first = post.image_id
    _update(post, 26, 'Image', name='Two', url='https://pics.example/media/1')
    assert post.image_id == first
    _update(post, 26, 'Image', name='Two', url='https://pics.example/media/2')
    assert post.image_id != first and post.image.source_url == 'https://pics.example/media/2'


def _signed_in(app, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)  # the post page's reply form needs csrf_token
    viewer = make_user(make_instance(app.config['SERVER_NAME'], software='piefed'), 'viewer', local=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(viewer.id)
        sess['_fresh'] = True
    return client


def test_the_post_page_plays_an_audio_post_without_an_audio_extension(world, app, monkeypatch):
    post = _stored(world, 30, 'Audio', name='Song', url=AUDIO)
    html = _signed_in(app, monkeypatch).get(f'/post/{post.id}').get_data(as_text=True)
    audio = BeautifulSoup(html, 'html.parser').select_one('audio')
    assert audio is not None and audio['src'] == AUDIO


def test_a_plain_link_without_an_audio_extension_has_no_player(world, app, monkeypatch):
    post = _stored(world, 31, 'Document', name='Doc', url='https://docs.example/1.pdf')
    html = _signed_in(app, monkeypatch).get(f'/post/{post.id}').get_data(as_text=True)
    assert BeautifulSoup(html, 'html.parser').select_one('audio') is None


@pytest.mark.parametrize('blank', ['', '   ', ' \n\t '])
def test_a_blank_name_on_a_media_object_is_no_name_in_create(world, blank):
    community, author = world
    with_body = Post.new(author, community, _create(_media(40, 'Image', name=blank, url='https://pics.example/1.png',
                                                           content='<p>Look at this. More text.</p>')))
    assert with_body.title.startswith('Look at this')
    bare = Post.new(author, community, _create(_media(41, 'Document', name=blank, url='https://docs.example/a.pdf')))
    assert bare.title == 'Document'


@pytest.mark.parametrize('blank', ['', '   '])
def test_a_blank_name_on_a_media_object_is_no_name_in_update(world, redis_lock_only_double, blank):
    post = _stored(world, 42, 'Audio', name='Song', url=AUDIO)
    _update(post, 42, 'Audio', name=blank, url=AUDIO, content='<p>Look at this. More text.</p>')
    assert 'Look at this' in post.title and post.title.strip() != ''
    bare = _stored(world, 43, 'Audio', name='Song', url=AUDIO)
    _update(bare, 43, 'Audio', name=blank, url=AUDIO)
    assert bare.title == 'Audio'
