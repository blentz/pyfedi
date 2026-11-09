"""A local person's boost (owner ruling 2026-11-08: build local boosts; plan W1.3): recorded, and federated as an
Announce from their own actor to their followers' instances and a remote author; taken back with an Undo naming it."""
from itertools import count
from unittest.mock import patch

import pytest

import app.shared.tasks.boosts as boosts
from app import db
from app.constants import SRC_API, VISIBILITY_FOLLOWERS
from app.models import CommunityBan, PostBoost, UserBlock, UserFollower
from app.shared import post as shared_post
from app.utils import Authorised
from tests.factories import make_community, make_instance, make_post, make_user

_SERIAL = count()


@pytest.fixture
def world(app, api_baseline):
    booster = make_user(None, 'booster', local=True, with_keys=True)
    far = make_instance('far.example')
    far.inbox = 'https://far.example/inbox'
    follower = make_user(far, 'farfollower')
    db.session.add(UserFollower(local_user_id=booster.id, remote_user_id=follower.id, is_inward=True,
                                is_accepted=True))
    author_home = make_instance('author.example')
    author = make_user(author_home, 'farauthor')
    author.ap_inbox_url = 'https://author.example/u/farauthor/inbox'
    author.ap_own_inbox_url = author.ap_inbox_url
    community = make_community('boosted')
    db.session.commit()
    post = make_post(community, author, ap_id=f'https://author.example/post/{next(_SERIAL)}')
    return {'booster': booster, 'post': post, 'community': community, 'author': author}


def _sent(world, undo=False):
    with patch.object(boosts, 'send_post_request') as send:
        boosts.send_boost(world['booster'].id, world['post'].id, undo)
    return {call.args[0]: call.args[1] for call in send.call_args_list}


def test_a_boost_is_an_announce_to_followers_and_the_remote_author(world):
    sent = _sent(world)
    assert set(sent) == {'https://far.example/inbox', 'https://author.example/u/farauthor/inbox'}
    announce = sent['https://far.example/inbox']
    assert announce['type'] == 'Announce' and announce['actor'] == world['booster'].public_url()
    assert announce['object'] == world['post'].public_url()
    assert announce['to'] == [boosts.PUBLIC]
    assert announce['cc'] == [world['booster'].followers_url(), world['author'].public_url()]
    assert announce['id'].endswith(f"/activities/announce/boost-{world['post'].id}")


def test_taking_it_back_is_an_undo_naming_the_same_announce(world):
    announce = _sent(world)['https://far.example/inbox']
    undo = _sent(world, undo=True)['https://far.example/inbox']
    assert undo['type'] == 'Undo' and undo['object']['id'] == announce['id']
    assert undo['object']['type'] == 'Announce' and '@context' not in undo['object']


@pytest.mark.parametrize('what', ['followers-only', 'deleted', 'private-community', 'local-only-community'])
def test_a_post_that_may_not_leave_is_never_announced(world, what):
    post, community = world['post'], world['community']
    if what == 'followers-only':
        post.visibility = VISIBILITY_FOLLOWERS
    elif what == 'deleted':
        post.deleted = True
    elif what == 'private-community':
        community.private = True
    else:
        community.local_only = True
    db.session.commit()
    assert _sent(world) == {}
    assert not boosts.boostable(post)


def test_nobody_or_no_post_sends_nothing(world):
    with patch.object(boosts, 'send_post_request') as send:
        boosts.send_boost(2 ** 31 - 1, world['post'].id, False)
        boosts.send_boost(world['booster'].id, 2 ** 31 - 1, False)
    send.assert_not_called()


def test_the_shared_functions_record_and_queue_once(world):
    auth = Authorised(world['booster'])
    with patch.object(shared_post, 'queue_boost') as queued:
        shared_post.boost_post(world['post'].id, SRC_API, auth)
        shared_post.boost_post(world['post'].id, SRC_API, auth)   # a second boost changes nothing
        assert PostBoost.query.filter_by(user_id=world['booster'].id).count() == 1
        db.session.expire_all()
        assert len(world['post'].post_boosts) == 1
        shared_post.unboost_post(world['post'].id, SRC_API, auth)
        shared_post.unboost_post(world['post'].id, SRC_API, auth)
    assert PostBoost.query.filter_by(user_id=world['booster'].id).count() == 0
    assert [call.kwargs.get('undo', False) for call in queued.call_args_list] == [False, True]


def test_a_post_that_cannot_leave_is_refused_and_nobodys_is_not_found(world):
    world['post'].visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    with pytest.raises(Exception, match='cannot be boosted'):
        shared_post.boost_post(world['post'].id, SRC_API, Authorised(world['booster']))
    with pytest.raises(Exception):
        shared_post.boost_post(2 ** 31 - 1, SRC_API, Authorised(world['booster']))
    with pytest.raises(Exception):
        shared_post.unboost_post(2 ** 31 - 1, SRC_API, Authorised(world['booster']))


def test_queue_boost_runs_at_once_in_debug_and_as_a_task_otherwise(app, world, monkeypatch):
    with patch.object(boosts, 'send_boost') as at_once, patch.object(boosts.boost_task, 'delay') as later:
        monkeypatch.setitem(app.config, 'DEBUG', True)
        boosts.queue_boost(1, 2)
        monkeypatch.setitem(app.config, 'DEBUG', False)
        boosts.queue_boost(1, 2, undo=True)
        boosts.boost_task(3, 4)   # the task's body, run in place
    at_once.assert_has_calls([((1, 2, False),), ((3, 4, False),)])
    later.assert_called_once_with(1, 2, True)


def test_a_local_authors_post_goes_to_the_followers_alone(world):
    world['post'].user_id = world['booster'].id
    db.session.commit()
    assert set(_sent(world)) == {'https://far.example/inbox'}


def test_an_activity_refused_on_the_way_out_is_not_sent(world):
    with patch.object(boosts, 'refused_outbound', return_value=True):
        assert _sent(world) == {}


def test_a_failed_send_is_raised_for_the_task_to_retry(world):
    with patch.object(boosts, 'send_post_request', side_effect=ConnectionError('down')):
        with pytest.raises(ConnectionError):
            boosts.send_boost(world['booster'].id, world['post'].id, False)


@pytest.mark.parametrize('what', ['followers-only', 'deleted', 'private-community'])
def test_an_undo_goes_whatever_the_post_became(world, what):
    """A retraction must reach the peers that hold the boost, even once the post may not be announced any more."""
    post = world['post']
    if what == 'followers-only':
        post.visibility = VISIBILITY_FOLLOWERS
    elif what == 'deleted':
        post.deleted = True
    else:
        world['community'].private = True
    db.session.commit()
    sent = _sent(world, undo=True)
    assert sent and all(body['type'] == 'Undo' for body in sent.values())


@pytest.mark.parametrize('what', ['banned-from-community', 'blocked-by-author'])
def test_a_person_the_community_or_author_refused_cannot_boost(world, what):
    booster = world['booster']
    if what == 'banned-from-community':
        db.session.add(CommunityBan(community_id=world['community'].id, user_id=booster.id))
    else:
        db.session.add(UserBlock(blocker_id=world['author'].id, blocked_id=booster.id))
    db.session.commit()
    with patch.object(shared_post, 'queue_boost') as queued, pytest.raises(Exception, match='cannot be boosted'):
        shared_post.boost_post(world['post'].id, SRC_API, Authorised(booster))
    queued.assert_not_called()
    assert PostBoost.query.filter_by(user_id=booster.id).count() == 0
