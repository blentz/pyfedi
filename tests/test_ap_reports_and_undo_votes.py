"""A peer taking a vote back, and a peer reporting something to us.

Sub-project 109 -- `undo_vote` and `process_report` in
`app/activitypub/util.py`. Both run off an activity that arrived in the inbox:
the first adjusts a score and somebody's reputation, the second files a Report
row and notifies whoever is responsible for the content.

One defect:

    'reporter_user_name': user.ap_id if user.ap_id else user.name,

`User` has no `name` -- the column is `user_name`, and the two branches above
this one spell it correctly. A reporter with no `ap_id`, which is what a local
actor is, was an `AttributeError` here and the report was never filed (D1300).
Its only protection was the invariant that the inbox's actor is always remote,
which nothing states.

Also guarded: `session.get(Instance, user.instance_id).domain` at three sites,
read without checking the row is there.

`undo_vote` carries a comment worth keeping in mind while reading the tests:
Lemmy sends `Like` for an upvote and `Dislike` for a downvote, but undoes BOTH
with `Undo Like` -- so which counter comes down is decided by the stored vote's
own effect, not by the activity.
"""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app.activitypub.util import process_report, undo_vote
from app import db
from app.constants import (NOTIF_REPORT, REPORT_TYPE_POST, REPORT_TYPE_REPLY,
                           REPORT_TYPE_USER)
from app.models import (Community, Instance, Notification, Post, PostReply,
                        PostReplyVote, PostVote, Report, Site, User)
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_reply, make_post_reply_vote,
                             make_post_vote, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = api_baseline.user2
    remote_instance = api_baseline.instance_remote
    voter = make_user(remote_instance, 'faraway')
    voter.ap_id = 'faraway@remote.test'
    voter.ap_profile_id = 'https://remote.test/u/faraway'
    voter.ap_public_url = 'https://remote.test/u/faraway'
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/p/1')
    db.session.commit()
    reply = make_post_reply(post, author, body='a reply')
    # `find_liked_object` reads the ap_id, and the factory leaves it unset.
    reply.ap_id = 'https://test.piefed.local/comment/1'
    db.session.commit()
    return SimpleNamespace(community=community, author=author, voter=voter,
                           post=post, reply=reply, baseline=api_baseline)


def reputation_of(user_id):
    db.session.expire_all()
    return db.session.get(User, user_id).reputation


# --------------------------------------------------------------------------
# taking a vote back
# --------------------------------------------------------------------------

class TestUndoingAVoteOnAPost:
    def test_an_upvote_is_taken_off_the_up_count(self, env):
        env.post.up_votes = 5
        env.post.score = 5
        make_post_vote(env.voter, env.post, 1)
        db.session.commit()
        result = undo_vote(None, env.post, env.post.ap_id, env.voter)
        assert result == env.post
        db.session.expire_all()
        post = db.session.get(Post, env.post.id)
        assert post.up_votes == 4
        assert post.score == 4

    def test_a_downvote_comes_off_the_down_count(self, env):
        """Lemmy undoes a downvote with `Undo Like` too, so the stored vote's
        own effect is what decides which counter moves."""
        env.post.down_votes = 3
        env.post.score = -3
        make_post_vote(env.voter, env.post, -1)
        db.session.commit()
        undo_vote(None, env.post, env.post.ap_id, env.voter)
        db.session.expire_all()
        post = db.session.get(Post, env.post.id)
        assert post.down_votes == 2
        assert post.score == -2

    def test_the_author_s_reputation_follows_the_vote(self, env):
        env.author.reputation = 10
        make_post_vote(env.voter, env.post, 1)
        db.session.commit()
        undo_vote(None, env.post, env.post.ap_id, env.voter)
        assert reputation_of(env.author.id) == 9

    def test_and_a_downvote_gives_it_back(self, env):
        env.author.reputation = 10
        make_post_vote(env.voter, env.post, -1)
        db.session.commit()
        undo_vote(None, env.post, env.post.ap_id, env.voter)
        assert reputation_of(env.author.id) == 11

    def test_the_vote_row_is_removed(self, env):
        make_post_vote(env.voter, env.post, 1)
        db.session.commit()
        undo_vote(None, env.post, env.post.ap_id, env.voter)
        assert PostVote.query.filter_by(user_id=env.voter.id,
                                        post_id=env.post.id).count() == 0

    def test_undoing_a_vote_nobody_cast(self, env):
        """A repeated Undo is a no-op, not a failure."""
        env.post.up_votes = 5
        db.session.commit()
        result = undo_vote(None, env.post, env.post.ap_id, env.voter)
        assert result == env.post
        db.session.expire_all()
        assert db.session.get(Post, env.post.id).up_votes == 5

    def test_somebody_else_s_vote_is_left_alone(self, env):
        make_post_vote(env.author, env.post, 1)
        db.session.commit()
        undo_vote(None, env.post, env.post.ap_id, env.voter)
        assert PostVote.query.filter_by(user_id=env.author.id,
                                        post_id=env.post.id).count() == 1


class TestUndoingAVoteOnAReply:
    def test_an_upvote_is_taken_off(self, env):
        env.reply.up_votes = 4
        env.reply.score = 4
        make_post_reply_vote(env.voter, env.reply, 1)
        db.session.commit()
        result = undo_vote(env.reply, None, env.reply.ap_id, env.voter)
        assert result == env.reply
        db.session.expire_all()
        reply = db.session.get(PostReply, env.reply.id)
        assert reply.up_votes == 3
        assert reply.score == 3

    def test_a_downvote_comes_off_the_down_count(self, env):
        env.reply.down_votes = 2
        env.reply.score = -2
        make_post_reply_vote(env.voter, env.reply, -1)
        db.session.commit()
        undo_vote(env.reply, None, env.reply.ap_id, env.voter)
        db.session.expire_all()
        reply = db.session.get(PostReply, env.reply.id)
        assert reply.down_votes == 1
        assert reply.score == -1

    def test_the_author_s_reputation_follows_it(self, env):
        env.author.reputation = 10
        make_post_reply_vote(env.voter, env.reply, 1)
        db.session.commit()
        undo_vote(env.reply, None, env.reply.ap_id, env.voter)
        assert reputation_of(env.author.id) == 9

    def test_the_vote_row_is_removed(self, env):
        make_post_reply_vote(env.voter, env.reply, 1)
        db.session.commit()
        undo_vote(env.reply, None, env.reply.ap_id, env.voter)
        assert PostReplyVote.query.filter_by(
            user_id=env.voter.id, post_reply_id=env.reply.id).count() == 0

    def test_undoing_a_vote_nobody_cast(self, env):
        result = undo_vote(env.reply, None, env.reply.ap_id, env.voter)
        assert result == env.reply


class TestUndoingAVoteOnSomethingElse:
    def test_an_id_that_names_nothing_here(self, env):
        assert undo_vote(None, None, 'https://remote.test/p/999',
                         env.voter) is None

    def test_no_id_at_all(self, env):
        """D1301. `'/comment/' in ap_id` was `TypeError: argument of type
        'NoneType' is not iterable`, and the id is whatever the peer put in
        `object.object` on its Undo."""
        assert undo_vote(None, None, None, env.voter) is None

    def test_an_id_that_is_an_object_rather_than_a_string(self, env):
        assert undo_vote(None, None, {'id': 'https://remote.test/p/1'},
                         env.voter) is None


# --------------------------------------------------------------------------
# a report arriving from a peer
# --------------------------------------------------------------------------

def a_flag(summary='spam', **overrides):
    activity = {'id': 'https://remote.test/activities/flag/1', 'type': 'Flag',
                'actor': 'https://remote.test/u/faraway',
                'object': 'https://test.piefed.local/p/1'}
    if summary is not None:
        activity['summary'] = summary
    activity.update(overrides)
    return activity


class TestReportingAnAccount:
    def test_a_report_is_filed(self, env):
        process_report(env.voter, env.author, a_flag(), db.session)
        report = Report.query.filter_by(suspect_user_id=env.author.id).one()
        assert report.type == REPORT_TYPE_USER
        assert report.reporter_id == env.voter.id
        assert report.reasons == 'spam'

    def test_where_it_came_from_is_recorded(self, env):
        process_report(env.voter, env.author, a_flag(), db.session)
        report = Report.query.filter_by(suspect_user_id=env.author.id).one()
        assert report.source_instance_id == env.voter.instance_id
        assert report.targets['source_instance_domain'] == \
            env.baseline.instance_remote.domain

    def test_the_admins_are_told(self, env):
        admin = env.baseline.user1
        g.admin_ids = [admin.id]
        with patch.object(Site, 'admins', staticmethod(lambda: [admin])):
            process_report(env.voter, env.author, a_flag(), db.session)
        assert Notification.query.filter_by(user_id=admin.id,
                                            notif_type=NOTIF_REPORT).count() == 1

    def test_the_count_on_the_account_goes_up(self, env):
        env.author.reports = 0
        db.session.commit()
        process_report(env.voter, env.author, a_flag(), db.session)
        db.session.expire_all()
        assert db.session.get(User, env.author.id).reports == 1

    def test_an_account_whose_reports_have_been_dismissed_for_good(self, env):
        """-1 means "stop telling me about this one"."""
        env.author.reports = -1
        db.session.commit()
        process_report(env.voter, env.author, a_flag(), db.session)
        assert Report.query.filter_by(suspect_user_id=env.author.id).count() == 0

    def test_an_instance_row_that_is_gone(self, env):
        """A foreign key stops the row being deleted while an account points
        at it, so the absence is simulated at the session rather than in the
        database -- which is the only way this arm can be reached at all."""
        class _WithoutInstances:
            def __init__(self, real):
                self._real = real

            def get(self, model, ident):
                if model is Instance:
                    return None
                return self._real.get(model, ident)

            def __getattr__(self, name):
                return getattr(self._real, name)

        process_report(env.voter, env.author, a_flag(),
                       _WithoutInstances(db.session))
        report = Report.query.filter_by(suspect_user_id=env.author.id).one()
        assert report.targets['source_instance_domain'] == ''


class TestWhatTheFlagSays:
    def test_a_summary_is_the_reason(self, env):
        process_report(env.voter, env.author, a_flag(summary='harassment'),
                       db.session)
        assert Report.query.one().reasons == 'harassment'

    def test_peertube_sends_content_instead(self, env):
        """A Flag from PeerTube carries no summary."""
        process_report(env.voter, env.author,
                       a_flag(summary=None, content='nudity'), db.session)
        assert Report.query.one().reasons == 'nudity'

    def test_one_that_says_nothing_at_all(self, env):
        process_report(env.voter, env.author, a_flag(summary=None),
                       db.session)
        assert Report.query.one().reasons == ''

    def test_a_reason_longer_than_the_column(self, env):
        process_report(env.voter, env.author, a_flag(summary='x' * 500),
                       db.session)
        assert len(Report.query.one().reasons) == 255


class TestReportingAPost:
    def test_a_report_is_filed_against_the_post_and_its_author(self, env):
        process_report(env.voter, env.post, a_flag(), db.session)
        report = Report.query.filter_by(suspect_post_id=env.post.id).one()
        assert report.type == REPORT_TYPE_POST
        assert report.suspect_user_id == env.author.id
        assert report.in_community_id == env.community.id

    def test_what_was_reported_is_recorded_for_the_moderators(self, env):
        env.post.title = 'a title'
        env.post.body = 'a body'
        db.session.commit()
        process_report(env.voter, env.post, a_flag(), db.session)
        targets = Report.query.one().targets
        assert targets['orig_post_title'] == 'a title'
        assert targets['orig_post_body'] == 'a body'

    def test_the_moderators_are_told(self, env):
        moderator = env.baseline.user3
        make_community_member(moderator, env.community, is_moderator=True)
        db.session.commit()
        process_report(env.voter, env.post, a_flag(), db.session)
        assert Notification.query.filter_by(
            user_id=moderator.id, notif_type=NOTIF_REPORT).count() == 1

    def test_an_unmoderated_local_community_tells_the_admins_too(self, env):
        admin = env.baseline.user1
        env.community.un_moderated = True
        db.session.commit()
        with patch.object(Site, 'admins', staticmethod(lambda: [admin])):
            process_report(env.voter, env.post, a_flag(), db.session)
        assert Notification.query.filter_by(user_id=admin.id).count() >= 1

    def test_a_moderated_one_does_not(self, env):
        admin = env.baseline.user1
        env.community.un_moderated = False
        db.session.commit()
        with patch.object(Site, 'admins', staticmethod(lambda: [admin])):
            process_report(env.voter, env.post, a_flag(), db.session)
        assert Notification.query.filter_by(user_id=admin.id).count() == 0

    def test_the_count_on_the_post_goes_up(self, env):
        env.post.reports = 0
        db.session.commit()
        process_report(env.voter, env.post, a_flag(), db.session)
        db.session.expire_all()
        assert db.session.get(Post, env.post.id).reports == 1

    def test_a_post_whose_reports_have_been_dismissed_for_good(self, env):
        env.post.reports = -1
        db.session.commit()
        process_report(env.voter, env.post, a_flag(), db.session)
        assert Report.query.count() == 0


class TestReportingAComment:
    def test_a_report_is_filed_against_the_comment_and_its_post(self, env):
        process_report(env.voter, env.reply, a_flag(), db.session)
        report = Report.query.filter_by(
            suspect_post_reply_id=env.reply.id).one()
        assert report.type == REPORT_TYPE_REPLY
        assert report.suspect_post_id == env.post.id
        assert report.suspect_user_id == env.author.id

    def test_what_was_said_is_recorded(self, env):
        process_report(env.voter, env.reply, a_flag(), db.session)
        assert Report.query.one().targets['orig_comment_body'] == 'a reply'

    def test_a_local_reporter_is_named_by_their_user_name(self, env):
        """D1300. `user.ap_id if user.ap_id else user.name` -- `User` has no
        `name`, so this was an AttributeError and the report was never filed.
        The account and post branches above spell it `user_name`."""
        local = env.baseline.user3
        assert local.ap_id is None
        process_report(local, env.reply, a_flag(), db.session)
        report = Report.query.one()
        assert report.targets['reporter_user_name'] == local.user_name

    def test_a_remote_reporter_is_named_by_their_handle(self, env):
        process_report(env.voter, env.reply, a_flag(), db.session)
        assert Report.query.one().targets['reporter_user_name'] == \
            env.voter.ap_id

    def test_the_moderators_are_told(self, env):
        moderator = env.baseline.user3
        make_community_member(moderator, env.community, is_moderator=True)
        db.session.commit()
        process_report(env.voter, env.reply, a_flag(), db.session)
        assert Notification.query.filter_by(
            user_id=moderator.id, notif_type=NOTIF_REPORT).count() == 1

    def test_the_count_on_the_comment_goes_up(self, env):
        env.reply.reports = 0
        db.session.commit()
        process_report(env.voter, env.reply, a_flag(), db.session)
        db.session.expire_all()
        assert db.session.get(PostReply, env.reply.id).reports == 1

    def test_one_whose_reports_have_been_dismissed_for_good(self, env):
        env.reply.reports = -1
        db.session.commit()
        process_report(env.voter, env.reply, a_flag(), db.session)
        assert Report.query.count() == 0
