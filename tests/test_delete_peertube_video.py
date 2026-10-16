"""delete_object for a video in a local video_channel community: PeerTube takes the Delete only from the channel's
owner account, and a restore as the author's Create again (spec 2026-10-14 §10 A15.6)."""
import pytest

from app import db
from app.activitypub.serializers import peertube_video
from app.constants import POST_TYPE_ARTICLE
from app.shared.tasks.deletes import delete_object
from tests.factories import make_user
from tests.peertube_video_fixtures import _example_hosts_resolve_without_a_resolver, sent, video_post    # noqa: F401


@pytest.fixture
def owner(video_post):
    user = make_user(video_post.author.instance, 'owner', local=True, with_keys=True)
    video_post.community.user_id = user.id
    db.session.commit()
    return user


@pytest.fixture
def moderator(video_post):
    return make_user(video_post.author.instance, 'mod', local=True, with_keys=True)


def test_peertube_gets_the_owners_delete(sent, video_post, owner):
    delete_object(video_post.author.id, video_post, is_post=True, session=db.session)
    body, key = sent['https://peertube.example/inbox']
    assert (body['type'], body['actor'], body['object']) == ('Delete', owner.public_url(), video_post.public_url())
    assert key == owner.public_url() + '#main-key'


def test_a_moderators_removal_is_still_signed_by_the_owner(sent, video_post, owner, moderator):
    delete_object(moderator.id, video_post, is_post=True, reason='spam', session=db.session)
    assert sent['https://peertube.example/inbox'][0]['actor'] == owner.public_url()


def test_other_followers_keep_the_communitys_announce(sent, video_post, owner):
    delete_object(video_post.author.id, video_post, is_post=True, session=db.session)
    assert sent['https://lemmy.example/inbox'][0]['type'] == 'Announce'
    assert sent['https://mastodon.example/inbox'][0]['type'] == 'Announce'


def test_without_an_owner_peertube_gets_nothing(sent, video_post):
    video_post.community.user_id = None
    db.session.commit()
    delete_object(video_post.author.id, video_post, is_post=True, session=db.session)
    assert 'https://peertube.example/inbox' not in sent
    assert 'https://lemmy.example/inbox' in sent


def test_a_restore_sends_peertube_the_create_again(sent, video_post):
    delete_object(video_post.author.id, video_post, is_post=True, is_restore=True, session=db.session)
    body, key = sent['https://peertube.example/inbox']
    assert (body['type'], body['actor']) == ('Create', video_post.author.public_url())
    assert body['object'] == peertube_video(video_post)
    assert key == video_post.author.public_url() + '#main-key'


def test_a_page_post_keeps_the_announce_for_peertube(sent, video_post, owner):
    video_post.type = POST_TYPE_ARTICLE
    delete_object(video_post.author.id, video_post, is_post=True, session=db.session)
    assert sent['https://peertube.example/inbox'][0]['type'] == 'Announce'
