"""Migration c3e9a7d1f5b2 (spec 2026-11-01 G-8): an image post with an album becomes a gallery post, reversibly.

Before it, an album was an image post whose image was its first picture and whose post_file rows at weights 1.. its
others. After it, the post is a gallery: a copy of the image's File (not its files on disk) is its picture at weight
0, gallery_count counts it, and the image stays as the thumbnail. The downgrade reverses exactly those posts.

The round trip runs on a connection of its own and is rolled back, as tests/test_migration_visibility.py's does.
"""
import importlib.util
import os
from pathlib import Path

import pytest

from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import current_app
from sqlalchemy import text

from app import db
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE
from app.models import File, GALLERY_BLOCKED_WEIGHT, post_file
from tests.factories import make_community, make_instance, make_post, make_user

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / \
    'c3e9a7d1f5b2_album_posts_become_galleries.py'


HASH = '10' * 128   # file.hash is bit(256)


def load_migration():
    spec = importlib.util.spec_from_file_location('album_to_gallery_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _image_post(community, author, n, album=()):
    """An image post as the branch stored an album: its image the first picture, `album` at weights 1.."""
    post = make_post(community, author, f'https://m.example/p/{n}')
    post.url = f'https://m.example/{n}/0.jpg'
    image = File(source_url=f'https://m.example/{n}/0.jpg', alt_text='first', width=4, height=3, hash=HASH,
                 file_path=f'app/static/media/posts/{n}/0.jpg', file_written_by_us=True)
    db.session.add(image)
    db.session.flush()
    post.type, post.image_id = POST_TYPE_IMAGE, image.id
    for weight, url in enumerate(album, start=1):
        file = File(source_url=url)
        db.session.add(file)
        db.session.flush()
        db.session.execute(post_file.insert().values(post_id=post.id, file_id=file.id, weight=weight))
    blocked = File(source_url=f'https://m.example/{n}/blocked.jpg')
    db.session.add(blocked)
    db.session.flush()
    db.session.execute(post_file.insert().values(post_id=post.id, file_id=blocked.id, weight=GALLERY_BLOCKED_WEIGHT))
    post.gallery_count = len(album)
    return post.id, image.id


def _state(connection, post_id):
    post = connection.execute(text('SELECT type, gallery_count, image_id, url FROM post WHERE id = :id'),
                              {'id': post_id}).one()
    rows = connection.execute(text(
        'SELECT pf.weight, f.source_url, f.alt_text, f.width, f.height, f.hash, f.file_path, f.file_written_by_us '
        'FROM post_file pf JOIN file f ON f.id = pf.file_id WHERE pf.post_id = :id ORDER BY pf.weight'),
        {'id': post_id}).all()
    return tuple(post), [tuple(row) for row in rows]


def test_an_album_becomes_a_gallery_and_back(app, db_session):
    author = make_user(make_instance('m.example'), 'alice')
    community = make_community()
    album_id, album_image = _image_post(community, author, 1, ['https://m.example/1/1.jpg', 'https://m.example/1/2.jpg'])
    single_id, _ = _image_post(community, author, 2)
    db.session.commit()
    db.session.close()   # nothing of the session's may still hold a lock on post or post_file
    migration = load_migration()

    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            before = {post_id: _state(connection, post_id) for post_id in (album_id, single_id)}
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                (kind, count, image_id, url), rows = _state(connection, album_id)
                assert (kind, count, image_id, url) == (POST_TYPE_GALLERY, 3, album_image, 'https://m.example/1/0.jpg')
                assert rows == [(GALLERY_BLOCKED_WEIGHT, 'https://m.example/1/blocked.jpg', None, None, None, None,
                                 None, False),
                                # the copy: the image's url, alt text, size, hash and path, never flagged as ours
                                (0, 'https://m.example/1/0.jpg', 'first', 4, 3, HASH, 'app/static/media/posts/1/0.jpg',
                                 False),
                                (1, 'https://m.example/1/1.jpg', None, None, None, None, None, False),
                                (2, 'https://m.example/1/2.jpg', None, None, None, None, None, False)]
                assert _state(connection, single_id) == before[single_id]   # one picture stays an image post

                migration.downgrade()
            assert {post_id: _state(connection, post_id) for post_id in (album_id, single_id)} == before
        finally:
            transaction.rollback()


@pytest.mark.usefixtures('private_static_tree')
@pytest.mark.parametrize('with_copy', [False, True])
def test_deleting_a_converted_gallery_removes_the_images_files(app, db_session, with_copy):
    """The copy names the image's own file (file_path), and a file another record names is kept. So the post's
    delete removes the gallery's copy, and flushes, before it asks the image: else every converted post's image
    would stay on disk for good."""
    author = make_user(make_instance('m.example'), 'alice')
    post = make_post(make_community(), author, 'https://m.example/p/1')
    path = 'app/static/media/posts/ab/cd/gallery_first_picture.png'
    os.makedirs(os.path.dirname(path))
    with open(path, 'wb') as handle:
        handle.write(b'not really a png')
    url = f"{current_app.config['SERVER_URL']}/static/media/posts/ab/cd/gallery_first_picture.png"
    image = File(source_url=url, file_path=path, file_written_by_us=True)   # a size we wrote of it
    gallery = [File(source_url=url, file_path=path)] if with_copy else []   # the copy: as the migration makes it
    gallery.append(File(source_url='https://m.example/2.png'))
    db.session.add_all([image, *gallery])
    db.session.flush()
    post.type, post.image_id, post.gallery_count = POST_TYPE_GALLERY, image.id, len(gallery)
    for weight, file in enumerate(gallery):
        db.session.execute(post_file.insert().values(post_id=post.id, file_id=file.id, weight=weight))
    db.session.commit()

    post.delete_dependencies(cache_urls=[])

    assert not os.path.exists(path)
    assert db.session.execute(post_file.select().where(post_file.c.post_id == post.id)).all() == []
