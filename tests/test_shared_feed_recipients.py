"""Who a feed change is announced to: `remote_subscriber_inboxes` in `app/shared/feed.py`.

D1369. This loop was inside `announce_feed_delete_to_subscribers`, which ran as a
celery task after `delete_feed` had deleted the feed and its `FeedMember` rows and
committed -- so it found none of them. It is a function of its own now, called while
the rows still exist, and its result is passed to the task.

The rules are the ones the loop had, and each is asserted here because each was
previously reachable only through a task that could not run in production.
"""
import pytest
from unittest.mock import patch

from app import db
from app.models import FeedMember, User
from app.shared.feed import remote_subscriber_inboxes
from tests.factories import (make_feed, make_feed_member, make_instance,
                             make_local_feed, make_user, seed_community_owner)


@pytest.fixture
def scene(app, db_session):
    """A local public feed with an owner, and an instance remote members live on."""
    from types import SimpleNamespace

    instance = seed_community_owner('remote.example')
    owner = make_user(instance, 'feedowner')
    feed = make_feed(instance, 'recipientfeed')
    feed.user_id = owner.id
    feed.public = True
    db.session.commit()
    return SimpleNamespace(instance=instance, owner=owner, feed=feed)


def a_remote_member(scene, name, domain='remote.example', inbox=None, online=True,
                    dormant=False, gone=False):
    instance = (scene.instance if domain == 'remote.example'
                else make_instance(domain))
    instance.inbox = inbox if inbox is not None else f'https://{domain}/inbox'
    instance.dormant = dormant
    instance.gone_forever = gone
    member = make_user(instance, name, local=False)
    db.session.commit()
    make_feed_member(member, scene.feed)
    db.session.commit()
    return member


def inboxes(scene, banned=False):
    with patch('app.shared.feed.instance_banned', return_value=banned):
        return remote_subscriber_inboxes(scene.feed)


class TestWhoIsIncluded:
    def test_a_remote_member(self, scene):
        a_remote_member(scene, 'remoteone')

        assert inboxes(scene) == ['https://remote.example/inbox']

    def test_a_feed_with_no_members(self, scene):
        assert inboxes(scene) == []

    def test_two_members_on_different_instances(self, scene):
        a_remote_member(scene, 'onehere')
        a_remote_member(scene, 'onethere', domain='other.example')

        assert sorted(inboxes(scene)) == ['https://other.example/inbox',
                                          'https://remote.example/inbox']

    def test_two_members_on_the_same_instance_give_one_inbox(self, scene):
        """One request per instance, not per subscriber."""
        a_remote_member(scene, 'first')
        a_remote_member(scene, 'second')

        assert inboxes(scene) == ['https://remote.example/inbox']


class TestWhoIsSkipped:
    def test_the_feeds_owner(self, scene):
        """The owner is doing the deleting; they do not need telling."""
        make_feed_member(scene.owner, scene.feed)
        db.session.commit()

        assert inboxes(scene) == []

    def test_a_local_member(self, scene):
        local = make_user(scene.instance, 'localfan', local=True)
        db.session.commit()
        make_feed_member(local, scene.feed)
        db.session.commit()

        assert inboxes(scene) == []

    def test_an_instance_with_no_inbox(self, scene):
        a_remote_member(scene, 'noinbox', inbox='')

        assert inboxes(scene) == []

    def test_a_banned_instance(self, scene):
        a_remote_member(scene, 'bannedone')

        assert inboxes(scene, banned=True) == []

    def test_a_dormant_instance(self, scene):
        """`instance.online()` is False for a dormant instance, so it is not sent to."""
        a_remote_member(scene, 'dormantone', dormant=True)

        assert inboxes(scene) == []

    def test_an_instance_that_is_gone_for_good(self, scene):
        a_remote_member(scene, 'goneone', gone=True)

        assert inboxes(scene) == []

    def test_a_member_whose_user_row_has_gone(self, scene, monkeypatch):
        """`member_user is None`. `FeedMember.user_id` has a foreign key, so a
        membership naming a user that does not exist cannot be committed -- the
        absence is simulated at the session, the way round 145 does for `Post.new`'s
        author lookup (fact 653).

        The first version of this test deleted the membership and re-added it, which
        left the USER in place, so the guard never ran and its mutant survived.
        """
        member = a_remote_member(scene, 'vanishing')
        real_get = db.session.get

        def get(model, identity, *arguments, **keywords):
            if model is User and identity == member.id:
                return None
            return real_get(model, identity, *arguments, **keywords)

        monkeypatch.setattr(db.session, 'get', get)

        assert inboxes(scene) == []


class TestOnlyThisFeed:
    def test_another_feeds_member_is_not_included(self, scene):
        other = make_feed(scene.instance, 'otherfeed')
        other.user_id = scene.owner.id
        db.session.commit()
        outsider = make_user(make_instance('elsewhere.example'), 'outsider',
                             local=False)
        outsider.instance.inbox = 'https://elsewhere.example/inbox'
        db.session.commit()
        make_feed_member(outsider, other)
        a_remote_member(scene, 'insider')

        assert inboxes(scene) == ['https://remote.example/inbox']
