"""Album posts through the alpha API (Pixelfed interop R5): `gallery`, the pictures after the post's image `url`.

An app uploads each picture through /upload/image and then names them: `url` is the first, `gallery` the rest, each
`{url, alt_text}`. On edit, no `gallery` key keeps the album, `[]` clears it, and a list replaces it.
"""
from unittest.mock import patch

import pytest
from marshmallow import ValidationError

from app import db
from app.api.alpha.schema import CreatePostRequest, EditPostRequest
from app.api.alpha.utils.post import post_post, put_post
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
    return [(file.source_url, file.alt_text) for file in post.album_images()]


def _create(env, **extra):
    data = {'title': 'an album', 'community_id': env.post.community_id, 'url': f'{OURS}/0.png', 'alt_text': 'zero'}
    data.update(extra)
    return post_post(token(env.author), data)['post_view']['post']['id']


def test_a_post_is_created_with_its_album(env, images):
    post_id = _create(env, gallery=[{'url': f'{OURS}/1.png', 'alt_text': 'one'}, {'url': f'{OURS}/2.png'}])

    assert _pictures(post_id) == [(f'{OURS}/0.png', 'zero'), (f'{OURS}/1.png', 'one'), (f'{OURS}/2.png', None)]


def test_the_post_view_lists_the_album(env, images):
    """The API already serializes an inbound album (extensions.gallery); a local one shows the same way."""
    res = post_post(token(env.author), {'title': 'an album', 'community_id': env.post.community_id,
                                        'url': f'{OURS}/0.png', 'gallery': [{'url': f'{OURS}/1.png', 'alt_text': 'one'}]})

    gallery = res['post_view']['post']['extensions']['gallery']
    assert [(image['url'], image['alt_text']) for image in gallery] == [(f'{OURS}/1.png', 'one')]


def test_an_edit_without_gallery_keeps_the_album(env, images):
    post_id = _create(env, gallery=[{'url': f'{OURS}/1.png', 'alt_text': 'one'}])

    put_post(token(env.author), {'post_id': post_id, 'title': 'renamed'})

    assert _pictures(post_id)[1:] == [(f'{OURS}/1.png', 'one')]


def test_an_edit_replaces_or_clears_the_album(env, images):
    post_id = _create(env, gallery=[{'url': f'{OURS}/1.png', 'alt_text': 'one'}])

    put_post(token(env.author), {'post_id': post_id, 'gallery': [{'url': f'{OURS}/2.png', 'alt_text': 'two'},
                                                                 {'url': f'{OURS}/1.png', 'alt_text': 'one!'}]})
    assert _pictures(post_id)[1:] == [(f'{OURS}/2.png', 'two'), (f'{OURS}/1.png', 'one!')]

    put_post(token(env.author), {'post_id': post_id, 'gallery': []})
    assert _pictures(post_id)[1:] == []


def test_an_album_over_the_limit_is_refused(env, images):
    set_setting('max_album_images', 2)

    with pytest.raises(Exception, match='At most 2 images'):
        _create(env, gallery=[{'url': f'{OURS}/1.png'}, {'url': f'{OURS}/2.png'}])


def test_an_album_url_must_be_http(env, images):
    """D1407's rule for `url`, held for each album picture: a template renders them as hrefs too."""
    with pytest.raises(Exception, match='must be an http'):
        _create(env, gallery=[{'url': 'javascript:alert(1)'}])


def test_an_album_without_an_image_is_refused(env, images):
    """An album is the pictures after an image post's image: on a text post it would be dropped unseen."""
    with pytest.raises(Exception, match='needs an image'):
        _create(env, url=None, gallery=[{'url': f'{OURS}/1.png'}])
    assert Post.query.filter_by(title='an album').count() == 0   # make_post removes the post it began


def test_the_schemas_take_a_gallery(app):
    entry = {'url': f'{OURS}/1.png', 'alt_text': None}
    assert CreatePostRequest().load({'title': 't', 'community_id': 1, 'gallery': [entry]})['gallery'] == [entry]
    assert EditPostRequest().load({'post_id': 1, 'gallery': []})['gallery'] == []
    with pytest.raises(ValidationError):
        CreatePostRequest().load({'title': 't', 'community_id': 1, 'gallery': [{'alt_text': 'no url'}]})
