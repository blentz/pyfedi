"""C4: Post.new maps Audio, Image, Document and Video objects onto post types."""
import pytest

from app import db
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
