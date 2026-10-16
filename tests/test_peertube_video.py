"""peertube_video (spec 2026-10-14 §4.3, amended §10 A15.4): a ready video upload in a local video_channel community
as the PeerTube Video that is pushed and fetched."""
import uuid

import pytest
from flask import current_app

from app import db
from app.activitypub.serializers import peertube_video, uses_peertube_video
from app.activitypub.util import post_to_page
from app.constants import (COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_VIDEO_CHANNEL, POST_TYPE_ARTICLE,
                           POST_TYPE_LINK, POST_TYPE_VIDEO, VISIBILITY_FOLLOWERS)
from app.models import File, Language, Tag, media_of
from app.shared.media import with_media
from tests.factories import make_community, make_instance, make_post, make_post_flair, make_user

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


@pytest.fixture
def video_post(db_session):
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True)
    community = make_community('chan', kind=COMMUNITY_KIND_VIDEO_CHANNEL)
    post = make_post(community, alice, ap_id='https://test.piefed.local/post/1', title='A video')
    server = current_app.config['SERVER_URL']
    post.type = POST_TYPE_VIDEO
    post.body = 'About the video'
    post.url = f'{server}/static/media/posts/ab/cd/abcdefghijklmno.mp4'
    with_media(post, status='ready', uuid=str(uuid.uuid4()), mime='video/mp4', bytes=12741, duration_s=10,
               width=640, height=360, fps=30, thumbnail_width=640, thumbnail_height=360)
    post.image = File(source_url=f'{server}/static/media/posts/ef/gh/thumbthumbthumb.jpg', width=640, height=360)
    db.session.commit()
    return post


def test_a_ready_video_is_a_peertube_video(video_post):
    video = peertube_video(video_post)
    server = current_app.config['SERVER_URL']
    assert video['type'] == 'Video'
    assert video['id'] == video_post.public_url()
    assert video['uuid'] == media_of(video_post)['uuid']
    assert video['name'] == video_post.title
    assert video['duration'] == 'PT10S'
    assert video['published'] and video['updated']
    assert video['views'] == 0
    assert video['likes'] == f'{server}/post/{video_post.id}/likes'
    assert video['dislikes'] == f'{server}/post/{video_post.id}/dislikes'
    assert video['url'][0] == {'type': 'Link', 'mediaType': 'video/mp4', 'href': video_post.url,
                               'height': 360, 'size': 12741, 'width': 640, 'fps': 30}
    assert video['url'][1] == {'type': 'Link', 'mediaType': 'text/html', 'href': video_post.public_url()}
    assert video['icon'] == [{'type': 'Image', 'url': video_post.image.source_url, 'mediaType': 'image/jpeg',
                              'width': 640, 'height': 360}]
    assert video['attributedTo'] == [{'type': 'Person', 'id': video_post.author.public_url()},
                                     {'type': 'Group', 'id': video_post.community.public_url()}]
    assert video['to'] == [PUBLIC]
    assert video['cc'] == [video_post.community.ap_followers_url]
    assert video['commentsPolicy'] == 1
    assert (video['state'], video['waitTranscoding'], video['isLiveBroadcast']) == (1, False, False)


def test_fetch_equals_push(video_post):
    assert post_to_page(video_post) == peertube_video(video_post)


def test_an_edited_video_is_updated_when_it_was_edited(video_post):
    edited = video_post.posted_at.replace(year=2030)
    video_post.edited_at = edited
    assert peertube_video(video_post)['updated'] == edited.isoformat() + '+00:00'


@pytest.mark.parametrize('title, name', [('ab', 'ab.'), ('x', 'x..'), ('  abc  ', 'abc'), ('t' * 121, 't' * 120)])
def test_the_name_fits_peertube(video_post, title, name):
    video_post.title = title
    assert peertube_video(video_post)['name'] == name


def test_a_mov_is_sent_as_mp4(video_post):
    with_media(video_post, mime='video/quicktime')
    assert peertube_video(video_post)['url'][0]['mediaType'] == 'video/mp4'


def test_webm_keeps_its_type_and_no_fps_is_left_out(video_post):
    with_media(video_post, mime='video/webm', fps=None, width=0)
    link = peertube_video(video_post)['url'][0]
    assert link['mediaType'] == 'video/webm'
    assert 'fps' not in link and 'width' not in link


@pytest.mark.parametrize('body, sent', [('', False), ('ab ', False), ('abc', True)])
def test_the_description_is_sent_from_three_characters(video_post, body, sent):
    video_post.body = body
    video = peertube_video(video_post)
    assert ('content' in video) is sent
    if sent:
        assert (video['content'], video['mediaType']) == ('abc', 'text/markdown')


def test_no_body_sends_no_description(video_post):
    video_post.body = None
    assert 'content' not in peertube_video(video_post)


def test_only_hashtags_of_two_to_thirty_characters_are_sent(video_post):
    for name in ('a', 'ok', 't' * 30, 't' * 31):
        video_post.tags.append(Tag(name=name, display_as=name.capitalize()))
    db.session.commit()
    make_post_flair(video_post, 'a flair')
    assert len(video_post.tags_for_activitypub()) == 5
    names = [tag['name'].lstrip('#') for tag in peertube_video(video_post)['tag']]
    assert names == ['ok', 't' * 30]


def test_comments_off_is_policy_two_and_flags_are_sensitive(video_post):
    video_post.comments_enabled = False
    video_post.content_warning = 'cw'
    video = peertube_video(video_post)
    assert video['commentsPolicy'] == 2
    assert video['sensitive'] is True


def test_an_unflagged_video_is_not_sensitive(video_post):
    assert peertube_video(video_post)['sensitive'] is False


def test_language_is_sent_when_set(video_post):
    assert 'language' not in peertube_video(video_post)
    language = Language(code='fr', name='French')
    db.session.add(language)
    db.session.commit()
    video_post.language_id = language.id
    db.session.commit()
    assert peertube_video(video_post)['language'] == {'identifier': 'fr', 'name': 'French'}
    assert peertube_video(video_post)['language'] == {'identifier': video_post.language_code(),
                                                      'name': video_post.language_name()}


def _discussion(post):
    post.community.kind = COMMUNITY_KIND_DISCUSSION


def _remote(post):
    post.community.ap_id = 'chan@peer.example'
    post.community.ap_profile_id = 'https://peer.example/c/chan'


def _link(post):
    post.type = POST_TYPE_LINK


def _processing(post):
    with_media(post, status='processing')


def _audio(post):
    with_media(post, mime='audio/mpeg')


def _followers(post):
    post.visibility = VISIBILITY_FOLLOWERS


def _no_image(post):
    post.image = None


def _no_dims(post):
    with_media(post, thumbnail_width=None)


def _no_media(post):
    post.extensions = {}


@pytest.mark.parametrize('change', [_discussion, _remote, _link, _processing, _audio, _followers, _no_image,
                                    _no_dims, _no_media],
                         ids=lambda change: change.__name__.lstrip('_'))
def test_anything_else_is_not_a_peertube_video(video_post, change):
    change(video_post)
    assert uses_peertube_video(video_post) is False


def test_the_ready_video_is_a_peertube_video(video_post):
    assert uses_peertube_video(video_post) is True


def test_a_text_post_in_a_video_channel_is_a_page(video_post):
    video_post.type = POST_TYPE_ARTICLE
    assert post_to_page(video_post)['type'] == 'Page'


def test_the_icon_size_survives_make_image_sizes_rewriting_the_file(video_post):
    # make_image_sizes_async later writes the 1200 px medium size into File.width/height; the url is not resized
    video_post.image.width, video_post.image.height = 1200, 675
    icon = peertube_video(video_post)['icon'][0]
    assert (icon['width'], icon['height']) == (640, 360)


@pytest.mark.parametrize('key', ['uuid', 'duration_s', 'height', 'bytes', 'thumbnail_width', 'thumbnail_height'])
def test_a_record_missing_a_key_the_builder_needs_is_a_page(video_post, key):
    with_media(video_post, **{key: None})
    assert uses_peertube_video(video_post) is False
    assert post_to_page(video_post)['type'] == 'Page'


@pytest.mark.parametrize('key', ['duration_s', 'height', 'bytes', 'thumbnail_width', 'thumbnail_height'])
@pytest.mark.parametrize('value', [1.5, 10.0, '10', True, 0, -3])
def test_a_numeric_field_that_is_not_a_positive_int_makes_a_page(video_post, key, value):
    with_media(video_post, **{key: value})
    assert uses_peertube_video(video_post) is False
    assert post_to_page(video_post)['type'] == 'Page'


@pytest.mark.parametrize('field, value', [('width', 1.5), ('width', '640'), ('width', True), ('width', -1),
                                          ('fps', 29.97), ('fps', '30'), ('fps', True), ('fps', -30)])
def test_an_odd_width_or_fps_is_left_out_of_the_file_link(video_post, field, value):
    with_media(video_post, **{field: value})
    assert field not in peertube_video(video_post)['url'][0]
