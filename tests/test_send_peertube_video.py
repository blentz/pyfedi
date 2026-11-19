"""send_post for a ready video in a local video_channel community pushes the Video peertube_video builds (spec
2026-10-14 §6.1, amended §10 A15.5)."""
from app import db
from app.activitypub.serializers import peertube_video
from app.constants import POST_TYPE_ARTICLE
from app.shared.media import with_media
from app.shared.tasks.pages import send_post
from tests.factories import make_follow, make_instance, make_instance_block, make_user
from tests.peertube_video_fixtures import _example_hosts_resolve_without_a_resolver


def test_peertube_gets_the_authors_create_of_the_video(peertube_video_sent, peertube_video_video_post):
    send_post(peertube_video_video_post.id, session=db.session)
    body, key = peertube_video_sent['https://peertube.example/inbox']
    assert body['type'] == 'Create' and body['actor'] == peertube_video_video_post.author.public_url()
    assert body['object'] == peertube_video(peertube_video_video_post)
    assert key == peertube_video_video_post.author.public_url() + '#main-key'


def test_a_microblog_app_gets_the_communitys_announce_of_the_id(peertube_video_sent, peertube_video_video_post):
    send_post(peertube_video_video_post.id, session=db.session)
    body, key = peertube_video_sent['https://mastodon.example/inbox']
    assert (body['type'], body['object']) == ('Announce', peertube_video_video_post.ap_id)
    assert key == peertube_video_video_post.community.public_url() + '#main-key'


def test_the_threadiverse_gets_the_announce_of_the_create_with_audience(peertube_video_sent, peertube_video_video_post):
    send_post(peertube_video_video_post.id, session=db.session)
    body, key = peertube_video_sent['https://lemmy.example/inbox']
    audience = peertube_video_video_post.community.public_url()
    assert body['type'] == 'Announce' and body['object']['type'] == 'Create'
    assert body['object']['audience'] == audience and body['object']['object']['audience'] == audience
    assert body['object']['object']['type'] == 'Video'
    assert key == audience + '#main-key'


def test_followers_and_mentions_get_the_authors_create_once(peertube_video_sent, peertube_video_video_post):
    send_post(peertube_video_video_post.id, session=db.session)
    for inbox in ('https://follower.example/inbox', 'https://mention.example/inbox'):
        assert peertube_video_sent[inbox][0]['type'] == 'Create'
    assert len(peertube_video_sent.calls) == 5            # one per instance


def test_an_edit_is_the_authors_update_to_peertube_and_microblog_apps(peertube_video_sent, peertube_video_video_post):
    send_post(peertube_video_video_post.id, edit=True, session=db.session)
    assert peertube_video_sent['https://peertube.example/inbox'][0]['type'] == 'Update'
    assert peertube_video_sent['https://mastodon.example/inbox'][0]['type'] == 'Update'
    assert peertube_video_sent['https://lemmy.example/inbox'][0]['object']['type'] == 'Update'
    assert len(peertube_video_sent.calls) == 5


def test_an_undeliverable_instance_is_skipped(peertube_video_sent, peertube_video_video_post):
    make_instance_block(peertube_video_video_post.author, make_instance('blocked.example'))
    peertube = next(i for i in peertube_video_video_post.community.following_instances() if i.software == 'peertube')
    make_instance_block(peertube_video_video_post.author, peertube)
    send_post(peertube_video_video_post.id, session=db.session)
    assert 'https://peertube.example/inbox' not in peertube_video_sent
    assert len(peertube_video_sent.calls) == 4


def test_a_processing_video_is_not_sent(peertube_video_sent, peertube_video_video_post):
    with_media(peertube_video_video_post, status='processing')
    send_post(peertube_video_video_post.id, session=db.session)
    assert not peertube_video_sent.calls


def test_a_text_post_in_a_video_channel_keeps_the_page_path(peertube_video_sent, peertube_video_video_post):
    peertube_video_video_post.type = POST_TYPE_ARTICLE
    send_post(peertube_video_video_post.id, session=db.session)
    assert peertube_video_sent['https://lemmy.example/inbox'][0]['object']['object']['type'] == 'Page'


def test_an_instance_already_sent_the_announce_gets_nothing_more_as_an_author_follower(peertube_video_sent, peertube_video_video_post):
    lemmy = next(i for i in peertube_video_video_post.community.following_instances() if i.software == 'lemmy')
    make_follow(peertube_video_video_post.author, make_user(lemmy, 'lefan'), is_inward=True)
    send_post(peertube_video_video_post.id, session=db.session)
    assert len(peertube_video_sent.calls) == 5 and peertube_video_sent['https://lemmy.example/inbox'][0]['type'] == 'Announce'


def test_an_undeliverable_author_follower_instance_is_skipped(peertube_video_sent, peertube_video_video_post):
    blocked = make_instance('blocked.example')
    blocked.inbox = 'https://blocked.example/inbox'
    make_follow(peertube_video_video_post.author, make_user(blocked, 'bl'), is_inward=True)
    make_instance_block(peertube_video_video_post.author, blocked)
    send_post(peertube_video_video_post.id, session=db.session)
    assert 'https://blocked.example/inbox' not in peertube_video_sent and len(peertube_video_sent.calls) == 5
