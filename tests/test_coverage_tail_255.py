"""Round 255: the counters a ban has to leave correct.

`site_ban_remove_data` and `community_ban_remove_data` delete a banned account's content.
`tests/test_ap_moderation.py` covers which content each one reaches. What it does not cover is
the bookkeeping either side of the deletion, and that bookkeeping is what every listing reads:

    2537-2539 / 2579-2581   `child_count` on every ancestor of a deleted NESTED reply
    2549-2550 / 2590-2591   the cross-post recalculation for a deleted link post

A wrong `child_count` shows a thread with replies that are not there; a stale `cross_posts` array
shows a post cross-posted to somewhere its copy has been deleted. Neither raises, so neither is
visible without a row.

Also here: `find_instance_id`'s IntegrityError arm, which is the race between two inbox workers
inserting the same new peer, and `create_post`'s refusal when a reply names a parent this
instance does not hold.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.activitypub.util import (community_ban_remove_data, find_instance_id,
                                  site_ban_remove_data)
from app.models import Community, Instance, Post, PostReply, Site
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_site, make_user,
                             seed_community_owner)

PEER = 'peer.example'


@pytest.fixture
def scene(app, db_session, monkeypatch):
    """A banned author with a nested reply and a link post, in a community with a moderator.

    `File.delete_from_disk` is neutralised: both functions delete the account's images, and the
    rows here are about the counters rather than the filesystem.
    """
    from app.models import File

    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *args, **kwargs: None)
    make_site()
    instance = seed_community_owner(PEER)
    community = make_community('countland', host='test.piefed.local')
    author = make_user(instance, 'banned_author', local=True)
    moderator = make_user(instance, 'the_moderator', local=True)
    make_community_member(author, community)
    make_community_member(moderator, community, is_moderator=True)
    db.session.commit()
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, instance=instance, community=community,
                           author=author, moderator=moderator)


def a_thread(scene, author, depth_author=None):
    """A post with a top-level reply and a nested reply under it.

    `path` is what the child_count update reads: it is the chain of reply ids from the root
    down, and the update decrements every entry but the last.
    """
    post = make_post(scene.community, scene.moderator,
                     ap_id='https://test.piefed.local/p/1')
    db.session.commit()
    parent = make_post_reply(post, scene.moderator, body='the parent')
    db.session.commit()
    parent.path = [parent.id]
    child = make_post_reply(post, depth_author or author, body='the child')
    db.session.commit()
    child.parent_id = parent.id
    child.root_id = parent.id
    child.path = [parent.id, child.id]
    parent.child_count = 1
    post.reply_count = 2
    scene.community.post_reply_count = 2
    db.session.commit()
    return SimpleNamespace(post=post, parent=parent, child=child)


# --------------------------------------------------------------------------
# What a site ban leaves behind
# --------------------------------------------------------------------------


class TestTheCountersAfterASiteBan:

    def test_an_ancestors_child_count_comes_down(self, scene):
        """`update post_reply set child_count = child_count - 1 where id in :parents`, with
        `reply.path[:-1]` -- every ancestor, not just the immediate parent, because a thread
        renders its counts at every level."""
        thread = a_thread(scene, scene.author)

        site_ban_remove_data(scene.moderator.id, scene.author)

        db.session.expire_all()
        assert db.session.get(PostReply, thread.parent.id).child_count == 0
        # `reply.path[:-1]` -- the reply's OWN id is excluded. It is being deleted, not
        # gaining a child, and a row is left behind with `child_count == -1` if the slice
        # goes.
        assert db.session.get(PostReply, thread.child.id).child_count == 0

    def test_a_top_level_reply_touches_no_ancestor(self, scene):
        """`if reply.path and len(reply.path) > 1`. A reply with a one-element path has no
        ancestors, and `tuple(reply.path[:-1])` would be `()` -- an empty IN list, which
        PostgreSQL refuses to parse."""
        post = make_post(scene.community, scene.moderator,
                         ap_id='https://test.piefed.local/p/2')
        db.session.commit()
        reply = make_post_reply(post, scene.author, body='top level')
        db.session.commit()
        reply.path = [reply.id]
        post.reply_count = 1
        db.session.commit()

        site_ban_remove_data(scene.moderator.id, scene.author)

        db.session.expire_all()
        assert db.session.get(Post, post.id).reply_count == 0

    def test_the_posts_reply_count_comes_down_for_a_human(self, scene):
        """`if not blocked.bot`. A bot's replies were never counted towards the visible total,
        so subtracting them would take the count below what a reader ever saw."""
        thread = a_thread(scene, scene.author)

        site_ban_remove_data(scene.moderator.id, scene.author)

        db.session.expire_all()
        assert db.session.get(Post, thread.post.id).reply_count == 1

    def test_a_bots_replies_do_not_move_the_posts_count(self, scene):
        thread = a_thread(scene, scene.author)
        scene.author.bot = True
        db.session.commit()

        site_ban_remove_data(scene.moderator.id, scene.author)

        db.session.expire_all()
        assert db.session.get(Post, thread.post.id).reply_count == 2

    def test_a_deleted_link_post_is_taken_out_of_its_cross_posts(self, scene):
        """`if post.url and post.cross_posts is not None: calculate_cross_posts(delete_only)`.
        The other copies of a cross-posted link hold this post's id in their own arrays, and
        leaving it there shows a reader a cross-post that has been deleted."""
        post = make_post(scene.community, scene.author,
                         ap_id='https://test.piefed.local/p/3')
        post.url = 'https://news.example/story'
        post.cross_posts = [999]
        db.session.commit()
        calls = []

        with patch.object(Post, 'calculate_cross_posts',
                          side_effect=lambda *args, **kwargs: calls.append(kwargs)):
            site_ban_remove_data(scene.moderator.id, scene.author)

        assert calls == [{'delete_only': True}]

    def test_a_post_with_no_link_is_not_recalculated(self, scene):
        """Both halves of the guard: a discussion post has no url, and a link post that has
        never been cross-posted has `cross_posts` NULL. Neither needs the work, and
        `calculate_cross_posts` is a query per call."""
        post = make_post(scene.community, scene.author,
                         ap_id='https://test.piefed.local/p/4')
        post.url = None
        post.cross_posts = None
        db.session.commit()
        calls = []

        with patch.object(Post, 'calculate_cross_posts',
                          side_effect=lambda *args, **kwargs: calls.append(kwargs)):
            site_ban_remove_data(scene.moderator.id, scene.author)

        assert calls == []

    def test_the_accounts_own_totals_are_zeroed(self, scene):
        """`blocked.post_count = 0` and `blocked.post_reply_count = 0` -- not decremented, set.
        A site ban removes EVERYTHING they wrote, so the only correct total is zero, and
        counting down would leave a residue for any content the loops did not reach."""
        a_thread(scene, scene.author)
        scene.author.post_count = 5
        scene.author.post_reply_count = 9
        db.session.commit()

        site_ban_remove_data(scene.moderator.id, scene.author)

        db.session.expire_all()
        refreshed = db.session.get(type(scene.author), scene.author.id)
        assert refreshed.post_count == 0
        assert refreshed.post_reply_count == 0


# --------------------------------------------------------------------------
# What a community ban leaves behind
# --------------------------------------------------------------------------


class TestTheCountersAfterACommunityBan:
    """The same bookkeeping, scoped to one community -- and here the account's own totals are
    DECREMENTED rather than zeroed, because their content elsewhere survives.
    """

    def test_an_ancestors_child_count_comes_down(self, scene):
        thread = a_thread(scene, scene.author)

        community_ban_remove_data(scene.moderator.id, scene.community.id, scene.author)

        db.session.expire_all()
        assert db.session.get(PostReply, thread.parent.id).child_count == 0
        assert db.session.get(PostReply, thread.child.id).child_count == 0

    def test_the_accounts_totals_are_decremented_not_zeroed(self, scene):
        """The asymmetry with the site ban, and the reason for it: a community ban leaves the
        account's posts in every other community, so zeroing the totals would under-count
        them everywhere."""
        a_thread(scene, scene.author)
        post = make_post(scene.community, scene.author,
                         ap_id='https://test.piefed.local/p/5')
        db.session.commit()
        scene.author.post_count = 5
        scene.author.post_reply_count = 9
        db.session.commit()

        community_ban_remove_data(scene.moderator.id, scene.community.id, scene.author)

        db.session.expire_all()
        refreshed = db.session.get(type(scene.author), scene.author.id)
        assert refreshed.post_count == 4
        assert refreshed.post_reply_count == 8

    def test_a_deleted_link_post_is_taken_out_of_its_cross_posts(self, scene):
        post = make_post(scene.community, scene.author,
                         ap_id='https://test.piefed.local/p/6')
        post.url = 'https://news.example/story'
        post.cross_posts = [999]
        db.session.commit()
        calls = []

        with patch.object(Post, 'calculate_cross_posts',
                          side_effect=lambda *args, **kwargs: calls.append(kwargs)):
            community_ban_remove_data(scene.moderator.id, scene.community.id,
                                      scene.author)

        assert calls == [{'delete_only': True}]

    def test_content_in_another_community_is_left_alone(self, scene):
        """The scope. `community_ban_remove_data` filters on `community_id`, and a row for that
        is what makes every assertion above a claim about this community rather than about the
        account."""
        elsewhere = make_community('otherland', host='test.piefed.local')
        db.session.commit()
        make_community_member(scene.author, elsewhere)
        other_post = make_post(elsewhere, scene.author,
                               ap_id='https://test.piefed.local/p/7')
        db.session.commit()

        community_ban_remove_data(scene.moderator.id, scene.community.id, scene.author)

        db.session.expire_all()
        assert db.session.get(Post, other_post.id).deleted is False


# --------------------------------------------------------------------------
# Two peers inserting the same instance at once
# --------------------------------------------------------------------------


class TestTwoWorkersMeetingANewPeer:

    def test_a_new_domain_is_inserted_and_returned(self, scene):
        instance_id = find_instance_id('brandnew.example')

        assert db.session.get(Instance, instance_id).domain == 'brandnew.example'

    def test_a_known_domain_is_not_inserted_twice(self, scene):
        first = find_instance_id(PEER)
        second = find_instance_id(PEER)

        assert first == second

    def test_a_race_between_two_workers_resolves_to_the_existing_row(self, scene):
        """`except IntegrityError: rollback; return the row's id`. Two inbox workers meeting the
        same new peer in the same instant both reach the INSERT, and `Instance.domain` is unique
        -- so the loser has to answer with the winner's id rather than raising, or one of the two
        activities is lost. This is where D1425 was: the arm returned the Instance OBJECT.

        The `db.session.rollback()` beside it is an equivalent mutant (fact 977): removing it
        leaves the failed INSERT in the session, and the SELECT that follows still answers. It is
        kept because a dirty session is not something to hand back to the caller.
        """
        from sqlalchemy.exc import IntegrityError

        real_commit = db.session.commit
        state = {'raised': False}

        def the_other_worker_got_there_first():
            """Stand in for the competing INSERT: the row appears, and this session's own
            commit fails on the unique index -- which is exactly the order the two workers
            see."""
            if state['raised']:
                return real_commit()
            state['raised'] = True
            db.session.rollback()
            make_instance('racy.example')
            real_commit()
            raise IntegrityError('insert', {}, Exception('duplicate key'))

        with patch.object(db.session, 'commit',
                          side_effect=the_other_worker_got_there_first):
            answered = find_instance_id('racy.example')

        assert state['raised'] is True
        winner = db.session.query(Instance).filter_by(domain='racy.example').one()
        assert answered == winner.id
