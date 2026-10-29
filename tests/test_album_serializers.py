"""An album post's images go out in album order: the Note's Documents and the fetched Page's Images (Pixelfed R5).

The album is `post.image` and then `post.gallery` by weight, leaving out the rows that only remember a blocked image
(GALLERY_BLOCKED_WEIGHT). The push path's Page is pinned in tests/test_shared_tasks_send_post.py.
"""
from app import db
from app.activitypub.serializers.microblog import note_attachments
from app.activitypub.util import post_to_page
from app.constants import POST_TYPE_IMAGE
from app.models import File, GALLERY_BLOCKED_WEIGHT, post_file
from tests.test_serializers_microblog import _local

OURS = 'https://test.piefed.local/static/media/posts/ab/cd'


def _album(post, *images):
    """`post.image` is the first of `images` (source_url, alt), the rest its album at weight 1.., plus one blocked row."""
    first, *rest = [File(source_url=url, alt_text=alt, width=10, height=10) for url, alt in images]
    blocked = File(source_url=f'{OURS}/blocked.png')
    db.session.add_all([first, blocked, *rest])
    db.session.flush()
    post.type = POST_TYPE_IMAGE
    post.image_id = first.id
    for weight, file in enumerate(rest, start=1):
        db.session.execute(post_file.insert().values(post_id=post.id, file_id=file.id, weight=weight))
    db.session.execute(post_file.insert().values(post_id=post.id, file_id=blocked.id, weight=GALLERY_BLOCKED_WEIGHT))
    post.gallery_count = len(rest)
    db.session.commit()
    return post


def test_the_album_is_the_image_then_the_gallery_without_blocked_rows(db_session):
    post = _album(_local(post_type=POST_TYPE_IMAGE), (f'{OURS}/a.png', 'a'), (f'{OURS}/b.png', 'b'), (f'{OURS}/c.png', 'c'))
    assert [file.source_url for file in post.album_images()] == [f'{OURS}/a.png', f'{OURS}/b.png', f'{OURS}/c.png']


def test_a_post_without_an_image_has_no_album(db_session):
    assert _local(post_type=POST_TYPE_IMAGE).album_images() == []


def test_the_note_carries_a_document_per_image(db_session):
    post = _album(_local(post_type=POST_TYPE_IMAGE), (f'{OURS}/a.png', 'a'), (f'{OURS}/b.webp', 'b'), (f'{OURS}/c.gif', None))
    second = post.album_images()[1]

    assert note_attachments(post) == [
        {'type': 'Document', 'mediaType': 'image/png', 'url': f'{OURS}/a.png', 'name': 'a', 'width': 10, 'height': 10},
        {'type': 'Document', 'mediaType': 'image/jpeg', 'url': f'https://test.piefed.local/media/jpeg/{second.id}.jpg',
         'name': 'b', 'width': 10, 'height': 10},   # our WebP as its JPEG (R3)
        {'type': 'Document', 'mediaType': 'image/gif', 'url': f'{OURS}/c.gif', 'name': '', 'width': 10, 'height': 10},
    ]


def test_the_fetched_page_carries_an_image_per_picture_and_the_first_as_its_image(db_session):
    post = _album(_local('c1', post_type=POST_TYPE_IMAGE), (f'{OURS}/a.png', 'a'), (f'{OURS}/b.png', 'b'))

    page = post_to_page(post)

    assert page['attachment'] == [{'type': 'Image', 'url': f'{OURS}/a.png', 'name': 'a'},
                                  {'type': 'Image', 'url': f'{OURS}/b.png', 'name': 'b'}]
    assert page['image']['url'] == f'{OURS}/a.png'
