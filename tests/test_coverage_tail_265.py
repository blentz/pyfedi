"""Round 265: the small decisions on `app/models.py`'s rows.

Thirty-odd one-line arms, each of which answers a question some template or route asks on nearly
every page. They are grouped here by the question rather than by the class, because the same
question is asked of three different rows with three copies of the answer:

    who is an instance admin        `Community.is_instance_admin(user)` and
                                    `User.is_instance_admin()`, each with an `instance_id`
                                    test. This is an AUTHORISATION answer.
    who wants to be notified        `notification_subscribers()` on Community, User and Feed --
                                    three raw SQL reads of one table, each filtering on its own
                                    `type`, so a wrong constant sends somebody else's
                                    notifications.
    what a membership is            `User.subscribed(community_id)` and `Feed.subscribed(user_id)`,
                                    whose answers drive the Join/Leave button.
    who follows whom                `User.is_following`, three strings.
    what a vote reversal means      `Post.vote` and `PostReply.vote`, whose `reversal` arms decide
                                    whether a score moves at all.
    what a delete takes with it     the `FileNotFoundError` races and the S3 keys, where getting it
                                    wrong either leaves a file for ever or deletes one somebody
                                    else is still using.
"""
import os
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.constants import (NOTIF_COMMUNITY, NOTIF_FEED, NOTIF_USER, POST_TYPE_VIDEO,
                           SUBSCRIPTION_NONMEMBER, SUBSCRIPTION_OWNER, SUBSCRIPTION_PENDING)
from app.models import (Community, CommunityJoinRequest, File, InstanceRole,
                        NotificationSubscription, Post, PostReply, PostVote, PostReplyVote,
                        RssFeed, Site, User)
from tests.factories import (make_community, make_community_member, make_feed,
                             make_feed_member, make_instance, make_post, make_post_reply,
                             make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('modelland')
    author = make_user(api_baseline.instance_local, 'modelauthor', local=True)
    reader = make_user(api_baseline.instance_local, 'modelreader', local=True)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    from types import SimpleNamespace
    return SimpleNamespace(app=app, community=community, author=author, reader=reader,
                           baseline=api_baseline)


# --------------------------------------------------------------------------
# Who is an instance admin
# --------------------------------------------------------------------------


class TestWhoCountsAsAnInstanceAdmin:
    """Two copies of the same lookup, one asked about a community's home instance and one about an
    account's own. `InstanceRole.role == 'admin'` is what both look for, and both are read by
    permission checks -- so the `else: return False` is the arm that keeps an actor with no
    instance from being treated as one of its admins.
    """

    def test_an_admin_of_the_communitys_instance_is_recognised(self, env):
        role = InstanceRole(instance_id=env.community.instance_id, user_id=env.author.id,
                            role='admin')
        db.session.add(role)
        db.session.commit()

        assert env.community.is_instance_admin(env.author) is True

    def test_a_non_admin_of_that_instance_is_not(self, env):
        assert env.community.is_instance_admin(env.reader) is False

    def test_another_role_on_that_instance_is_not_admin(self, env):
        """The `role == 'admin'` filter. `InstanceRole` carries other roles, and a moderator of an
        instance is not one of its admins."""
        db.session.add(InstanceRole(instance_id=env.community.instance_id,
                                    user_id=env.author.id, role='moderator'))
        db.session.commit()

        assert env.community.is_instance_admin(env.author) is False

    def test_another_role_on_the_accounts_instance_is_not_admin_either(self, env):
        """The `role == 'admin'` filter on the SECOND copy. Two near-identical methods, and a
        filter dropped from one says nothing about the other -- `Community.is_instance_admin` is
        read about a community's home instance and this one about the acting account."""
        db.session.add(InstanceRole(instance_id=env.author.instance_id,
                                    user_id=env.author.id, role='moderator'))
        db.session.commit()

        assert env.author.is_instance_admin() is False

    def test_a_community_with_no_instance_has_no_instance_admins(self, env):
        """`else: return False`, on both copies.

        BOTH `if self.instance_id:` GUARDS ARE EQUIVALENT MUTANTS, and provably rather than for
        want of a row: `InstanceRole.instance_id` is part of that table's PRIMARY KEY, so it is
        NOT NULL and no role row with a null instance can exist -- the query the guard skips could
        never match anything. The guards are kept for saying so without relying on the schema,
        and these two rows assert the answer either way. Fact 781's shape.
        """
        env.community.instance_id = None
        db.session.commit()

        assert env.community.is_instance_admin(env.author) is False

    def test_an_account_with_no_instance_is_not_an_instance_admin(self, env):
        env.author.instance_id = None
        db.session.commit()

        assert env.author.is_instance_admin() is False

    def test_an_account_with_the_admin_role_is_one(self, env):
        db.session.add(InstanceRole(instance_id=env.author.instance_id,
                                    user_id=env.author.id, role='admin'))
        db.session.commit()

        assert env.author.is_instance_admin() is True


# --------------------------------------------------------------------------
# Who wants to be notified
# --------------------------------------------------------------------------


class TestWhoIsNotifiedAboutWhat:
    """One table, three readers, three `type` constants. Each is raw SQL returning user ids, and
    the notification is then SENT to them -- so a reader filtering on the wrong constant delivers
    a community's posts to people who subscribed to an account.
    """

    def _subscribe(self, user, entity_id, notif_type):
        db.session.add(NotificationSubscription(name='a subscription', user_id=user.id,
                                                entity_id=entity_id, type=notif_type))
        db.session.commit()

    def test_a_communitys_subscribers_are_listed(self, env):
        self._subscribe(env.reader, env.community.id, NOTIF_COMMUNITY)

        assert env.community.notification_subscribers() == [env.reader.id]

    def test_a_community_nobody_subscribed_to_lists_nobody(self, env):
        assert env.community.notification_subscribers() == []

    def test_a_subscription_of_another_type_on_the_same_id_is_not_listed(self, env):
        """The `type` filter, which is the whole reason these three methods are not one. The
        entity id is the SAME number in each table row -- ids are per-table -- so without the
        filter a community would inherit the subscribers of the account with its id."""
        self._subscribe(env.reader, env.community.id, NOTIF_USER)

        assert env.community.notification_subscribers() == []

    def test_an_accounts_subscribers_are_listed(self, env):
        self._subscribe(env.reader, env.author.id, NOTIF_USER)

        assert env.author.notification_subscribers() == [env.reader.id]

    def test_an_account_reads_its_own_type_only(self, env):
        self._subscribe(env.reader, env.author.id, NOTIF_COMMUNITY)

        assert env.author.notification_subscribers() == []

    def test_a_feeds_subscribers_are_listed(self, env):
        feed = make_feed(env.baseline.instance_local, 'modelfeed')
        db.session.commit()
        self._subscribe(env.reader, feed.id, NOTIF_FEED)

        assert feed.notification_subscribers() == [env.reader.id]

    def test_a_feed_reads_its_own_type_only(self, env):
        feed = make_feed(env.baseline.instance_local, 'modelfeed')
        db.session.commit()
        self._subscribe(env.reader, feed.id, NOTIF_USER)

        assert feed.notification_subscribers() == []


# --------------------------------------------------------------------------
# What a membership is
# --------------------------------------------------------------------------


class TestWhatAMembershipIs:

    def test_no_community_is_not_a_membership(self, env):
        """`if community_id is None: return False`. Templates call this with
        `post.community_id`, which is None for a post being previewed -- and the answer has to be
        falsy rather than a query on `community_id IS NULL`, which would match nothing but cost a
        round trip on every row of every listing."""
        assert env.author.subscribed(None) is False

    def test_a_pending_join_request_is_pending(self, env):
        """`SUBSCRIPTION_PENDING`, the arm reached only when there is no membership row yet. A
        private community's join request sits here, and the button it draws says "pending" rather
        than "join" -- so a wrong answer invites a second request."""
        other = make_community('privateland')
        db.session.commit()
        db.session.add(CommunityJoinRequest(user_id=env.author.id, community_id=other.id))
        db.session.commit()

        assert env.author.subscribed(other.id) == SUBSCRIPTION_PENDING

    def test_no_membership_and_no_request_is_nonmember(self, env):
        other = make_community('elsewhereland')
        db.session.commit()

        assert env.author.subscribed(other.id) == SUBSCRIPTION_NONMEMBER

    def test_a_membership_wins_over_a_request(self, env):
        """The `if subscription:` / `else:` order. A member who also has a stale request row is a
        MEMBER -- the request is what remains of how they got there."""
        db.session.add(CommunityJoinRequest(user_id=env.author.id,
                                           community_id=env.community.id))
        db.session.commit()

        assert env.author.subscribed(env.community.id) != SUBSCRIPTION_PENDING

    def test_no_user_is_not_a_feed_membership(self, env):
        """`Feed.subscribed`'s own copy of the None guard, asked with the id of an anonymous
        visitor."""
        feed = make_feed(env.baseline.instance_local, 'modelfeed')
        db.session.commit()

        assert feed.subscribed(None) is False

    def test_a_feed_owner_is_reported_as_the_owner(self, env):
        feed = make_feed(env.baseline.instance_local, 'modelfeed')
        db.session.commit()
        member = make_feed_member(env.author, feed)
        member.is_owner = True
        db.session.commit()

        assert feed.subscribed(env.author.id) == SUBSCRIPTION_OWNER


class TestWhoFollowsWhom:
    """Three strings, read by the Follow button. `is_accepted` is a nullable Boolean, so True,
    None and False exhaust it -- which is why D1431 deleted the fourth arm.
    """

    def _follow(self, follower, followed, accepted):
        from app.models import UserFollower

        db.session.add(UserFollower(local_user_id=follower.id, remote_user_id=followed.id,
                                    is_inward=False, is_accepted=accepted))
        db.session.commit()

    def test_an_accepted_follow_is_following(self, env):
        self._follow(env.author, env.reader, True)

        assert env.author.is_following(env.reader) == 'following'

    def test_an_unanswered_follow_is_pending(self, env):
        """`is_accepted is None` -- the row exists and the other side has not replied."""
        self._follow(env.author, env.reader, None)

        assert env.author.is_following(env.reader) == 'pending'

    def test_a_refused_follow_is_pending_too(self, env):
        """Recorded as OBSERVED rather than as wanted: `is_accepted is False` shares the arm with
        None, so a follow the other side REFUSED draws the same "pending" button as one it has not
        answered. The row says so plainly, because the alternative is the button offering to
        follow again."""
        self._follow(env.author, env.reader, False)

        assert env.author.is_following(env.reader) == 'pending'

    def test_no_follow_row_is_no(self, env):
        assert env.author.is_following(env.reader) == 'no'

    def test_an_inward_follow_does_not_count(self, env):
        """`is_inward == False` in the filter. The inward row is the OTHER direction -- somebody
        following this account -- and reading it here would draw the button as though this account
        followed them back."""
        from app.models import UserFollower

        db.session.add(UserFollower(local_user_id=env.author.id, remote_user_id=env.reader.id,
                                    is_inward=True, is_accepted=True))
        db.session.commit()

        assert env.author.is_following(env.reader) == 'no'


class TestMarkingAPostAsRead:

    def test_a_post_is_marked_read_once(self, env):
        post = make_post(env.community, env.author, ap_id='https://test.piefed.local/m/1')
        db.session.commit()

        env.author.mark_post_as_read(post)
        db.session.commit()

        assert env.author.has_read_post(post) is True

    def test_marking_it_again_does_not_add_a_second_row(self, env):
        """`if not self.has_read_post(post)`. The association table has no unique constraint, so
        without the guard every page view of a post adds another row for the same reader."""
        post = make_post(env.community, env.author, ap_id='https://test.piefed.local/m/2')
        db.session.commit()

        env.author.mark_post_as_read(post)
        db.session.commit()
        env.author.mark_post_as_read(post)
        db.session.commit()

        assert env.author.read_post.filter_by(id=post.id).count() == 1

    def test_an_unread_post_is_not_reported_as_read(self, env):
        post = make_post(env.community, env.author, ap_id='https://test.piefed.local/m/3')
        db.session.commit()

        assert env.author.has_read_post(post) is False


class TestWhetherACommunityThemeMayBeApplied:

    def test_the_answer_is_delegated_with_both_ids(self, env):
        """A two-line method whose whole content is the delegation, and the ARGUMENT ORDER is what
        it can get wrong: `get_community_theme_allowed(community_id, self.id)` -- community first,
        user second. Swapped, it asks whether the user's id is a community whose theme the
        community's id may see."""
        seen = []

        with patch('app.community.util.get_community_theme_allowed',
                   side_effect=lambda community_id, user_id: seen.append(
                       (community_id, user_id)) or True):
            assert env.author.community_theme_allowed(env.community.id) is True

        assert seen == [(env.community.id, env.author.id)]


# --------------------------------------------------------------------------
# Cross posts
# --------------------------------------------------------------------------


class TestWhichPostsCountAsCrossPosts:

    def _post(self, env, url, community=None, number=0):
        post = make_post(community or env.community, env.author,
                         ap_id=f'https://test.piefed.local/x/{number}')
        post.url = url
        db.session.commit()
        return post

    def test_posts_sharing_a_url_find_each_other(self, env):
        first = self._post(env, 'https://news.example/story/1', number=1)
        first.calculate_cross_posts()
        db.session.commit()
        second = self._post(env, 'https://news.example/story/1', number=2)

        second.calculate_cross_posts()
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(Post, first.id).cross_posts == [second.id]
        assert db.session.get(Post, second.id).cross_posts == [first.id]

    def test_one_community_is_exempt_by_name(self, env):
        """A hardcoded `ap_profile_id` check, and the comment says why: that community's daily
        game posts all point at the same url on purpose, so treating them as cross-posts would
        collapse every day's thread into one. Worth a row because it is a LITERAL -- nothing else
        in the codebase names it."""
        exempt = make_community('dailygames')
        exempt.ap_profile_id = 'https://lemmy.zip/c/dailygames'
        db.session.commit()
        first = self._post(env, 'https://travle.earth/usa', number=3)
        first.calculate_cross_posts()
        db.session.commit()
        second = self._post(env, 'https://travle.earth/usa', community=exempt, number=4)

        second.calculate_cross_posts()
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(Post, second.id).cross_posts is None
        assert db.session.get(Post, first.id).cross_posts is None

    def test_a_bare_domain_is_not_a_cross_post(self, env):
        """`url.count('/') < 3`. Two posts merely linking to the same SITE are not the same story,
        and the front page of a news site is posted by somebody most days."""
        first = self._post(env, 'https://news.example', number=5)
        first.calculate_cross_posts()
        db.session.commit()
        second = self._post(env, 'https://news.example', number=6)

        second.calculate_cross_posts()
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(Post, second.id).cross_posts is None

    def test_a_post_already_at_the_limit_is_not_appended_to(self, env):
        """`elif len(ncp.cross_posts) < limit`. The column is a list on the row, so without the
        limit a url posted to a hundred communities gives every one of them a hundred-entry list
        that every render of every one of those posts reads."""
        existing = self._post(env, 'https://news.example/story/2', number=7)
        existing.cross_posts = list(range(900, 909))  # nine, which is the limit
        db.session.commit()
        newcomer = self._post(env, 'https://news.example/story/2', number=8)

        newcomer.calculate_cross_posts()
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(Post, existing.id).cross_posts == list(range(900, 909))
        # The newcomer's own list is set outright rather than appended to, so it names the
        # other post -- what the `elif` decides is only whether the OTHER post names it back.
        assert db.session.get(Post, newcomer.id).cross_posts == [existing.id]


# --------------------------------------------------------------------------
# Reversing a vote
# --------------------------------------------------------------------------


class TestReversingAVoteThatDidNothing:
    """`reversal` is what the API sends as `0`. It is remapped to the direction of the vote it is
    undoing, and a stored vote with `effect == 0` cannot be remapped -- the comment says the rows
    only exist because of a bot-detection feature that has been removed.
    """

    @pytest.fixture
    def voted(self, env):
        post = make_post(env.community, env.author, ap_id='https://test.piefed.local/v/1')
        db.session.commit()
        reply = make_post_reply(post, env.author, body='a reply')
        db.session.commit()
        env.post = post
        env.reply = reply
        return env

    def test_reversing_a_zero_effect_post_vote_does_nothing(self, voted):
        db.session.add(PostVote(user_id=voted.reader.id, post_id=voted.post.id,
                               author_id=voted.author.id, effect=0))
        db.session.commit()
        before = voted.post.score

        assert voted.post.vote(voted.reader, 'reversal', None) is None
        db.session.expire_all()
        assert db.session.get(Post, voted.post.id).score == before

    def test_reversing_a_zero_effect_reply_vote_does_nothing(self, voted):
        db.session.add(PostReplyVote(user_id=voted.reader.id, post_reply_id=voted.reply.id,
                                    author_id=voted.author.id, effect=0))
        db.session.commit()

        assert voted.reply.vote(voted.reader, 'reversal', None) is None

    def test_reversing_a_vote_that_is_not_there_does_nothing(self, voted):
        """The `else` of the same block, on both rows: there is nothing to reverse."""
        assert voted.post.vote(voted.reader, 'reversal', None) is None
        assert voted.reply.vote(voted.reader, 'reversal', None) is None

    def test_a_direction_that_is_none_of_the_three_is_refused(self, voted):
        """`raise ValueError`, and the comment above it says what it replaced: an `assert`, which
        vanishes under `python -O`. It did, and the fall-through cast a NEW DOWNVOTE past the
        caller's permission gates."""
        with pytest.raises(ValueError, match='unresolvable vote direction'):
            voted.reply.vote(voted.reader, 'sideways', None)

    def test_the_reversal_spelling_with_no_vote_row_reaches_neither(self, voted):
        """The two functions differ here, and the comments record it: `PostReply.vote` remaps
        `reversal` only when a vote row is found, so a reversal with no row would arrive at the
        ValueError still spelled `reversal` -- which is why its early return exists."""
        assert voted.reply.vote(voted.reader, 'reversal', None) is None


# --------------------------------------------------------------------------
# What a delete takes with it
# --------------------------------------------------------------------------


class TestAFileThatVanishesMidDelete:
    """`if os.path.isfile(path): try: os.unlink(path) except FileNotFoundError: ...`

    The guard and the handler are not redundant: between the test and the unlink another worker
    may have removed the same file -- two posts sharing a thumbnail being deleted at once. The
    handler is what stops that race aborting the whole delete and leaving the row half-removed.
    """

    def _file(self, tmp_path, **columns):
        row = File(**columns)
        db.session.add(row)
        db.session.commit()
        return row

    def test_a_thumbnail_removed_by_somebody_else_is_not_an_error(self, env, tmp_path):
        path = tmp_path / 'thumb.png'
        path.write_bytes(b'bytes')
        row = self._file(tmp_path, thumbnail_path=str(path))

        with patch('app.models.os.unlink',
                   side_effect=FileNotFoundError('another worker got there first')):
            row.delete_from_disk()

        # The row's own deletion is the caller's business; what this asserts is that it returned.
        assert db.session.get(File, row.id) is not None

    def test_an_archived_post_whose_file_vanished_is_still_deleted(self, env, tmp_path):
        """The same race on `Post.archived`, which is the JSON archive of a post's replies."""
        archive = tmp_path / 'archive.json.gz'
        archive.write_bytes(b'{}')
        post = make_post(env.community, env.author, ap_id='https://test.piefed.local/a/1')
        post.archived = str(archive)
        db.session.commit()
        post_id = post.id

        with patch('app.models.os.unlink',
                   side_effect=FileNotFoundError('already gone')):
            post.delete_dependencies()
            db.session.delete(post)
            db.session.commit()

        assert db.session.get(Post, post_id) is None


class TestWhatACommunityDeleteTakesWithIt:

    def test_the_communitys_rss_feeds_go_with_it(self, env):
        """The first loop in `Community.delete_dependencies`. An `RssFeed` row is what polls a
        remote feed and posts it here, so one left behind after its community is deleted keeps
        fetching and posting into nothing.

        THE LOOP ITSELF IS AN EQUIVALENT MUTANT, for three reasons that together cover everything
        it does: `Community.rss_feeds` cascades `all, delete-orphan`, so the feed rows go with the
        community; `RssFeed.items` cascades `all,delete`, so the items go with the feed; and the
        posts those items name are in THIS community, so the post loop below reaches them anyway.
        Recorded rather than deleted -- the loop states the ordering it wants, and the cascade that
        makes it redundant is a schema detail three classes away.
        """
        feed = RssFeed(community_id=env.community.id, url='https://news.example/rss',
                       title='A feed')
        db.session.add(feed)
        db.session.commit()
        feed_id = feed.id

        env.community.delete_dependencies()
        db.session.delete(env.community)
        db.session.commit()

        assert db.session.get(RssFeed, feed_id) is None
