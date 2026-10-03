"""E9: moderation activities about a followers-only object are not federated to the community.

A followers-only object was never sent to the community's followers, so its Delete, Undo(Delete), Lock, Undo(Lock),
Add and Remove (sticky) must not be either -- the outbound mirror of the inbound gates in test_visibility_relays.py.
A public or unlisted object is still federated, for a local community (Announce to each following instance) and for
a remote one (a send to the community's inbox).
"""
from unittest.mock import patch

import pytest

from app import db
from app.community.util import (
    delete_post_from_community_task,
    delete_post_reply_from_community_task,
)
from app.models import ActivityBatch
from app.shared.tasks import adds, deletes, likes, locks, removes
from tests.factories import (
    make_community_member,
    make_instance,
    make_user,
    make_visibility_world,
)
from tests.test_visibility_moderation import (  # noqa: F401  (world is a fixture)
    web,
    world,
)

# (name, task, kind of object it acts on)
TASKS = [
    ('lock_post', locks.lock_post, 'post'),
    ('unlock_post', locks.unlock_post, 'post'),
    ('lock_post_reply', locks.lock_post_reply, 'reply'),
    ('unlock_post_reply', locks.unlock_post_reply, 'reply'),
    ('sticky_post', adds.sticky_post, 'post'),
    ('unsticky_post', removes.unsticky_post, 'post'),
    ('delete_post', deletes.delete_post, 'post'),
    ('restore_post', deletes.restore_post, 'post'),
    ('delete_reply', deletes.delete_reply, 'reply'),
    ('restore_reply', deletes.restore_reply, 'reply'),
]
MODULES = ('app.shared.tasks.locks', 'app.shared.tasks.adds', 'app.shared.tasks.removes', 'app.shared.tasks.deletes')


class Sent(list):
    def __call__(self, inbox, *args, **kwargs):
        self.append(inbox)


@pytest.fixture
def sent(monkeypatch):
    recorder = Sent()
    for module in MODULES:
        monkeypatch.setattr(f'{module}.send_post_request', recorder)
    return recorder


def follow_the_community(w):
    """A remote instance with a member in the community: what following_instances() returns."""
    instance = make_instance('f.example')
    instance.inbox = 'https://f.example/inbox'
    make_community_member(make_user(instance, 'follower_of_community'), w.community)
    db.session.commit()


def make_remote(w):
    home = make_instance('c.example')
    w.community.ap_id = 'c1@c.example'
    w.community.ap_profile_id = 'https://c.example/c/c1'
    w.community.ap_public_url = 'https://c.example/c/c1'
    w.community.ap_inbox_url = 'https://c.example/c/c1/inbox'
    w.community.ap_domain = 'c.example'
    w.community.instance_id = home.id
    db.session.commit()


def scene(db_session, remote):
    w = make_visibility_world()
    w.community.local_only = False
    if remote:
        make_remote(w)
    else:
        follow_the_community(w)
    return w


def target_of(w, kind, open_):
    if kind == 'post':
        return (w.public_post if open_ else w.post).id
    return (w.public_child if open_ else w.reply).id


@pytest.mark.parametrize('remote', [False, True], ids=['local-community', 'remote-community'])
@pytest.mark.parametrize('name, task, kind', TASKS, ids=[t[0] for t in TASKS])
def test_an_open_object_is_federated(db_session, sent, name, task, kind, remote):
    w = scene(db_session, remote)
    task(None, w.stranger.id, target_of(w, kind, open_=True))
    assert sent, name


@pytest.mark.parametrize('remote', [False, True], ids=['local-community', 'remote-community'])
@pytest.mark.parametrize('name, task, kind', TASKS, ids=[t[0] for t in TASKS])
def test_a_followers_only_object_is_not_federated(db_session, sent, name, task, kind, remote):
    w = scene(db_session, remote)
    task(None, w.stranger.id, target_of(w, kind, open_=False))
    assert sent == [], name


@pytest.mark.parametrize('remote', [False, True], ids=['local-community', 'remote-community'])
@pytest.mark.parametrize('visibility, federated', [('public', True), ('unlisted', True), ('followers', False)])
def test_the_author_deleting_their_own_post_or_reply(db_session, remote, visibility, federated):
    w = scene(db_session, remote)
    w.post.visibility = w.reply.visibility = visibility
    w.post.user_id = w.reply.user_id = w.stranger.id
    db.session.commit()
    with patch('app.community.util.send_post_request') as direct, \
            patch('app.community.util.send_to_remote_instance') as announce:
        delete_post_from_community_task(w.post.id, w.stranger.id)
        delete_post_reply_from_community_task(w.reply.id, w.stranger.id)
    assert bool(direct.call_count + announce.call_count) is federated


@pytest.mark.parametrize('remote', [False, True], ids=['local-community', 'remote-community'])
@pytest.mark.parametrize('visibility, federated', [('public', True), ('unlisted', True), ('followers', False)])
def test_a_web_reply_restore(app, world, remote, visibility, federated):  # noqa: F811
    w = world
    w.community.local_only = False
    if remote:
        make_remote(w)
    else:
        follow_the_community(w)
    reply = w.reply
    reply.visibility = visibility
    reply.deleted, reply.deleted_by = True, reply.user_id
    reply.user_id = w.admin.id  # the admin restores their own deletion, so no moderator permission is in question
    reply.deleted_by = w.admin.id
    db.session.commit()
    with patch('app.post.routes.send_post_request') as direct, patch('app.post.routes.send_to_remote_instance') as announce:
        response = web(app, w.admin, f'/post/{w.public_post.id}/comment/{reply.id}/restore', {})
    assert response.status_code in (200, 302)
    db.session.expire_all()
    assert reply.deleted is False
    assert bool(direct.call_count + announce.call_count) is federated


# F2: a vote is an activity about its object too. For a local community the Announce of it to each following instance
# (or the ActivityBatch row a PieFed instance gets instead) is skipped when the object is followers-only.
VOTES = [
    ('like', None, 'upvote', None),
    ('dislike', None, 'downvote', None),
    ('undo', 'Like', 'upvote', None),
    ('emoji', None, 'upvote', 'x'),
]


@pytest.mark.parametrize('software', ['mastodon', 'piefed'])
@pytest.mark.parametrize('kind', ['post', 'reply'])
@pytest.mark.parametrize('open_', [True, False], ids=['open', 'followers-only'])
@pytest.mark.parametrize('name, undo, direction, emoji', VOTES, ids=[v[0] for v in VOTES])
def test_a_vote_in_a_local_community_is_announced_only_for_an_open_object(
        db_session, monkeypatch, name, undo, direction, emoji, open_, kind, software):
    w = scene(db_session, remote=False)
    for instance in w.community.following_instances():
        instance.software = software
    db.session.commit()
    recorder = Sent()
    monkeypatch.setattr('app.shared.tasks.likes.send_post_request', recorder)
    task = likes.vote_for_post if kind == 'post' else likes.vote_for_reply
    task(None, w.stranger.id, target_of(w, kind, open_), undo, direction, emoji=emoji)
    announced = bool(recorder) or ActivityBatch.query.count() > 0
    assert announced is open_, name


@pytest.mark.parametrize('open_', [True, False], ids=['open', 'followers-only'])
def test_a_vote_in_a_remote_community_still_goes_to_its_inbox(db_session, monkeypatch, open_):
    """R-a: the remote community is the object's home and already holds it."""
    w = scene(db_session, remote=True)
    recorder = Sent()
    monkeypatch.setattr('app.shared.tasks.likes.send_post_request', recorder)
    likes.vote_for_post(None, w.stranger.id, target_of(w, 'post', open_), None, 'upvote')
    assert recorder == ['https://c.example/c/c1/inbox']
