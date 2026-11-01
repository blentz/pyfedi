"""Gallery posts through the alpha API (spec 2026-11-01 G-5, Pixelfed interop R5): `gallery` is every picture.

An app uploads each picture through /upload/image and then names them in `gallery`, each `{url, alt_text}`: two to
the site's max_album_images. Given, the post is a gallery and `url` is not used. On edit, no `gallery` key keeps the
pictures and a list replaces them.
"""
import pytest
from marshmallow import ValidationError

from app import db
from app.api.alpha.schema import CreatePostRequest, EditPostRequest
from app.api.alpha.utils.post import post_post, put_post
from app.constants import POST_TYPE_GALLERY
from app.models import Post
from app.utils import set_setting
from tests.test_api_post_actions import env, token  # noqa: F401 (env)

OURS = 'https://test.piefed.local/static/media/posts/ab/cd'


@pytest.fixture
def images(monkeypatch):
    """Every url is an image, and sizing one is a no-op: neither is what is under test."""
    monkeypatch.setattr('app.shared.post.is_image_url', lambda url: True)
    monkeypatch.setattr('app.shared.post.make_image_sizes', lambda *a, **k: None)
    monkeypatch.setattr('app.shared.post.opengraph_parse', lambda url: None)


def _pictures(post_id):
    post = db.session.get(Post, post_id)
    return [(file.source_url, file.alt_text) for file in post.gallery_pictures()]


@pytest.fixture
def composite(monkeypatch):
    """The composite thumbnail fetches every picture: a stand-in that only names it."""
    from app.models import File

    def build(urls, post, return_file_id=True):
        thumbnail = File(source_url=f'{OURS}/composite.webp')
        db.session.add(thumbnail)
        db.session.flush()
        return thumbnail.id
    monkeypatch.setattr('app.shared.post.build_gallery_thumbnail', build)


def _create(env, **extra):
    data = {'title': 'a gallery', 'community_id': env.post.community_id}
    data.update(extra)
    return post_post(token(env.author), data)['post_view']['post']['id']


def _two():
    return [{'url': f'{OURS}/1.png', 'alt_text': 'one'}, {'url': f'{OURS}/2.png'}]


def test_a_gallery_post_is_created_with_its_pictures(env, images, composite):
    post_id = _create(env, gallery=_two())

    post = db.session.get(Post, post_id)
    assert post.type == POST_TYPE_GALLERY
    assert _pictures(post_id) == [(f'{OURS}/1.png', 'one'), (f'{OURS}/2.png', None)]
    assert post.image.source_url == f'{OURS}/composite.webp' and post.url.endswith(post.slug)


def test_a_url_beside_the_gallery_is_not_used(env, images, composite):
    post_id = _create(env, url=f'{OURS}/0.png', gallery=_two())

    assert [url for url, _alt in _pictures(post_id)] == [f'{OURS}/1.png', f'{OURS}/2.png']


def test_the_post_view_lists_every_picture(env, images, composite):
    res = post_post(token(env.author), {'title': 'a gallery', 'community_id': env.post.community_id,
                                        'gallery': _two()})

    post = res['post_view']['post']
    assert [(image['url'], image['alt_text']) for image in post['extensions']['gallery']] == \
        [(f'{OURS}/1.png', 'one'), (f'{OURS}/2.png', None)]


def test_an_edit_without_gallery_keeps_the_pictures(env, images, composite):
    post_id = _create(env, gallery=_two())

    put_post(token(env.author), {'post_id': post_id, 'title': 'renamed'})

    assert db.session.get(Post, post_id).type == POST_TYPE_GALLERY
    assert _pictures(post_id) == [(f'{OURS}/1.png', 'one'), (f'{OURS}/2.png', None)]


def test_an_edit_replaces_the_pictures(env, images, composite):
    post_id = _create(env, gallery=_two())

    put_post(token(env.author), {'post_id': post_id, 'gallery': [{'url': f'{OURS}/2.png', 'alt_text': 'two'},
                                                                 {'url': f'{OURS}/1.png', 'alt_text': 'one!'}]})

    assert _pictures(post_id) == [(f'{OURS}/2.png', 'two'), (f'{OURS}/1.png', 'one!')]


def test_an_edit_to_fewer_than_two_pictures_is_refused(env, images, composite):
    post_id = _create(env, gallery=_two())

    with pytest.raises(Exception, match='A gallery has 2 to'):
        put_post(token(env.author), {'post_id': post_id, 'gallery': []})


def test_a_gallery_over_the_limit_is_refused(env, images, composite):
    set_setting('max_album_images', 2)

    with pytest.raises(Exception, match='At most 2 images'):
        _create(env, gallery=_two() + [{'url': f'{OURS}/3.png'}])


def test_a_gallery_url_must_be_http(env, images, composite):
    """D1407's rule for `url`, held for each picture: a template renders them as hrefs too."""
    with pytest.raises(Exception, match='must be an http'):
        _create(env, gallery=[{'url': 'javascript:alert(1)'}, {'url': f'{OURS}/2.png'}])


def test_one_picture_is_not_a_gallery(env, images, composite):
    """A gallery post needs two pictures; one is an image post, made with `url`."""
    with pytest.raises(Exception, match='A gallery has 2 to'):
        _create(env, gallery=[{'url': f'{OURS}/1.png'}])
    assert Post.query.filter_by(title='a gallery').count() == 0   # make_post removes the post it began


def test_the_schemas_take_a_gallery(app):
    entry = {'url': f'{OURS}/1.png', 'alt_text': None}
    assert CreatePostRequest().load({'title': 't', 'community_id': 1, 'gallery': [entry]})['gallery'] == [entry]
    assert EditPostRequest().load({'post_id': 1, 'gallery': []})['gallery'] == []
    with pytest.raises(ValidationError):
        CreatePostRequest().load({'title': 't', 'community_id': 1, 'gallery': [{'alt_text': 'no url'}]})
