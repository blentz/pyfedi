"""I1 + I2 (Loops spike 2026-10-30, spec 2026-10-30 §3.1): a video Note's `preview` becomes the post's thumbnail, with
the video attachment's alt text.

Loops sends each video as a `Note` whose `attachment` is a `video/mp4` `Document` (alt text in its `name`) and whose
`preview` is the thumbnail it made: `{type: Image, name, width, height, url: {href, mediaType}}`. PieFed kept the mp4
as the post's url and dropped the rest, so the player had no poster and card views no picture."""
import pytest
from flask import current_app, g

from app import db
from app.activitypub.util import update_post_from_activity
from app.constants import POST_TYPE_VIDEO
from app.models import Post, Site, video_preview

AUTHOR = 'https://loops.example/ap/users/1'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
VIDEO = 'https://s3.loops.example/loops/videos/1/2/clip.720p.mp4'
THUMB = 'https://s3.loops.example/loops/videos/1/2/clip_thumb.jpg'
PREVIEW = {'type': 'Image', 'name': 'a test clip', 'width': 720, 'height': 1280,
           'url': {'href': THUMB, 'mediaType': 'image/jpeg'}}


@pytest.fixture
def env(app, api_baseline, monkeypatch, http_mock):
    from types import SimpleNamespace

    from tests.factories import make_community, make_community_member, make_user

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('microblogs')
    author = make_user(api_baseline.instance_remote, 'lucy')
    author.ap_id = 'lucy@loops.example'
    author.ap_profile_id = AUTHOR
    author.ap_public_url = AUTHOR
    db.session.commit()
    make_community_member(author, community)
    monkeypatch.setitem(current_app.config, 'IMAGE_HASHING_ENDPOINT', '')
    http_mock.head(VIDEO).respond(200, headers={'Content-Type': 'video/mp4'})   # is_image_url's HEAD
    return SimpleNamespace(community=community, author=author)


def video_note(counter=[0], **fields):
    counter[0] += 1
    note = {'id': f'https://loops.example/ap/users/1/video/{counter[0]}', 'type': 'Note',
            'attributedTo': AUTHOR, 'to': [PUBLIC], 'cc': [AUTHOR + '/followers'],
            'published': '2026-10-30T09:47:27+00:00', 'url': f'https://loops.example/v/{counter[0]}',
            'content': '<p>Loops probe one</p>', 'sensitive': False,
            'attachment': [{'type': 'Document', 'mediaType': 'video/mp4', 'url': VIDEO, 'name': 'a test clip',
                            'width': 720, 'height': 1280}],
            'preview': PREVIEW}
    note.update(fields)
    return {k: v for k, v in note.items() if v is not ...}


def create(env, note):
    return Post.new(env.author, env.community, {'id': note['id'] + '/activity', 'type': 'Create',
                                                'to': [PUBLIC], 'object': note})


def test_the_preview_is_the_thumbnail_with_the_attachments_alt_text(env):
    post = create(env, video_note())

    assert post.type == POST_TYPE_VIDEO and post.url == VIDEO
    assert post.image.source_url == THUMB
    assert post.image.alt_text == 'a test clip'
    assert (post.image.width, post.image.height) == (720, 1280)


def test_the_previews_name_is_the_alt_text_when_the_attachment_has_none(env):
    note = video_note(attachment=[{'type': 'Document', 'mediaType': 'video/mp4', 'url': VIDEO}],
                      preview=dict(PREVIEW, name='from the preview'))

    assert create(env, note).image.alt_text == 'from the preview'


def test_an_image_key_wins_over_the_preview(env):
    post = create(env, video_note(image={'url': 'https://loops.example/cover.jpg'}))

    assert post.image.source_url == 'https://loops.example/cover.jpg'


@pytest.mark.parametrize('preview,expected', [
    (PREVIEW, (THUMB, 'a test clip', 720, 1280)),
    (dict(PREVIEW, url=THUMB), (THUMB, 'a test clip', 720, 1280)),
    (dict(PREVIEW, url=[{'href': THUMB, 'mediaType': 'image/jpeg'}]), (THUMB, 'a test clip', 720, 1280)),
    ([{'type': 'Link', 'href': 'https://x.example/a'}, PREVIEW], (THUMB, 'a test clip', 720, 1280)),
    ({'type': 'Image', 'url': THUMB}, (THUMB, None, None, None)),
    (dict(PREVIEW, width='wide', height=None, name=5), (THUMB, None, None, None)),
    (dict(PREVIEW, url={'href': VIDEO, 'mediaType': 'video/mp4'}), None),
    (dict(PREVIEW, url='javascript:alert(1)'), None),
    (dict(PREVIEW, url=[]), None),
    (dict(PREVIEW, url={'mediaType': 'image/jpeg'}), None),
    (dict(PREVIEW, type='Video'), None),
    ([], None), ('https://x.example/a.jpg', None), (None, None), (5, None),
], ids=['loops-shape', 'string-url', 'list-url', 'list-of-previews', 'bare', 'junk-sizes-and-name', 'not-an-image',
        'unsafe-scheme', 'empty-url-list', 'no-href', 'not-an-Image', 'empty-list', 'string', 'none', 'number'])
def test_video_preview_reads_every_shape_a_peer_might_send(preview, expected):
    assert video_preview({'preview': preview}) == expected


def test_an_update_that_changes_the_video_takes_the_new_preview(env, http_mock):
    post = create(env, video_note(preview=...))
    assert post.image is None
    new_video = VIDEO.replace('clip', 'other')
    http_mock.head(new_video).respond(200, headers={'Content-Type': 'video/mp4'})
    note = video_note(attachment=[{'type': 'Document', 'mediaType': 'video/mp4', 'url': new_video,
                                   'name': 'the new clip'}])
    note['id'] = post.ap_id

    update_post_from_activity(post, {'id': post.ap_id + '#update', 'type': 'Update', 'actor': AUTHOR,
                                     'to': [PUBLIC], 'object': note})

    db.session.expire_all()
    assert post.url == new_video and post.type == POST_TYPE_VIDEO
    assert post.image.source_url == THUMB and post.image.alt_text == 'the new clip'
