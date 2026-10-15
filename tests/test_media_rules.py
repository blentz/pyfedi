import os
import pytest
from app.constants import COMMUNITY_KIND_PODCAST, COMMUNITY_KIND_VIDEO_CHANNEL
from app.shared.media import (can_upload_media, media_kind_for, media_of, sniff_media_mime,
                              upload_byte_limit, with_media)
from app.utils import get_setting, set_setting
from tests.factories import make_community, make_community_member, make_instance, make_post, make_user

FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')


def head(name):
    with open(os.path.join(FIX, name), 'rb') as f:
        return f.read(64)


@pytest.mark.parametrize('name, kind, mime', [
    ('video_h264.mp4', 'video', 'video/mp4'), ('video_vp9.webm', 'video', 'video/webm'),
    ('audio.mp3', 'audio', 'audio/mpeg'), ('audio_cover.mp3', 'audio', 'audio/mpeg'),
    ('audio.m4a', 'audio', 'audio/mp4'), ('audio_opus.ogg', 'audio', 'audio/ogg'),
])
def test_real_files_sniff_to_their_type(name, kind, mime):
    assert sniff_media_mime(head(name), kind) == mime


@pytest.mark.parametrize('name, kind', [
    ('video_h264.mp4', 'audio'), ('audio.mp3', 'video'), ('audio.m4a', 'video'),
    ('audio_opus.ogg', 'video'), ('video_vp9.webm', 'audio'), ('not_media.mp4', 'video'),
    ('not_media.mp4', 'audio'),
])
def test_wrong_kind_or_not_media_is_refused(name, kind):
    assert sniff_media_mime(head(name), kind) is None


@pytest.mark.parametrize('data, kind, mime', [
    (b'\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00', 'video', 'video/quicktime'),
    (b'\x00\x00\x00\x14ftypM4B \x00\x00\x00\x00', 'audio', 'audio/mp4'),
    (b'\xff\xfb\x90\x00' + b'\x00' * 60, 'audio', 'audio/mpeg'),          # bare MPEG frame sync
    (b'\x1a\x45\xdf\xa3' + b'\x00' * 20 + b'matroska', 'video', None),   # Matroska is not WebM
    (b'', 'video', None), (b'\x00\x00\x00\x14ftyp', 'video', None),     # too short for a brand
    (b'\xff\x00', 'audio', None),
])
def test_edge_headers(data, kind, mime):
    assert sniff_media_mime(data, kind) == mime


def test_limits_come_from_settings(db_session):
    assert upload_byte_limit('video') == 524288000 and upload_byte_limit('audio') == 209715200
    set_setting('max_video_upload_bytes', 1000)
    assert upload_byte_limit('video') == 1000


def test_kind_for_community():
    class C:   # plain stand-in: kind is the only attribute read
        kind = COMMUNITY_KIND_VIDEO_CHANNEL
    assert media_kind_for(C) == 'video'
    C.kind = COMMUNITY_KIND_PODCAST
    assert media_kind_for(C) == 'audio'
    C.kind = 'discussion'
    assert media_kind_for(C) is None


def test_only_a_permitted_moderator_of_a_media_community_may_upload(db_session):
    make_instance('test.piefed.local', software='piefed')
    make_user(None, 'first', local=True)   # occupies id 1
    mod = make_user(None, 'mod', local=True)
    other = make_user(None, 'other', local=True)
    video = make_community('chan')
    video.kind = COMMUNITY_KIND_VIDEO_CHANNEL
    podcast = make_community('cast')
    podcast.kind = COMMUNITY_KIND_PODCAST
    plain = make_community('talk')
    for community in (video, podcast, plain):
        make_community_member(mod, community, is_moderator=True)
        make_community_member(other, community)
    saved = {n: get_setting(n) for n in ('allow_video_file_uploads', 'allow_audio_file_uploads')}
    try:
        set_setting('allow_video_file_uploads', 'users')
        set_setting('allow_audio_file_uploads', 'users')
        assert can_upload_media(mod, video) is True
        assert can_upload_media(other, video) is False
        assert can_upload_media(mod, podcast) is True
        assert can_upload_media(mod, plain) is False
        set_setting('allow_video_file_uploads', 'no')
        assert can_upload_media(mod, video) is False
        assert can_upload_media(mod, podcast) is True
    finally:
        for name, value in saved.items():
            set_setting(name, value)


def test_media_record_is_merged_into_a_new_dict(db_session):
    make_instance('test.piefed.local', software='piefed')
    user = make_user(None, 'author', local=True)
    post = make_post(make_community('chan'), user, 'https://test.piefed.local/post/1')
    assert media_of(post) is None
    post.extensions = {'podcast': {'credits': []}}
    original = post.extensions
    with_media(post, status='processing', uuid='u')
    assert media_of(post) == {'status': 'processing', 'uuid': 'u'}
    with_media(post, status='ready')
    assert post.extensions == {'podcast': {'credits': []}, 'media': {'status': 'ready', 'uuid': 'u'}}
    assert post.extensions is not original
    assert original == {'podcast': {'credits': []}}


def test_media_record_tolerates_non_dict_extensions(db_session):
    class P:
        extensions = None
    post = P()
    assert media_of(post) is None
    with_media(post, status='processing')
    assert post.extensions == {'media': {'status': 'processing'}}
    post.extensions = {'media': 'garbage'}
    assert media_of(post) is None
    post.extensions = ['x']
    assert media_of(post) is None
