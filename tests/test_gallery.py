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


BLOCKED_HASH = '1' * 256
CLEAN_HASH = '0' * 256


@pytest.fixture
def hashing(app, pixelfed, monkeypatch):
    """A hashing endpoint that answers SECOND (and only SECOND) with a blocked
    image's hash, and a BlockedImage row holding that hash."""
    import httpx
    from app.models import BlockedImage
    monkeypatch.setitem(app.config, 'IMAGE_HASHING_ENDPOINT', 'https://hash.example/pdq')
    db.session.add(BlockedImage(hash=BLOCKED_HASH, file_name='blocked.jpg'))
    db.session.commit()
    asked = []

    def answer(request):
        image_url = request.url.params['image_url']
        asked.append(image_url)
        pdq = BLOCKED_HASH if image_url == hashing.blocked else CLEAN_HASH
        return httpx.Response(200, json={'quality': 100, 'pdq_hash_binary': pdq})
    hashing.blocked = None
    hashing.asked = asked
    pixelfed.route(host='hash.example').mock(side_effect=answer)
    return hashing


def test_gallery_images_are_hashed(ingest, hashing):
    post = ingest(album(image(FIRST, 'a'), image(SECOND, 'b')))

    assert SECOND in hashing.asked
    assert gallery_of(post)[0].hash == CLEAN_HASH


def test_a_blocked_gallery_image_refuses_the_post_as_a_blocked_primary_does(ingest, hashing):
    hashing.blocked = SECOND

    assert ingest(album(image(FIRST, 'a'), image(SECOND, 'b'))) is None
    assert Post.query.count() == 0
    assert File.query.filter_by(source_url=SECOND).count() == 0


def test_an_update_drops_a_blocked_gallery_image(ingest, hashing):
    from app.activitypub.util import update_post_from_activity
    post = ingest(album(image(FIRST, 'a'), image(THIRD, 'c')))
    hashing.blocked = SECOND

    update = album(image(FIRST, 'a'), image(SECOND, 'b'), image(THIRD, 'c'))
    update['type'] = 'Update'
    update_post_from_activity(post, update)
    db.session.refresh(post)

    assert [f.source_url for f in gallery_of(post)] == [THIRD]


def test_deleting_the_post_deletes_its_gallery(ingest, tmp_path):
    from app.models import post_file
    post = ingest(album(image(FIRST, 'a'), image(SECOND, 'b'), image(THIRD, 'c')))
    on_disk = []
    for number, file in enumerate(gallery_of(post)):
        path = tmp_path / f'{number}.jpg'
        path.write_bytes(b'x')
        file.file_path = str(path)
        on_disk.append(path)
    db.session.commit()
    gallery_ids = [file.id for file in gallery_of(post)]

    cache_urls = []
    post.delete_dependencies(cache_urls=cache_urls)
    db.session.commit()

    assert not any(path.exists() for path in on_disk)
    assert all(str(path) in cache_urls for path in on_disk)
    assert db.session.execute(post_file.select().where(post_file.c.post_id == post.id)).all() == []
    assert File.query.filter(File.id.in_(gallery_ids)).count() == 0


def test_image_type_attachments_keep_the_first_as_the_primary(ingest):
    """PieFed, Lemmy and Pixelfed send `type: Image`: the first is the post's own image, the rest the gallery in order."""
    post = ingest(album({'type': 'Image', 'url': FIRST, 'name': 'a'},
                        {'type': 'Image', 'url': SECOND, 'name': 'b'},
                        {'type': 'Image', 'url': THIRD, 'name': 'c'}))

    assert post.image.source_url == FIRST
    assert post.image.alt_text == 'a'
    assert [f.source_url for f in gallery_of(post)] == [SECOND, THIRD]


@pytest.fixture
def post_file_queries():
    """The SQL statements that read post_file, recorded while the test runs."""
    from sqlalchemy import event
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if 'post_file' in statement:
            seen.append(statement)
    engine = db.engine
    event.listen(engine, 'before_cursor_execute', record)
    yield seen
    event.remove(engine, 'before_cursor_execute', record)


def test_the_gallery_count_follows_the_gallery(ingest):
    from app.activitypub.util import update_post_from_activity
    post = ingest(album(image(FIRST, 'a'), image(SECOND, 'b'), image(THIRD, 'c')))
    assert post.gallery_count == 2

    update = album(image(FIRST, 'a'))
    update['type'] = 'Update'
    update_post_from_activity(post, update)
    db.session.refresh(post)
    assert post.gallery_count == 0


def test_the_teaser_badge_reads_no_post_file_rows(app, ingest, post_file_queries):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    post = ingest(album(image(FIRST, 'a'), image(SECOND, 'b'), image(THIRD, 'c')))
    post_file_queries.clear()

    html = app.test_client().get(f'/c/{post.community.link()}').get_data(as_text=True)

    assert '3 images' in html
    assert post_file_queries == []


def test_the_api_reads_no_post_file_rows_for_a_post_without_a_gallery(app, ingest, post_file_queries):
    from app.api.alpha.views import post_view
    post = ingest(album(image(FIRST, 'a')))
    post_file_queries.clear()

    post_view(post=post, variant=1)

    assert post_file_queries == []
