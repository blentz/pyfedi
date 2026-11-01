"""A gallery post's pictures go out in order: the Note's Documents and the fetched Page's (Pixelfed R5, spec
2026-11-01 G-4).

The pictures are `post.gallery` by weight 0.., leaving out the rows that only remember a blocked image
(GALLERY_BLOCKED_WEIGHT); `post.image` is the thumbnail and never one of them (G-2). The Page form adds v1.8.10's Link
to the post's page; the Note form does not. The push path's Page is pinned in tests/test_shared_tasks_send_post.py.
"""
from app import db
from app.activitypub.serializers.microblog import note_attachments
from app.activitypub.util import post_to_page
from app.constants import POST_TYPE_GALLERY, POST_TYPE_IMAGE
from app.models import File, GALLERY_BLOCKED_WEIGHT, post_file
from tests.test_serializers_microblog import _local

OURS = 'https://test.piefed.local/static/media/posts/ab/cd'


def _gallery(post, *images):
    """`images` (source_url, alt) are the gallery at weight 0.., with a composite thumbnail and one blocked row."""
    pictures = [File(source_url=url, alt_text=alt, width=10, height=10) for url, alt in images]
    thumbnail = File(source_url=f'{OURS}/composite.webp', width=10, height=10)
    blocked = File(source_url=f'{OURS}/blocked.png')
    db.session.add_all([thumbnail, blocked, *pictures])
    db.session.flush()
    post.type = POST_TYPE_GALLERY
    post.image_id = thumbnail.id
    for weight, file in enumerate(pictures):
        db.session.execute(post_file.insert().values(post_id=post.id, file_id=file.id, weight=weight))
    db.session.execute(post_file.insert().values(post_id=post.id, file_id=blocked.id, weight=GALLERY_BLOCKED_WEIGHT))
    post.gallery_count = len(pictures)
    db.session.commit()
    return post


def test_the_pictures_are_the_gallery_without_the_thumbnail_or_blocked_rows(db_session):
    post = _gallery(_local(post_type=POST_TYPE_GALLERY), (f'{OURS}/a.png', 'a'), (f'{OURS}/b.png', 'b'),
                    (f'{OURS}/c.png', 'c'))
    assert [file.source_url for file in post.album_images()] == [f'{OURS}/a.png', f'{OURS}/b.png', f'{OURS}/c.png']


def test_an_image_post_has_its_one_picture(db_session):
    post = _local(post_type=POST_TYPE_IMAGE)
    assert post.album_images() == ([post.image] if post.image is not None else [])


def test_the_note_carries_a_document_per_picture(db_session):
    post = _gallery(_local(post_type=POST_TYPE_GALLERY), (f'{OURS}/a.png', 'a'), (f'{OURS}/b.webp', 'b'),
                    (f'{OURS}/c.gif', None))
    second = post.album_images()[1]

    assert note_attachments(post) == [
        {'type': 'Document', 'mediaType': 'image/png', 'url': f'{OURS}/a.png', 'name': 'a', 'width': 10, 'height': 10},
        {'type': 'Document', 'mediaType': 'image/jpeg', 'url': f'https://test.piefed.local/media/jpeg/{second.id}.jpg',
         'name': 'b', 'width': 10, 'height': 10},   # our WebP as its JPEG (R3)
        {'type': 'Document', 'mediaType': 'image/gif', 'url': f'{OURS}/c.gif', 'name': '', 'width': 10, 'height': 10},
    ]


def test_the_fetched_page_carries_a_document_per_picture_then_a_link_to_the_post(db_session):
    post = _gallery(_local('c1', post_type=POST_TYPE_GALLERY), (f'{OURS}/a.png', 'a'), (f'{OURS}/b.png', 'b'))

    page = post_to_page(post)

    assert [(item['type'], item.get('url') or item.get('href')) for item in page['attachment']] == [
        ('Document', f'{OURS}/a.png'), ('Document', f'{OURS}/b.png'), ('Link', post.public_url())]
    assert page['image']['url'].endswith('composite.webp')   # the thumbnail
