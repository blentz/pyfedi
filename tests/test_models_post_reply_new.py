"""Every reason a reply is refused, and what a reply that is accepted does.

Sub-project 104 -- `PostReply.new` in `app/models.py`. Post.new was the
previous round; this is its sibling, and the interesting half of it is the
eight refusals: comments closed, an account banned from commenting, the
recipient having blocked the replier, a duplicate, a gif reaction, a low-effort
reply, a blocked phrase, and an integrity error from two replies racing.

Each refusal raises `PostReplyValidationError`, and each is the only thing
standing between a setting an admin turned on and a reply that ignores it.

No defect fixed. Two things recorded:

* `notification_target.author` is read after
  `db.session.get(PostReply, in_reply_to.parent_id)`, which answers None for a
  parent that has been deleted -- narrow, since the child row would normally
  go with it;
* the `IntegrityError` handler reads `request_json['object']['id']`, and
  `request_json` is None for a locally written reply, so a local duplicate
  that reached the database would be a `TypeError` rather than the existing
  row. Both are left as they are and pinned as far as they can be reached.
"""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import (NotificationSubscription, Post, PostReply,
                        PostReplyValidationError, PostReplyVote, Site, User,
                        UserBlock)
from tests.cache_doubles import calls_for, get_for
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_reply, make_user)

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.enable_gif_reply_rep_decrease = False
    g.site.enable_this_comment_filter = False
    community = make_community('probeland')
    author = api_baseline.user2
    replier = api_baseline.user3
    db.session.commit()
    make_community_member(author, community)
    make_community_member(replier, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/p/1')
    db.session.commit()
    return SimpleNamespace(community=community, author=author,
                           replier=replier, post=post,
                           baseline=api_baseline)


def a_reply(env, body='a reply', **kwargs):
    return PostReply.new(kwargs.pop('user', env.replier),
                         kwargs.pop('post', env.post),
                         kwargs.pop('in_reply_to', None),
                         body, f'<p>{body}</p>',
                         kwargs.pop('notify_author', False),
                         kwargs.pop('language_id', None),
                         kwargs.pop('distinguished', False),
                         kwargs.pop('answer', False),
                         **kwargs)


def an_activity(reply_id='https://remote.test/r/1', **overrides):
    activity = {'id': f'{reply_id}/create', 'type': 'Create', 'to': [PUBLIC],
                'object': {'id': reply_id, 'type': 'Note'}}
    activity.update(overrides)
    return activity


class TestTheReasonsAReplyIsRefused:
    def test_a_post_with_comments_closed(self, env):
        env.post.comments_enabled = False
        db.session.commit()
        with pytest.raises(PostReplyValidationError,
                           match='Comments are disabled'):
            a_reply(env)

    def test_an_account_banned_from_commenting(self, env):
        env.replier.ban_comments = True
        db.session.commit()
        with pytest.raises(PostReplyValidationError,
                           match='Banned from commenting'):
            a_reply(env)

    def test_a_recipient_who_has_blocked_the_replier(self, env):
        db.session.add(UserBlock(blocker_id=env.author.id,
                                 blocked_id=env.replier.id))
        db.session.commit()
        with pytest.raises(PostReplyValidationError, match='Replier blocked'):
            a_reply(env)

    def test_the_same_reply_twice(self, env):
        a_reply(env, body='the same words')
        with pytest.raises(PostReplyValidationError, match='Duplicate reply'):
            a_reply(env, body='the same words')

    def test_a_blocked_phrase(self, env):
        with patch('app.utils.blocked_phrases', return_value=['buy now']):
            with pytest.raises(PostReplyValidationError,
                               match='Blocked phrase'):
                a_reply(env, body='buy now, cheap')

    def test_a_gif_reaction_when_the_instance_discourages_them(self, env):
        g.site.enable_gif_reply_rep_decrease = True
        db.session.commit()
        with patch('app.utils.reply_is_just_link_to_gif_reaction',
                   return_value=True):
            with pytest.raises(PostReplyValidationError,
                               match='Gif comment ignored'):
                a_reply(env, body='https://giphy.test/a.gif')

    def test_and_when_it_does_not(self, env):
        g.site.enable_gif_reply_rep_decrease = False
        db.session.commit()
        with patch('app.utils.reply_is_just_link_to_gif_reaction',
                   return_value=True):
            assert a_reply(env, body='https://giphy.test/a.gif') is not None

    def test_a_low_effort_reply_when_the_filter_is_on(self, env):
        g.site.enable_this_comment_filter = True
        db.session.commit()
        with patch('app.utils.reply_is_low_effort', return_value=True):
            with pytest.raises(PostReplyValidationError,
                               match='Low quality reply'):
                a_reply(env, body='this')

    def test_and_when_it_is_off(self, env):
        g.site.enable_this_comment_filter = False
        db.session.commit()
        with patch('app.utils.reply_is_low_effort', return_value=True):
            assert a_reply(env, body='this') is not None



class TestWhatAnAcceptedReplyDoes:
    def test_it_is_stored_against_the_post_and_the_community(self, env):
        reply = a_reply(env)
        assert reply.post_id == env.post.id
        assert reply.community_id == env.community.id
        assert reply.user_id == env.replier.id

    def test_it_upvotes_itself(self, env):
        reply = a_reply(env)
        assert reply.score == 1
        assert reply.up_votes == 1
        assert PostReplyVote.query.filter_by(post_reply_id=reply.id,
                                             user_id=env.replier.id).count() == 1

    def test_a_top_level_reply_gets_a_path_of_its_own(self, env):
        reply = a_reply(env)
        assert reply.path == [0, reply.id]
        assert reply.root_id == reply.id

    def test_a_child_hangs_off_its_parent(self, env):
        parent = a_reply(env, body='the first')
        child = a_reply(env, body='the second', in_reply_to=parent)
        assert child.parent_id == parent.id
        assert child.depth == 1
        assert child.path == [0, parent.id, child.id]
        assert child.root_id == parent.id

    def test_the_parent_s_child_count_goes_up(self, env):
        parent = a_reply(env, body='the first')
        a_reply(env, body='the second', in_reply_to=parent)
        db.session.expire_all()
        assert db.session.get(PostReply, parent.id).child_count == 1

    def test_the_post_s_reply_count_goes_up(self, env):
        a_reply(env)
        db.session.expire_all()
        assert db.session.get(Post, env.post.id).reply_count == 1

    def test_a_bot_s_reply_is_not_counted(self, env):
        env.replier.bot = True
        db.session.commit()
        a_reply(env)
        db.session.expire_all()
        assert db.session.get(Post, env.post.id).reply_count == 0

    def test_asking_to_be_notified_creates_the_subscription(self, env):
        reply = a_reply(env, notify_author=True)
        assert NotificationSubscription.query.filter_by(
            entity_id=reply.id, user_id=env.replier.id).count() == 1

    def test_not_asking_does_not(self, env):
        reply = a_reply(env, notify_author=False)
        assert NotificationSubscription.query.filter_by(
            entity_id=reply.id, user_id=env.replier.id).count() == 0

    def test_a_reply_by_somebody_other_than_the_author_is_collapsible(self,
                                                                     env):
        assert a_reply(env).collapsible is True

    def test_and_the_author_s_own_is_not(self, env):
        assert a_reply(env, user=env.author).collapsible is False

    def test_the_cross_posted_count_covers_every_copy(self, env):
        other = make_post(env.community, env.author,
                          ap_id='https://test.piefed.local/p/2')
        db.session.commit()
        env.post.cross_posts = [other.id]
        db.session.commit()
        a_reply(env)
        db.session.expire_all()
        assert db.session.get(Post, other.id).reply_count_cross_posted == 1

    def test_and_a_post_with_no_cross_posts_counts_only_itself(self, env):
        a_reply(env)
        db.session.expire_all()
        post = db.session.get(Post, env.post.id)
        assert post.reply_count_cross_posted == post.reply_count


class TestWhatAPeerSays:
    def test_a_reply_that_arrives_over_activitypub(self, env):
        reply = a_reply(env, request_json=an_activity())
        assert reply.ap_create_id == 'https://remote.test/r/1/create'

    def test_an_update_is_marked_as_edited(self, env):
        reply = a_reply(env, request_json=an_activity(type='Update'))
        assert reply.edited_at is not None

    def test_an_activity_with_no_type_is_not(self, env):
        activity = an_activity()
        del activity['type']
        assert a_reply(env, request_json=activity).edited_at is None

    def test_a_followers_only_reply_is_stored_as_followers(self, env):
        activity = an_activity()
        activity['object']['to'] = ['https://remote.test/u/someone/followers']
        assert a_reply(env, request_json=activity).visibility == 'followers'

    def test_a_public_one_is_stored_as_public(self, env):
        activity = an_activity()
        activity['object']['to'] = [PUBLIC]
        assert a_reply(env, request_json=activity).visibility == 'public'

    def test_the_old_private_marker_is_no_longer_written(self, env):
        activity = an_activity(to=['https://remote.test/u/someone/followers'])
        assert a_reply(env, request_json=activity).private is False

    def test_one_the_peer_says_is_not_searchable(self, env):
        activity = an_activity()
        activity['object']['searchableBy'] = \
            'https://remote.test/u/someone/followers'
        assert a_reply(env, request_json=activity).indexable is False

    def test_one_it_says_is(self, env):
        activity = an_activity()
        activity['object']['searchableBy'] = PUBLIC
        assert a_reply(env, request_json=activity).indexable is True

    def test_a_reply_in_a_private_community_is_never_indexable(self, env):
        env.community.private = True
        db.session.commit()
        assert a_reply(env, request_json=an_activity()).indexable is False

    def test_a_locally_written_reply_carries_no_activity_ids(self, env):
        reply = a_reply(env)
        assert reply.ap_create_id is None


class TestTheEmDashReport:
    """A new account using an em-dash is reported to the admins as likely AI."""

    @pytest.fixture
    def newcomer(self, env):
        from app.utils import utcnow
        env.replier.created = utcnow()
        db.session.commit()
        return env.replier

    def test_a_new_account_using_one_is_reported(self, env, newcomer):
        with patch('app.utils.notify_admin') as notify:
            a_reply(env, body='a reply — with an em dash')
        assert notify.call_count == 1
        assert 'em-dash' in notify.call_args.args[0]

    def test_an_older_account_is_not(self, env):
        from datetime import timedelta

        from app.utils import utcnow
        env.replier.created = utcnow() - timedelta(days=90)
        db.session.commit()
        with patch('app.utils.notify_admin') as notify:
            a_reply(env, body='a reply — with an em dash')
        assert notify.call_count == 0

    def test_a_reply_with_no_em_dash_is_not(self, env, newcomer):
        with patch('app.utils.notify_admin') as notify:
            a_reply(env, body='a reply with no em dash')
        assert notify.call_count == 0

    def test_the_setting_turned_off(self, env, newcomer):
        from app.utils import set_setting
        set_setting('enable_report_em_dash_replies', False)
        with patch('app.utils.notify_admin') as notify:
            a_reply(env, body='a reply — with an em dash')
        assert notify.call_count == 0

    def test_an_account_already_reported_is_not_reported_again(self, env,
                                                              newcomer):
        """The "only once" setting reads a redis key, so the second report is
        suppressed by what the first one stored. The cache is asserted
        directly, because the test cache does not carry a value between two
        calls in one test."""
        from app.utils import set_setting
        set_setting('limit_one_em_report_per_user', True)
        with patch('app.models.cache.get', side_effect=get_for('em-dash_used_by_', True)), \
                patch('app.utils.notify_admin') as notify:
            a_reply(env, body='a reply — with an em dash')
        assert notify.call_count == 0

    def test_and_the_first_report_stores_that_flag(self, env, newcomer):
        from app.utils import set_setting
        set_setting('limit_one_em_report_per_user', True)
        with patch('app.models.cache.get', side_effect=get_for('em-dash_used_by_', None)), \
                patch('app.models.cache.set') as store, \
                patch('app.utils.notify_admin') as notify:
            a_reply(env, body='a reply — with an em dash')
        assert notify.call_count == 1
        flags = calls_for(store, 'em-dash_used_by_')
        assert len(flags) == 1
        assert flags[0].kwargs['timeout'] == 86400

    def test_and_every_time_when_not(self, env, newcomer):
        from app.utils import set_setting
        set_setting('limit_one_em_report_per_user', False)
        with patch('app.utils.notify_admin') as notify:
            a_reply(env, body='the first — em dash')
            a_reply(env, body='the second — em dash')
        assert notify.call_count == 2
