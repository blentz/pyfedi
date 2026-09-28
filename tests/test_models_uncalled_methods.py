"""Model methods nothing calls, and whether they would work if something did.

D1365. A sweep of `app/models.py` for methods with no reference anywhere in `app/`
(python or templates) found twelve. Four of them could not have run at all:

  * `PostReply.child_replies` read `db.session(PostReply)` -- calling the scoped
    session rather than `db.session.query(...)`, which is what `has_replies` two
    lines below does with the same filter. Repaired, because the method has an
    obvious meaning and a correct sibling to copy.
  * `User.expires_soon`, `User.is_expired` and `User.expired_ages_ago` each read
    `self.expires`. `User` has no such column -- the only `expires` in the file
    belongs to the commented-out `IngressQueue` model -- so all three raised
    `AttributeError`, and all three date from the initial commit. Deleted, because
    there was no caller to preserve and nothing to point them at; repairing them
    would have meant inventing semantics.

This file covers the repaired one and asserts the deleted three are gone, so that
re-adding a method reading a column that does not exist has to be deliberate.

The other eight (`Site.active_now`, `User.get_by_email`, `Post.get_by_slug`,
`Community.has_followers_from_domain`, `Feed.has_followers_from_domain`,
`Instance.post_replies_count`, `Instance.votes_are_public`,
`Post.post_reply_count_recalculate`) are uncalled but WORK, so they are left alone --
`has_followers_from_domain` and `post_reply_count_recalculate` are covered here too,
since they are the two whose queries could rot unnoticed.
"""
import pytest
from flask import g

from app import db
from app.models import Community, Feed, Post, PostReply, Site, User, utcnow


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    db.session.commit()
    return SimpleNamespace(baseline=api_baseline,
                           post=db.session.get(Post, api_baseline.post1.id),
                           reply=db.session.get(PostReply, api_baseline.reply1.id))


def a_reply(env, parent=None, deleted=False, body='a child'):
    reply = PostReply(user_id=env.baseline.user2.id, post_id=env.post.id,
                      community_id=env.post.community_id, body=body,
                      body_html=f'<p>{body}</p>', posted_at=utcnow(),
                      deleted=deleted,
                      parent_id=parent.id if parent is not None else None)
    db.session.add(reply)
    db.session.commit()
    return reply


class TestChildReplies:
    """The repair. Every assertion here failed with `TypeError` before it."""

    def test_a_reply_with_no_children(self, env):
        assert env.reply.child_replies() == []

    def test_one_child(self, env):
        child = a_reply(env, parent=env.reply)

        assert [reply.id for reply in env.reply.child_replies()] == [child.id]

    def test_several_children(self, env):
        children = [a_reply(env, parent=env.reply, body=f'child {n}')
                    for n in range(3)]

        assert sorted(reply.id for reply in env.reply.child_replies()) == \
            sorted(child.id for child in children)

    def test_only_direct_children(self, env):
        """`parent_id`, not `root_id`: a grandchild belongs to its own parent."""
        child = a_reply(env, parent=env.reply)
        a_reply(env, parent=child, body='a grandchild')

        assert [reply.id for reply in env.reply.child_replies()] == [child.id]

    def test_a_deleted_child_is_included(self, env):
        """`child_replies` does not filter on `deleted`, unlike `has_replies`,
        which takes an `include_deleted` argument. Asserted rather than assumed,
        since the two methods are neighbours and read almost the same query."""
        child = a_reply(env, parent=env.reply, deleted=True)

        assert [reply.id for reply in env.reply.child_replies()] == [child.id]

    def test_another_replys_children_are_not_included(self, env):
        other = a_reply(env, body='a sibling')
        a_reply(env, parent=other, body='the siblings child')

        assert env.reply.child_replies() == []


class TestHasReplies:
    """`child_replies`' neighbour, for the contrast the test above describes."""

    def test_no_replies(self, env):
        assert env.reply.has_replies() is False

    def test_one_reply(self, env):
        a_reply(env, parent=env.reply)

        assert env.reply.has_replies() is True

    def test_a_deleted_reply_does_not_count(self, env):
        a_reply(env, parent=env.reply, deleted=True)

        assert env.reply.has_replies() is False

    def test_a_deleted_reply_counts_when_asked_for(self, env):
        a_reply(env, parent=env.reply, deleted=True)

        assert env.reply.has_replies(include_deleted=True) is True


class TestTheDeletedExpiryMethods:
    """Three methods that read `User.expires`, a column this model does not have."""

    @pytest.mark.parametrize('name', ['expires_soon', 'is_expired',
                                      'expired_ages_ago'])
    def test_it_is_gone(self, name):
        assert not hasattr(User, name), (
            f'User.{name} is back. It read `self.expires`, which is not a column '
            'on this model; if it has been restored it needs one.')

    def test_the_model_still_has_no_expires_column(self):
        """The reason they could not work. If an `expires` column is ever added,
        this fails and whoever added it can decide what the three methods should
        have meant."""
        assert 'expires' not in {column.name for column in User.__table__.columns}


class TestTheUncalledMethodsThatDoWork:
    """Two of the eight left alone, chosen because their queries are the ones that
    could rot without anybody noticing."""

    def test_post_reply_count_recalculate_counts_live_replies(self, env):
        """Asserted on the COLUMN, read back after a commit.

        The first version of this test asserted `env.post.post_reply_count` and
        passed against the defect: that name is not a column on Post, so the method
        set a stray Python attribute and the test read the same stray attribute
        back. `reply_count` is Post's column, and a refresh is what proves the
        recount reached it.
        """
        a_reply(env)
        a_reply(env, deleted=True)
        env.post.reply_count = 99
        db.session.commit()

        env.post.post_reply_count_recalculate()
        db.session.commit()
        db.session.refresh(env.post)

        # api_baseline's own reply1 is on this post as well
        assert env.post.reply_count == 2
        assert not hasattr(env.post, 'post_reply_count') or \
            'post_reply_count' not in {c.name for c in env.post.__table__.columns}

    def test_a_communitys_followers_from_a_domain(self, env):
        from tests.factories import (make_community, make_community_member,
                                     make_instance, make_user)

        community = make_community('followed')
        instance = make_instance('followers.test')
        member = make_user(instance, 'somebody')
        db.session.commit()
        make_community_member(member, community)

        assert community.has_followers_from_domain('followers.test') is True
        assert community.has_followers_from_domain('nobody.test') is False

    def test_a_banned_member_does_not_count_as_a_follower(self, env):
        from app.models import CommunityMember
        from tests.factories import (make_community, make_community_member,
                                     make_instance, make_user)

        community = make_community('followed2')
        instance = make_instance('banned.test')
        member = make_user(instance, 'banished')
        db.session.commit()
        membership = make_community_member(member, community)
        membership.is_banned = True
        db.session.commit()

        assert community.has_followers_from_domain('banned.test') is False

    def test_a_feeds_banned_member_does_not_count_as_a_follower(self, env):
        """`FeedMember.is_banned == False` in `has_followers_from_domain`, which the
        Community version of this test already covers for its own method. Its mutant
        survived the first pass -- the feed tests for the other method are about
        `following_instances`, and these two filters are separate code."""
        from tests.factories import (make_feed, make_feed_member, make_instance,
                                     make_user)

        instance = make_instance('bannedfollower.test')
        member = make_user(instance, 'bannedfollower')
        feed = make_feed(env.baseline.instance_local, 'bannedfollowerfeed')
        db.session.commit()
        membership = make_feed_member(member, feed)
        membership.is_banned = True
        db.session.commit()

        assert feed.has_followers_from_domain('bannedfollower.test') is False

    def test_a_feeds_followers_from_a_domain(self, env):
        """The Feed copy of the same method, separate and byte-identical."""
        from tests.factories import (make_feed, make_feed_member, make_instance,
                                     make_user)

        instance = make_instance('feedfollowers.test')
        member = make_user(instance, 'feedfan')
        feed = make_feed(env.baseline.instance_local, 'followedfeed')
        db.session.commit()
        make_feed_member(member, feed)  # (user, feed), not (feed, user)

        assert feed.has_followers_from_domain('feedfollowers.test') is True
        assert feed.has_followers_from_domain('nobody.test') is False


class TestAPropertyForTheWholeSweep:
    """What found these: a method nothing calls is never executed, so an attribute
    that does not exist sits there until somebody wires it up.

    The scan reads every `self.<name>` inside a model class and every
    `<ModelClass>.<name>` anywhere in `app/models.py`, and asks the REAL class
    whether it has that attribute. Runtime `hasattr` rather than a parse of the class
    body, because a relationship can arrive by `backref` from the other side
    (`Conversation.members`) and an attribute can be inherited from a mixin
    (`User.is_authenticated` from Flask-Login's `UserMixin`) -- both legitimate, and
    both invisible to an AST-only view.

    It catches exactly what this round repaired: `self.expires` on a model with no
    such column, and `FeedMember.community_id` on a model whose column is `feed_id`.
    """

    def test_no_model_code_reads_an_attribute_the_class_does_not_have(self, app):
        import ast
        from pathlib import Path

        from app import models

        source = Path('app/models.py').read_text(encoding='utf8')
        tree = ast.parse(source)
        classes = {name: obj for name, obj in vars(models).items()
                   if isinstance(obj, type)}

        offenders = []
        for klass in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            runtime = classes.get(klass.name)
            for node in ast.walk(klass):
                if not isinstance(node, ast.Attribute):
                    continue
                if not isinstance(node.value, ast.Name):
                    continue
                if node.value.id == 'self':
                    owner, label = runtime, f'self.{node.attr}'
                else:
                    owner, label = classes.get(node.value.id), \
                        f'{node.value.id}.{node.attr}'
                if owner is None:
                    continue
                if not hasattr(owner, node.attr):
                    offenders.append(f'{klass.name}: {label} (line {node.lineno})')

        assert offenders == [], (
            'these read an attribute the class does not have: '
            + ', '.join(sorted(set(offenders))))


class TestTheFeedsQueriesThatCouldNotRun:
    """`Feed.following_instances` and `Feed.has_followers_from_domain`, both of which
    filtered `FeedMember.community_id`. Only the Community versions have callers, so
    neither AttributeError had ever been raised."""

    def a_feed_with_a_member(self, env, domain='feedinstances.test',
                             dormant=False, banned=False):
        from tests.factories import (make_feed, make_feed_member, make_instance,
                                     make_user)

        instance = make_instance(domain)
        instance.dormant = dormant
        member = make_user(instance, f'member_{domain.replace(".", "_")}')
        feed = make_feed(env.baseline.instance_local, f'feed_{domain.split(".")[0]}')
        db.session.commit()
        membership = make_feed_member(member, feed)
        membership.is_banned = banned
        db.session.commit()
        return feed, instance

    def test_following_instances_finds_a_members_instance(self, env):
        feed, instance = self.a_feed_with_a_member(env)

        assert instance.id in [found.id for found in feed.following_instances()]

    def test_following_instances_skips_a_dormant_instance(self, env):
        feed, instance = self.a_feed_with_a_member(env, domain='dormant.test',
                                                  dormant=True)

        assert feed.following_instances() == []

    def test_following_instances_includes_a_dormant_one_when_asked(self, env):
        feed, instance = self.a_feed_with_a_member(env, domain='dormant2.test',
                                                  dormant=True)

        assert instance.id in [found.id for found in
                               feed.following_instances(include_dormant=True)]

    def test_following_instances_skips_a_banned_member(self, env):
        feed, instance = self.a_feed_with_a_member(env, domain='bannedfeed.test',
                                                  banned=True)

        assert feed.following_instances() == []

    def test_following_instances_excludes_this_instance(self, env):
        """`Instance.id != 1`: a feed's local members are not somewhere to federate
        to."""
        from tests.factories import make_feed, make_feed_member, make_user

        local_member = make_user(env.baseline.instance_local, 'localfeedfan',
                                 local=True)
        feed = make_feed(env.baseline.instance_local, 'localonlyfeed')
        db.session.commit()
        make_feed_member(local_member, feed)
        db.session.commit()

        assert [found.id for found in feed.following_instances()] == []

    def test_another_feeds_members_are_not_included(self, env):
        """What the wrong column cost: `community_id` on a table that has none. Had
        it been a real column the filter would have matched the wrong rows, which is
        why this asserts the scoping rather than only that the query runs."""
        feed, instance = self.a_feed_with_a_member(env, domain='mine.test')
        other, other_instance = self.a_feed_with_a_member(env, domain='theirs.test')

        found = [row.id for row in feed.following_instances()]

        assert instance.id in found
        assert other_instance.id not in found
