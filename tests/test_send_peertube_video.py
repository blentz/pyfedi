"""send_post for a ready video in a local video_channel community pushes the Video peertube_video builds (spec
2026-10-14 §6.1, amended §10 A15.5)."""
import pytest

from app import db
from app.activitypub.serializers import peertube_video
from app.constants import COMMUNITY_KIND_VIDEO_CHANNEL, POST_TYPE_ARTICLE, POST_TYPE_VIDEO
from app.shared.media import with_media
from app.shared.tasks.pages import send_post
from tests.factories import make_instance, make_instance_block
from tests.peertube_video_fixtures import _example_hosts_resolve_without_a_resolver, sent, video_post    # noqa: F401


def test_peertube_gets_the_authors_create_of_the_video(sent, video_post):
    send_post(video_post.id, session=db.session)
    body, key = sent['https://peertube.example/inbox']
    assert body['type'] == 'Create' and body['actor'] == video_post.author.public_url()
    assert body['object'] == peertube_video(video_post)
    assert key == video_post.author.public_url() + '#main-key'


def test_a_microblog_app_gets_the_communitys_announce_of_the_id(sent, video_post):
    send_post(video_post.id, session=db.session)
    body, key = sent['https://mastodon.example/inbox']
    assert (body['type'], body['object']) == ('Announce', video_post.ap_id)
    assert key == video_post.community.public_url() + '#main-key'


def test_the_threadiverse_gets_the_announce_of_the_create_with_audience(sent, video_post):
    send_post(video_post.id, session=db.session)
    body, key = sent['https://lemmy.example/inbox']
    audience = video_post.community.public_url()
    assert body['type'] == 'Announce' and body['object']['type'] == 'Create'
    assert body['object']['audience'] == audience and body['object']['object']['audience'] == audience
    assert body['object']['object']['type'] == 'Video'
    assert key == audience + '#main-key'


def test_followers_and_mentions_get_the_authors_create_once(sent, video_post):
    send_post(video_post.id, session=db.session)
    for inbox in ('https://follower.example/inbox', 'https://mention.example/inbox'):
        assert sent[inbox][0]['type'] == 'Create'
    assert len(sent.calls) == 5            # one per instance


def test_an_edit_is_the_authors_update_to_peertube_and_microblog_apps(sent, video_post):
    send_post(video_post.id, edit=True, session=db.session)
    assert sent['https://peertube.example/inbox'][0]['type'] == 'Update'
    assert sent['https://mastodon.example/inbox'][0]['type'] == 'Update'
    assert sent['https://lemmy.example/inbox'][0]['object']['type'] == 'Update'
    assert len(sent.calls) == 5


def test_an_undeliverable_instance_is_skipped(sent, video_post):
    make_instance_block(video_post.author, make_instance('blocked.example'))
    peertube = next(i for i in video_post.community.following_instances() if i.software == 'peertube')
    make_instance_block(video_post.author, peertube)
    send_post(video_post.id, session=db.session)
    assert 'https://peertube.example/inbox' not in sent
    assert len(sent.calls) == 4


def test_a_processing_video_is_not_sent(sent, video_post):
    with_media(video_post, status='processing')
    send_post(video_post.id, session=db.session)
    assert not sent.calls


def test_a_text_post_in_a_video_channel_keeps_the_page_path(sent, video_post):
    video_post.type = POST_TYPE_ARTICLE
    send_post(video_post.id, session=db.session)
    assert sent['https://lemmy.example/inbox'][0]['object']['object']['type'] == 'Page'
