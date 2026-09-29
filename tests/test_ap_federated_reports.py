"""D1401: a peer's Flag about a user or a community, and what happened to it.

`find_reported_object` resolves the flagged id by trying a Post, then a PostReply, then
`find_actor_or_create` -- whose own signature is `Union[User, Community, Feed, None]`.
So the Flag branch of the inbox can be handed five kinds of thing, and it treated all
five as though they were a Post.

TWO DEFECTS, both measured first.

**One: a report nobody recorded was logged as a success.** `process_report`'s
`isinstance(reported, Community)` arm is a bare `...`, its `Conversation` arm likewise,
and a Feed matches no arm at all -- while the caller logged `APLOG_REPORT,
APLOG_SUCCESS` regardless and fanned the Flag out to the moderators' instances.

    PROBE  find_reported_object(<a community actor url>)  ->  Community
           process_report(reporter, community, ...)       ->  0 Report rows
                                                              0 Notifications

So a report about one of our communities reached no admin's queue, produced no
notification, and appeared in the activity log as having worked. An operator reading
that log has no reason to look. `process_report` returns a bool now, and the caller
logs `APLOG_IGNORED` with the type it could not record.

**Two: a report about a USER crashed after recording itself.** The fan-out read
`reported.community` and `reported.author`, and neither attribute exists on a User or
on a Community:

    PROBE  User.community       AttributeError: 'User' object has no attribute 'community'
           User.author          AttributeError
           Community.community  AttributeError

`process_report`'s User arm works -- it creates the Report and notifies the admins, and
commits -- and then the next line raised out of the inbox. The peer saw a 500 and
retried, and **every retry recorded the report again**. Nothing in the dispatcher
catches it: there is no `try` between that line and the request.

A Flag is announced to a community's followers, so it is announceable only when it
names content IN a community. A report about a user or a community has no such
community, and the local record is the whole of what this instance does with it.

WHAT IS NOT FIXED, and why it is a feature rather than a repair. The Community and
Conversation arms remain unimplemented. A community report needs the `targets` dict
D1393 defined for `admin/reports/community_report.html` --
`suspect_community_name`, `reporter_user_name` -- and a notification subtype; a
conversation report needs the same for `conversation_report.html`. Building those
changes what admins see and how it federates. What this round fixes is the two things
that were wrong about the code as it stands: the claim, and the crash.
"""
import pytest
from flask import g

from app import db
from app.models import (ActivityPubLog, Conversation, Notification, Report, Site,
                        utcnow)
from tests.factories import (grant_permission, make_community, make_instance,
                             make_post, make_post_reply, make_user)

pytestmark = pytest.mark.usefixtures('site')
PEER = 'peer.test'
HOST = 'test.piefed.local'


@pytest.fixture
def env(app, db_session, monkeypatch):
    """A local community with a post and a reply, a local user, and a remote reporter.

    `LOG_ACTIVITYPUB_TO_DB` is on because the result of the log row is half of what
    this file asserts, and it writes nothing when the setting is off (fact 861).
    """
    from types import SimpleNamespace
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()

    local = make_instance(HOST, software='piefed')
    admin = make_user(local, 'founder', local=True)
    # A ROW IN `user_role`, not just `g.admin_ids`. `Site.admins()` consults
    # `g.admin_ids` when it is set, and the dispatcher runs under its own app context
    # where it is not -- so it falls back to a query that JOINS `user_role`, and a
    # user with no role row is excluded even by its `or_(..., User.id == 1)` arm. A
    # seeded instance always gives id 1 the admin role, so a fixture that does not is
    # a state production never has: the report was recorded and nobody was notified.
    grant_permission(admin, 'change instance settings')
    g.admin_ids = [admin.id]
    author = make_user(local, 'author', local=True)
    # A local account carries an `ap_profile_id` in production -- it is how a peer
    # names it in a Flag -- and `make_user(local=True)` leaves it None, which makes
    # `find_actor_or_create(None)` an AttributeError on `.strip()` rather than a
    # lookup. Fact 781: a fixture row the product cannot produce.
    author.ap_profile_id = f'https://{HOST}/u/author'
    community = make_community('general')
    db.session.commit()
    post = make_post(community, author, f'https://{HOST}/c/general/p/1/a-post',
                     title='A POST')
    reply = make_post_reply(post, author, body='a reply')
    reply.ap_id = f'https://{HOST}/comment/1'
    peer = make_instance(PEER)
    reporter = make_user(peer, 'reporter')
    reporter.ap_profile_id = f'https://{PEER}/u/reporter'
    reporter.ap_fetched_at = utcnow()
    db.session.commit()
    return SimpleNamespace(community=community, post=post, reply=reply,
                           author=author, reporter=reporter, admin=admin,
                           peer=peer, local=local)


def _flag(env, target_ap_id):
    return {'id': f'https://{PEER}/activities/flag/1', 'type': 'Flag',
            'actor': env.reporter.ap_profile_id, 'object': target_ap_id,
            'summary': 'spam'}


def _report(env, reported):
    """Call `process_report` directly and say what it answered and what it wrote."""
    from app.activitypub.util import process_report
    before = Report.query.count()
    answered = process_report(env.reporter, reported,
                              _flag(env, 'https://example.test/x'), db.session)
    db.session.commit()
    return answered, Report.query.count() - before


# --------------------------------------------------------------------------
# What find_reported_object can hand the Flag branch
# --------------------------------------------------------------------------


class TestWhatCanBeReported:
    def test_a_post_resolves_to_a_post(self, env):
        from app.activitypub.util import find_reported_object

        assert find_reported_object(env.post.ap_id) is env.post

    def test_a_reply_resolves_to_a_reply(self, env):
        from app.activitypub.util import find_reported_object

        assert find_reported_object(env.reply.ap_id) is env.reply

    def test_a_user_resolves_to_a_user(self, env):
        """`find_actor_or_create`, the third thing it tries, and the reason the branch
        can be handed something with no `.community`."""
        from app.activitypub.util import find_reported_object

        assert find_reported_object(env.author.ap_profile_id) is env.author

    def test_a_community_resolves_to_a_community(self, env):
        """The premise of the whole round. `find_reported_object`'s annotation says
        `Union[User, Post, PostReply, None]` and `find_actor_or_create`'s says
        `Union[User, Community, Feed, None]` -- the wider one is the truth."""
        from app.activitypub.util import find_reported_object

        assert find_reported_object(env.community.ap_profile_id) is env.community

    def test_an_id_naming_nothing_resolves_to_nothing(self, env):
        from app.activitypub.util import find_reported_object

        assert find_reported_object('https://peer.test/nothing/here') is None


# --------------------------------------------------------------------------
# process_report now says whether it recorded anything
# --------------------------------------------------------------------------


class TestWhatProcessReportAnswers:
    def test_a_post_is_recorded(self, env):
        answered, written = _report(env, env.post)

        assert answered is True
        assert written == 1

    def test_a_reply_is_recorded(self, env):
        answered, written = _report(env, env.reply)

        assert answered is True
        assert written == 1

    def test_a_user_is_recorded(self, env):
        answered, written = _report(env, env.author)

        assert answered is True
        assert written == 1

    def test_a_community_is_not_recorded_and_says_so(self, env):
        """The arm that is a bare `...`. It answered nothing before, so the caller
        could not tell this apart from the rows above."""
        answered, written = _report(env, env.community)

        assert answered is False
        assert written == 0

    def test_a_conversation_is_not_recorded_and_says_so(self, env):
        conversation = Conversation(user_id=env.author.id)
        db.session.add(conversation)
        db.session.commit()

        answered, written = _report(env, conversation)

        assert answered is False
        assert written == 0

    def test_something_no_arm_matches_is_not_recorded_either(self, env):
        """The fall-through, which today means a Feed: `find_actor_or_create` returns
        one and `process_report` has no arm for it. Before the repair this reached the
        end of the function and returned None, which is falsy -- so the answer is the
        same, but now it is stated rather than incidental."""
        from tests.factories import make_local_feed
        feed = make_local_feed('afeed', public=True)
        db.session.commit()

        answered, written = _report(env, feed)

        assert answered is False
        assert written == 0

    @pytest.mark.parametrize('attribute', ['post', 'reply', 'author'])
    def test_a_target_exempt_from_reports_is_not_recorded(self, env, attribute):
        """`if reported.reports == -1: return` on each of the three implemented arms.
        Nothing is written, so the answer has to be False -- these returned None
        before, which was falsy by accident rather than by statement."""
        target = getattr(env, attribute)
        target.reports = -1
        db.session.commit()

        answered, written = _report(env, target)

        assert answered is False
        assert written == 0


# --------------------------------------------------------------------------
# The inbox, end to end
# --------------------------------------------------------------------------


def _dispatch(env, target_ap_id, monkeypatch):
    """Put a Flag through the inbox and report the log row and the fan-out."""
    import app.activitypub.routes as routes
    announced = []

    def record_announce(*args, **kwargs):
        announced.append({'community': args[0], 'kwargs': kwargs})

    monkeypatch.setattr(routes, 'announce_activity_to_followers', record_announce)
    activity = _flag(env, target_ap_id)
    routes.process_inbox_request(activity, store_ap_json=False)
    rows = ActivityPubLog.query.all()
    return rows, announced


class TestTheInboxFlagBranch:
    def test_a_reported_post_is_a_success_and_is_announced(self, env, monkeypatch):
        """The control, and the one type for which all three things happen: recorded,
        logged success, relayed to the moderators' instances."""
        rows, announced = _dispatch(env, env.post.ap_id, monkeypatch)

        assert [row.result for row in rows] == ['success']
        assert len(announced) == 1
        assert announced[0]['community'].id == env.community.id
        assert announced[0]['kwargs']['is_flag'] is True

    def test_a_reported_user_is_recorded_and_no_longer_crashes(self, env,
                                                              monkeypatch):
        """`reported.community` was an AttributeError here, AFTER `process_report`
        had committed the row -- so the peer got a 500 and every retry duplicated the
        report. Recorded, logged, and not relayed: a user report names no community
        whose followers could receive it.
        """
        rows, announced = _dispatch(env, env.author.ap_profile_id, monkeypatch)

        assert Report.query.count() == 1
        assert [row.result for row in rows] == ['success']
        assert announced == []

    def test_a_reported_community_is_logged_as_ignored(self, env, monkeypatch):
        """Nothing recorded, so nothing claimed. The message names the type, because
        `Report ignored due to missing content` -- the branch's other refusal -- would
        send a reader looking for a deleted post."""
        rows, announced = _dispatch(env, env.community.ap_profile_id, monkeypatch)

        assert Report.query.count() == 0
        assert [row.result for row in rows] == ['ignored']
        assert 'Community' in rows[0].exception_message
        assert announced == []

    def test_a_reported_id_naming_nothing_keeps_its_own_message(self, env,
                                                               monkeypatch):
        """The pre-existing refusal, unchanged -- and distinguishable from the new
        one, which is why the new one names the type."""
        rows, announced = _dispatch(env, 'https://peer.test/nothing', monkeypatch)

        assert [row.result for row in rows] == ['ignored']
        assert 'missing content' in rows[0].exception_message
        assert announced == []

    def test_a_reported_reply_is_announced_with_its_own_community(self, env,
                                                                 monkeypatch):
        """The second announceable type, and the `isinstance(reported, (Post,
        PostReply))` guard's other arm."""
        rows, announced = _dispatch(env, env.reply.ap_id, monkeypatch)

        assert [row.result for row in rows] == ['success']
        assert len(announced) == 1
        assert announced[0]['community'].id == env.community.id

    def test_the_report_row_carries_what_the_admin_page_reads(self, env,
                                                             monkeypatch):
        """The Post arm's `targets` dict, which is what `post_report.html` renders.
        Asserted here because a recorded-but-unreadable report is the shape D1393
        repaired on the local side."""
        _dispatch(env, env.post.ap_id, monkeypatch)

        report = Report.query.one()
        assert report.targets['suspect_post_id'] == env.post.id
        assert report.targets['reporter_user_name'] == env.reporter.ap_id
        assert report.reasons == 'spam'

    def test_an_admin_is_notified_about_a_reported_user(self, env, monkeypatch):
        """What the User arm is for, and what the crash used to happen after."""
        _dispatch(env, env.author.ap_profile_id, monkeypatch)

        notifications = Notification.query.filter_by(user_id=env.admin.id).all()
        assert len(notifications) == 1
