"""Gallery posts through the web forms (spec 2026-11-01 G-6; Pixelfed interop R5, R7).

The gallery form offers one slot per picture the site allows (`max_album_images()`), each a file
(`images-N-image_file`) and its alt text (`images-N-alt_text`). Each picture is checked as an image post's image is
(R207's 10 MB, the chan filter) and stored as it is (store_post_image), so its File names the file written and
deleting the post removes it. The edit page lists the pictures in their slots, each with a box to remove it
(`gallery_remove_N`); a slot's new upload replaces its picture, a blank alt text keeps the picture's own.
"""
import io
import os

import pytest
from PIL import Image

from app import db
from app.constants import POST_TYPE_GALLERY
from app.models import File, Post, Site
from app.utils import set_setting
from tests.test_media_upload import env, submit, submit_url  # noqa: F401 (env)

pytestmark = pytest.mark.usefixtures('site', 'private_static_tree')


@pytest.fixture
def images_allowed(env, monkeypatch):
    db.session.get(Site, 1).allow_local_image_posts = True
    db.session.commit()
    monkeypatch.setattr('app.shared.post.make_image_sizes', lambda *a, **k: None)   # resizing is not under test
    monkeypatch.setattr('app.utils.is_image_url', lambda url: True)   # post_edit's is_local_image_url's HEAD

    def composite(urls, post, return_file_id=True):   # it fetches every picture: not under test
        thumbnail = File(source_url='https://test.piefed.local/static/media/posts/co/mp/composite.webp')
        db.session.add(thumbnail)
        db.session.flush()
        return thumbnail.id
    monkeypatch.setattr('app.shared.post.build_gallery_thumbnail', composite)
    return env


def png(colour=(1, 2, 3), size=(10, 10)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', size, colour).save(buffer, format='PNG')
    return buffer.getvalue()


def slots(*pictures, start=0):
    fields = {}
    for n, (payload, alt) in enumerate(pictures, start=start):
        if payload is not None:
            fields[f'images-{n}-image_file'] = (io.BytesIO(payload), f'p{n}.png')
        fields[f'images-{n}-alt_text'] = alt
    return fields


def the_post():
    return Post.query.filter_by(title='A clip').one_or_none()


def post_gallery(env, *pictures):
    return submit(env, env.stranger, env.talk, 'gallery', **slots(*pictures))


def test_the_form_offers_a_slot_per_picture_the_site_allows(images_allowed):
    env = images_allowed
    client, _ = env.client_for(env.stranger)
    page = client.get(submit_url(env.app, env.talk, 'gallery')).data
    assert [f'name="images-{n}-image_file"'.encode() in page for n in range(5)] == [True] * 4 + [False]

    set_setting('max_album_images', 2)
    page = client.get(submit_url(env.app, env.talk, 'gallery')).data
    assert [f'name="images-{n}-image_file"'.encode() in page for n in range(3)] == [True, True, False]


def test_a_gallery_is_posted_with_its_alt_texts(images_allowed):
    env = images_allowed
    response = post_gallery(env, (png(), 'the first one'), (png((9, 9, 9)), 'second one'), (png((8, 8, 8)), ''))

    assert response.status_code == 302, response.data[:400]
    post = the_post()
    assert post.type == POST_TYPE_GALLERY
    pictures = post.gallery_pictures()
    assert [file.alt_text for file in pictures] == ['the first one', 'second one', None]
    assert all(file.wrote_its_source() for file in pictures)   # our upload: deleting the post removes it
    assert all(os.path.isfile(file.local_path_for_url(file.source_url)) for file in pictures)


def test_one_picture_is_not_a_gallery(images_allowed):
    env = images_allowed

    response = post_gallery(env, (png(), 'alone'))

    assert response.status_code == 302 and response.headers['Location'].endswith('/submit/gallery')
    assert the_post() is None


def test_slots_past_the_sites_limit_are_not_read(images_allowed):
    env = images_allowed
    set_setting('max_album_images', 2)

    post_gallery(env, (png(), 'one'), (png((4, 4, 4)), 'two'), (png((5, 5, 5)), 'three'))

    assert [file.alt_text for file in the_post().gallery_pictures()] == ['one', 'two']


def test_a_picture_over_the_size_cap_is_refused(images_allowed, monkeypatch):
    env = images_allowed
    monkeypatch.setattr('app.community.forms.MAX_IMAGE_UPLOAD_BYTES', 200)

    response = post_gallery(env, (png(size=(2, 2)), 'small'), (png(size=(300, 300)), 'big'))

    assert response.status_code == 200 and b'This image filesize is too large.' in response.data
    assert the_post() is None


def test_a_picture_from_4chan_is_refused(images_allowed, monkeypatch):
    """The chan image filter reads each picture as it reads an image post's: only the second is a chan screenshot."""
    env = images_allowed
    db.session.get(Site, 1).enable_chan_image_filter = True
    db.session.commit()
    monkeypatch.setattr('app.community.forms.pytesseract.image_to_string',
                        lambda image: 'Anonymous No.12345' if image.getpixel((0, 0)) == 66 else '')

    response = post_gallery(env, (png((5, 5, 5)), 'fine'), (png((66, 66, 66)), 'chan'))

    assert response.status_code == 200 and b'This image is from 4chan.' in response.data
    assert the_post() is None


def test_a_gallery_without_a_title_is_not_posted(images_allowed):
    env = images_allowed

    response = submit(env, env.stranger, env.talk, 'gallery', title='', **slots((png(), 'one'), (png((9, 9, 9)), 'two')))

    assert response.status_code == 200 and the_post() is None
    assert Post.query.count() == 0


def _gallery(env):
    post_gallery(env, (png(), 'first'), (png((9, 9, 9)), 'keep me'), (png((8, 8, 8)), 'drop me'))
    return the_post()


def edit_fields(env, token, **fields):
    data = {'title': 'A clip', 'body': 'about it', 'language_id': str(env.english.id),
            'notify_author': 'y', 'timezone': 'Europe/London', 'submit': 'Publish', 'csrf_token': token}
    data.update(fields)
    return data


def test_the_edit_page_lists_the_pictures_in_their_slots(images_allowed):
    env = images_allowed
    post = _gallery(env)
    client, _ = env.client_for(env.stranger)

    page = client.get(f'/post/{post.id}/edit').data

    for n, file in enumerate(post.gallery_pictures()):
        assert file.alt_text.encode() in page and f'name="gallery_remove_{n}"'.encode() in page
    assert b'name="gallery_remove_3"' not in page and b'name="images-3-image_file"' in page   # 4 allowed, 3 used


def test_an_edit_re_captions_removes_replaces_and_adds(images_allowed):
    env = images_allowed
    post = _gallery(env)
    first, keep, drop = post.gallery_pictures()
    dropped_path = drop.local_path_for_url(drop.source_url)
    client, token = env.client_for(env.stranger)

    response = client.post(f'/post/{post.id}/edit', content_type='multipart/form-data', data=edit_fields(
        env, token, **slots((None, ''), (None, 'kept, re-captioned'), (None, ''), (png((7, 7, 7)), 'new one')),
        gallery_remove_2='y'))

    assert response.status_code == 302, response.data[:400]
    db.session.expire_all()
    pictures = db.session.get(Post, post.id).gallery_pictures()
    assert [file.alt_text for file in pictures] == ['first', 'kept, re-captioned', 'new one']
    assert [pictures[0].id, pictures[1].id] == [first.id, keep.id]
    assert not os.path.exists(dropped_path)


def test_an_edit_that_touches_nothing_keeps_the_pictures(images_allowed):
    env = images_allowed
    post = _gallery(env)
    before = [(file.id, file.alt_text) for file in post.gallery_pictures()]
    client, token = env.client_for(env.stranger)

    response = client.post(f'/post/{post.id}/edit', content_type='multipart/form-data', data=edit_fields(env, token))

    assert response.status_code == 302
    db.session.expire_all()
    assert [(file.id, file.alt_text) for file in db.session.get(Post, post.id).gallery_pictures()] == before


def test_the_image_form_takes_one_picture(images_allowed):
    """The album slots the image form had are gone: a gallery is made with the gallery form (G-6)."""
    env = images_allowed
    client, _ = env.client_for(env.stranger)

    page = client.get(submit_url(env.app, env.talk, 'image')).data

    assert b'album_files' not in page and b'images-0-image_file' not in page
