"""Album posts through the web forms (Pixelfed interop R5, R7).

The image post form offers one slot per picture the site allows after the main image (`max_album_images() - 1`), each a
file (`album_files-N`) and its alt text (`album_alts-N`). Each picture is checked as the main image is (R207's 10 MB) and
stored as it is (store_post_image), so its File names the file written and deleting the post removes it. The edit page
lists the album: each picture's alt text (`album_alt_<file id>`) and a box to remove it (`album_remove_<file id>`), then
empty slots for more.
"""
import io
import os

import pytest
from PIL import Image

from app import db
from app.constants import POST_TYPE_IMAGE
from app.models import Post, Site
from app.utils import set_setting
from tests.test_media_upload import env, submit, submit_url  # noqa: F401 (env)

pytestmark = pytest.mark.usefixtures('site', 'private_static_tree')


@pytest.fixture
def images_allowed(env, monkeypatch):
    db.session.get(Site, 1).allow_local_image_posts = True
    db.session.commit()
    monkeypatch.setattr('app.shared.post.make_image_sizes', lambda *a, **k: None)   # resizing is not under test
    monkeypatch.setattr('app.shared.post.is_image_url', lambda url: True)   # a HEAD of our own upload's url
    monkeypatch.setattr('app.utils.is_image_url', lambda url: True)   # ...and post_edit's is_local_image_url's
    return env


def png(colour=(1, 2, 3), size=(10, 10)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', size, colour).save(buffer, format='PNG')
    return buffer.getvalue()


def album_fields(*pictures):
    fields = {}
    for n, (payload, alt) in enumerate(pictures):
        fields[f'album_files-{n}'] = (io.BytesIO(payload), f'p{n}.png')
        fields[f'album_alts-{n}'] = alt
    return fields


def the_post():
    return Post.query.filter_by(title='A clip').one_or_none()


def test_the_form_offers_a_slot_per_picture_the_site_allows(images_allowed):
    env = images_allowed
    client, _ = env.client_for(env.stranger)
    page = client.get(submit_url(env.app, env.talk, 'image')).data
    assert [f'name="album_files-{n}"'.encode() in page for n in range(4)] == [True, True, True, False]

    set_setting('max_album_images', 2)
    page = client.get(submit_url(env.app, env.talk, 'image')).data
    assert [f'name="album_files-{n}"'.encode() in page for n in range(2)] == [True, False]


def test_an_album_is_posted_with_its_alt_texts(images_allowed):
    env = images_allowed
    response = submit(env, env.stranger, env.talk, 'image', 'main.png', png(), image_alt_text='the main one',
                      **album_fields((png((9, 9, 9)), 'second one'), (png((8, 8, 8)), '')))

    assert response.status_code == 302, response.data[:400]
    post = the_post()
    assert post.type == POST_TYPE_IMAGE
    pictures = post.album_images()
    assert [file.alt_text for file in pictures] == ['the main one', 'second one', None]
    assert all(file.wrote_its_source() for file in pictures[1:])   # our upload: deleting the post removes it
    assert all(os.path.isfile(file.local_path_for_url(file.source_url)) for file in pictures)


def test_more_pictures_than_the_site_allows_are_refused(images_allowed):
    env = images_allowed
    set_setting('max_album_images', 2)

    response = submit(env, env.stranger, env.talk, 'image', 'main.png', png(),
                      **album_fields((png(), 'one'), (png(), 'two')))

    assert response.status_code == 200 and b'At most 2 images' in response.data
    assert the_post() is None


def test_an_album_picture_over_the_size_cap_is_refused(images_allowed, monkeypatch):
    env = images_allowed
    monkeypatch.setattr('app.community.forms.MAX_IMAGE_UPLOAD_BYTES', 200)

    response = submit(env, env.stranger, env.talk, 'image', 'main.png', png(size=(2, 2)),
                      **album_fields((png(size=(300, 300)), 'big')))

    assert response.status_code == 200 and b'This image filesize is too large.' in response.data
    assert the_post() is None


def _album_post(env):
    submit(env, env.stranger, env.talk, 'image', 'main.png', png(), image_alt_text='main',
           **album_fields((png((9, 9, 9)), 'keep me'), (png((8, 8, 8)), 'drop me')))
    return the_post()


def edit_fields(env, post, token, **fields):
    data = {'title': 'A clip', 'body': 'about it', 'image_alt_text': 'main', 'language_id': str(env.english.id),
            'notify_author': 'y', 'timezone': 'Europe/London', 'submit': 'Publish', 'csrf_token': token}
    data.update(fields)
    return data


def test_the_edit_page_lists_the_album(images_allowed):
    env = images_allowed
    post = _album_post(env)
    keep, drop = post.album_images()[1:]
    client, _ = env.client_for(env.stranger)

    page = client.get(f'/post/{post.id}/edit').data

    for file in (keep, drop):
        assert f'name="album_alt_{file.id}"'.encode() in page and f'name="album_remove_{file.id}"'.encode() in page
    assert b'name="album_files-0"' in page and b'name="album_files-1"' not in page   # 4 allowed, 3 used


def test_an_edit_re_captions_removes_and_adds(images_allowed):
    env = images_allowed
    post = _album_post(env)
    keep, drop = post.album_images()[1:]
    dropped_path = drop.local_path_for_url(drop.source_url)
    client, token = env.client_for(env.stranger)

    response = client.post(f'/post/{post.id}/edit', content_type='multipart/form-data', data=edit_fields(
        env, post, token, **{f'album_alt_{keep.id}': 'kept, re-captioned', f'album_alt_{drop.id}': 'drop me',
                             f'album_remove_{drop.id}': 'y'}, **album_fields((png((7, 7, 7)), 'new one'))))

    assert response.status_code == 302, response.data[:400]
    db.session.expire_all()
    pictures = db.session.get(Post, post.id).album_images()
    assert [file.alt_text for file in pictures] == ['main', 'kept, re-captioned', 'new one']
    assert pictures[1].id == keep.id
    assert not os.path.exists(dropped_path)


def test_an_edit_that_touches_nothing_keeps_the_album(images_allowed):
    env = images_allowed
    post = _album_post(env)
    before = [(file.id, file.alt_text) for file in post.album_images()]
    client, token = env.client_for(env.stranger)
    page_alts = {f'album_alt_{file.id}': file.alt_text for file in post.album_images()[1:]}

    response = client.post(f'/post/{post.id}/edit', content_type='multipart/form-data',
                           data=edit_fields(env, post, token, **page_alts))

    assert response.status_code == 302
    db.session.expire_all()
    assert [(file.id, file.alt_text) for file in db.session.get(Post, post.id).album_images()] == before


def test_an_album_picture_from_4chan_is_refused(images_allowed, monkeypatch):
    """The chan image filter reads each album picture as it reads the image: only the second picture is a chan
    screenshot here, and the error is on its own slot."""
    env = images_allowed
    db.session.get(Site, 1).enable_chan_image_filter = True
    db.session.commit()
    chan = png((66, 66, 66))
    monkeypatch.setattr('app.community.forms.pytesseract.image_to_string',
                        lambda image: 'Anonymous No.12345' if image.getpixel((0, 0)) == 66 else '')

    response = submit(env, env.stranger, env.talk, 'image', 'main.png', png(),
                      **album_fields((png((5, 5, 5)), 'fine'), (chan, 'chan')))

    assert response.status_code == 200 and b'This image is from 4chan.' in response.data
    assert the_post() is None
