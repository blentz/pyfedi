"""D3 (residuals WP-D): no activity about a followers-only object is relayed to a community's followers -- Like,
Dislike, Delete, Flag, Move, Undo-Delete, Undo-vote, poll vote and answer-chosen -- as process_new_content already
refuses to relay its Create. Each arm still does its local work; a public object is still relayed."""
import contextlib
from unittest.mock import patch

import pytest

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.routes import (process_downvote, process_new_content, process_poll_vote,
                                    process_question_answer, process_upvote)
from app.models import Poll, PollChoice
from app.utils import utcnow
from tests.factories import (inbox_activity, make_community, make_instance, make_post, make_post_reply, make_site,
                             make_user, make_visibility_world)
from tests.test_inbox_dispatch_lock_delete import record_moderation
from tests.test_inbox_dispatch_preamble import dispatch
from tests.test_visibility_ingest import FOLLOWERS, PUBLIC, note_activity

VISIBILITIES = [('public', 1), ('unlisted', 1), ('followers', 0)]


class _LockOnlyRedis:
    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def scene(app, db_session, monkeypatch):
    make_site()
    instance = make_instance('peer.example')
    make_user(instance, 'community_owner')
    community = make_community('origincomm', host='peer.example')
    community.ap_fetched_at = utcnow()
    target = make_community('targetcomm', host='peer.example')
    target.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    author.ap_fetched_at = utcnow()
    actor = make_user(instance, 'actor')
    actor.ap_fetched_at = utcnow()
    post = make_post(community, author, ap_id='https://peer.example/objects/1')
    db.session.commit()
    announced = record_moderation(monkeypatch, 'announce_activity_to_followers')['announce_activity_to_followers']
    return type('Scene', (), dict(community=community, target=target, author=author, actor=actor, post=post,
                                  announced=announced))


def _hide(obj, visibility):
    obj.visibility = visibility
    db.session.commit()


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_a_like_or_dislike(scene, visibility, relays):
    _hide(scene.post, visibility)
    process_upvote(scene.actor, False, {'id': 'https://peer.example/a/1', 'object': scene.post.ap_id}, False)
    process_downvote(scene.actor, False, {'id': 'https://peer.example/a/2', 'object': scene.post.ap_id}, False)
    assert len(scene.announced) == 2 * relays


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_a_delete(scene, visibility, relays, monkeypatch):
    monkeypatch.setattr('app.redis_client', _LockOnlyRedis())
    _hide(scene.post, visibility)
    deleted = record_moderation(monkeypatch, 'delete_post_or_comment')['delete_post_or_comment']
    dispatch(inbox_activity(scene.author, activity_type='Delete', object_uri=scene.post.ap_id))
    assert len(deleted) == 1
    assert len(scene.announced) == relays


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_a_flag(scene, visibility, relays, monkeypatch):
    _hide(scene.post, visibility)
    monkeypatch.setattr(activitypub_routes, 'process_report', lambda *a: True)
    dispatch(inbox_activity(scene.actor, activity_type='Flag', object=scene.post.ap_id, summary='spam'))
    assert len(scene.announced) == relays


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_a_move(scene, visibility, relays):
    _hide(scene.post, visibility)
    dispatch(inbox_activity(scene.author, activity_type='Move', object=scene.post.ap_id,
                            origin=scene.community.ap_profile_id, target=scene.target.ap_profile_id))
    assert len(scene.announced) == relays


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_an_undo_delete(scene, visibility, relays, monkeypatch):
    monkeypatch.setattr('app.redis_client', _LockOnlyRedis())
    scene.post.deleted = True
    _hide(scene.post, visibility)
    restored = record_moderation(monkeypatch, 'restore_post_or_comment')['restore_post_or_comment']
    dispatch(inbox_activity(scene.author, activity_type='Undo', object={'type': 'Delete', 'object': scene.post.ap_id}))
    assert len(restored) == 1
    assert len(scene.announced) == relays


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_an_undo_vote(scene, visibility, relays, monkeypatch):
    _hide(scene.post, visibility)
    post = scene.post
    monkeypatch.setattr(activitypub_routes, 'undo_vote', lambda comment, post_, target_ap_id, user: post)
    dispatch(inbox_activity(scene.actor, activity_type='Undo', object={'type': 'Like', 'object': post.ap_id}))
    assert len(scene.announced) == relays


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_a_poll_vote(scene, visibility, relays):
    _hide(scene.post, visibility)
    db.session.add(Poll(post_id=scene.post.id, mode='single', local_only=False, latest_vote=None))
    db.session.add(PollChoice(post_id=scene.post.id, choice_text='yes', sort_order=0, num_votes=0))
    db.session.commit()
    vote = {'id': 'https://peer.example/a/3', 'object': scene.post.ap_id, 'choice_text': 'yes'}
    process_poll_vote(scene.actor, False, vote, False)
    assert len(scene.announced) == relays


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_an_answer_chosen(scene, visibility, relays, monkeypatch):
    monkeypatch.setattr('app.redis_client', _LockOnlyRedis())
    reply = make_post_reply(scene.post, scene.author, 'the answer')
    reply.ap_id = 'https://peer.example/comment/1'
    _hide(reply, visibility)
    process_question_answer(scene.author, False, {'id': 'https://peer.example/a/4', 'object': reply.ap_id}, False)
    assert db.session.get(type(reply), reply.id).answer is True
    assert len(scene.announced) == relays


# --- process_new_content: the post-Update, new-reply and reply-Update arms (Task 11) ---------------------------
# The new-post arm is pinned in test_visibility_end_to_end.py. An edit or a reply to followers-only content is
# stored and notified, never forwarded to the community's followers; the same arm still forwards a public one.

def _content(url, kind, to, cc, **extra):
    activity = note_activity(to, cc)
    activity['type'] = kind
    activity['id'] = f'{url}/activity'
    activity['object'].update({'id': url, 'attributedTo': 'https://m.example/users/alice', **extra})
    return activity


@contextlib.contextmanager
def _relay_spy():
    with patch('app.activitypub.routes.can_create_post', return_value=True), \
            patch('app.activitypub.routes.can_create_post_reply', return_value=True), \
            patch('app.activitypub.routes.update_post_from_activity'), \
            patch('app.activitypub.routes.update_post_reply_from_activity'), \
            patch('app.activitypub.routes.announce_activity_to_followers') as announce:
        yield announce


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_a_post_update(app, db_session, visibility, relays):
    w = make_visibility_world()
    post = w.post if visibility == 'followers' else w.public_post
    post.visibility = visibility
    db.session.commit()
    activity = _content(post.ap_id, 'Update', [PUBLIC] if relays else [FOLLOWERS], [])
    with _relay_spy() as announce:
        process_new_content(w.author, w.community, False, activity, False)
    assert announce.call_count == relays


@pytest.mark.parametrize('visibility, relays', VISIBILITIES)
def test_a_reply_update(app, db_session, visibility, relays):
    w = make_visibility_world()
    w.reply.ap_id = 'https://m.example/comment/1'
    w.reply.visibility = visibility
    db.session.commit()
    activity = _content(w.reply.ap_id, 'Update', [PUBLIC] if relays else [FOLLOWERS], [],
                        inReplyTo=w.public_post.ap_id)
    with _relay_spy() as announce:
        process_new_content(w.author, w.community, False, activity, False)
    assert announce.call_count == relays


@pytest.mark.parametrize('to, cc, relays', [([FOLLOWERS], [], 0), ([PUBLIC], [FOLLOWERS], 1), ([PUBLIC], [], 1)],
                         ids=['followers', 'public-addressed-to-followers-too', 'public'])
def test_a_new_reply(app, db_session, to, cc, relays):
    w = make_visibility_world()
    activity = _content('https://m.example/comment/2', 'Create', to, cc, inReplyTo=w.public_post.ap_id)
    with _relay_spy() as announce:
        process_new_content(w.author, w.community, False, activity, False)
    assert announce.call_count == relays
