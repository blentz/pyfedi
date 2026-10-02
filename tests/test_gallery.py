"""Multi-image posts (Pixelfed albums, Mastodon): the attachments after the
first are kept in `Post.gallery`, shown on the post page, and offered to API
clients under `extensions`."""
import io

import pytest
import respx
from PIL import Image

from app import db
from app.constants import POST_TYPE_IMAGE
from app.models import File, Language, Post, Site
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_site, make_user)

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
FIRST = 'https://pix.example/storage/one.jpg'
SECOND = 'https://pix.example/storage/two.jpg'
THIRD = 'https://pix.example/storage/three.png'


def a_jpeg():
    buffer = io.BytesIO()
    Image.new('RGB', (4, 4), 'red').save(buffer, 'JPEG')
    return buffer.getvalue()


def image(url, name, **extra):
    return {'type': 'Document', 'mediaType': 'image/jpeg', 'url': url, 'name': name, **extra}


def album(*attachments):
    return {
        'id': 'https://pix.example/p/alice/1/activity',
        'type': 'Create',
        'object': {
            'id': 'https://pix.example/p/alice/1',
            'type': 'Note',
            'content': '<p>my album</p>',
            'attributedTo': 'https://pix.example/users/alice',
            'to': [PUBLIC],
            'cc': [],
            'attachment': list(attachments),
        },
    }


@pytest.fixture
def author(db_session):
    return make_user(make_instance('pix.example', software='pixelfed'), 'alice')


@pytest.fixture
def pixelfed():
    """The peer answers every HEAD (is_image_url) and GET (make_image_sizes) as a
    1x1 jpeg."""
    with respx.mock(assert_all_called=False) as router:
        router.route(host='pix.example').respond(200, headers={'Content-Type': 'image/jpeg'}, content=a_jpeg())
        yield router


@pytest.fixture
def ingest(db_session, author, pixelfed):
    from app.activitypub.util import create_post
    make_site()
    community = make_community()
    make_community_member(author, community)

    def _ingest(activity):
        return create_post(False, community, activity, author)
    return _ingest


def gallery_of(post):
    return post.gallery.all()


def test_second_attachment_is_stored_in_the_gallery(ingest):
    post = ingest(album(image(FIRST, 'first alt'),
                        image(SECOND, 'second alt', width=640, height=480)))

    assert post.type == POST_TYPE_IMAGE
    assert post.image.source_url == FIRST
    extra = gallery_of(post)
    assert len(extra) == 1
    assert extra[0].source_url == SECOND
    assert extra[0].alt_text == 'second alt'
    assert (extra[0].width, extra[0].height) == (640, 480)


def test_gallery_keeps_attachment_order(ingest):
    post = ingest(album(image(FIRST, 'a'), image(SECOND, 'b'), image(THIRD, 'c')))

    assert [f.source_url for f in gallery_of(post)] == [SECOND, THIRD]


def test_single_image_has_no_gallery(ingest):
    post = ingest(album(image(FIRST, 'a')))

    assert gallery_of(post) == []


def test_unsafe_scheme_attachment_is_not_stored(ingest):
    post = ingest(album(image(FIRST, 'a'), image('javascript:alert(1)', 'bad'),
                        image(SECOND, 'b')))

    assert [f.source_url for f in gallery_of(post)] == [SECOND]


def test_update_replaces_the_gallery(ingest):
    from app.activitypub.util import update_post_from_activity
    post = ingest(album(image(FIRST, 'a'), image(SECOND, 'b')))
    assert len(gallery_of(post)) == 1

    update = album(image(FIRST, 'a'), image(THIRD, 'c'))
    update['type'] = 'Update'
    update_post_from_activity(post, update)
    db.session.refresh(post)
    assert [f.source_url for f in gallery_of(post)] == [THIRD]

    update = album(image(FIRST, 'a'))
    update['type'] = 'Update'
    update_post_from_activity(post, update)
    db.session.refresh(post)
    assert gallery_of(post) == []


def test_post_page_shows_every_image_and_alt_text(app, ingest):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    post = ingest(album(image(FIRST, 'first alt'), image(SECOND, 'second alt')))

    html = app.test_client().get(f'/post/{post.id}', follow_redirects=True).get_data(as_text=True)

    assert FIRST in html and SECOND in html
    assert 'first alt' in html and 'second alt' in html


def test_api_post_view_offers_the_gallery_under_extensions(app, ingest):
    from app.api.alpha.views import post_view
    post = ingest(album(image(FIRST, 'first alt'),
                        image(SECOND, 'second alt', width=640, height=480)))

    view = post_view(post=post, variant=1)

    assert view['extensions']['gallery'] == [
        {'url': SECOND, 'alt_text': 'second alt', 'width': 640, 'height': 480}]


def test_api_post_view_has_no_extensions_without_a_gallery(app, ingest):
    from app.api.alpha.views import post_view
    post = ingest(album(image(FIRST, 'a')))

    assert 'extensions' not in post_view(post=post, variant=1)


def test_teaser_badge_counts_the_images(app, ingest):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    post = ingest(album(image(FIRST, 'a'), image(SECOND, 'b'), image(THIRD, 'c')))

    html = app.test_client().get(f'/c/{post.community.link()}').get_data(as_text=True)

    assert 'post_gallery_badge' in html
    assert '3 images' in html
