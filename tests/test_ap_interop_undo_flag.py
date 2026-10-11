"""Interop C1, item 3 (Flag): an Undo of a Flag withdraws the report while it is still open.

Before this change the Undo dispatch had no Flag arm, so a peer withdrawing a report was
logged as 'Unmatched activity' and the report stayed in the moderators' queue.

Owner ruling (2026-10-11): withdraw the report if it is still open (status 0, new). If
moderators already acted on it -- escalated, appealed, resolved or discarded -- keep it
and log the Undo. Only the reporter may undo: the signed actor must be the report's
reporter.

The report is found by `Report.ap_id`, the Flag's id, which `process_report` now stores.
A report filed before that column existed has no ap_id, so failing that the newest report
with the same reporter and suspect target is taken.

Withdrawing corrects what the report raised: one off the suspect's `reports` counter, and
the unread report notifications it sent, with each recipient's unread count recounted.
"""
import uuid
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.activitypub import routes as activitypub_routes
from app.activitypub.util import process_report
from app.constants import (REPORT_STATE_APPEALED, REPORT_STATE_DISCARDED, REPORT_STATE_ESCALATED,
                           REPORT_STATE_RESOLVED)
from app.models import ActivityPubLog, Notification, Post, PostReply, Report, Site, User, utcnow
from tests.factories import (grant_permission, make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_user)
from tests.test_inbox_dispatch_preamble import dispatch

pytestmark = pytest.mark.usefixtures('site')
PEER = 'peer.test'
HOST = 'test.piefed.local'


@pytest.fixture
def env(app, db_session, monkeypatch):
    """A local community with a moderator, a post and a reply, a local admin, and two remote
    reporters. Built like tests/test_ap_federated_reports.py's `env`, whose docstring says why
    the admin needs a `user_role` row and the author an `ap_profile_id`.

    The Flag arm relays a report of a post to the community's followers; that relay is
    recorded away, it is not what these tests are about."""
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    monkeypatch.setattr(activitypub_routes, 'announce_activity_to_followers', lambda *args, **kwargs: None)
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()

    local = make_instance(HOST, software='piefed')
    admin = make_user(local, 'founder', local=True)
    grant_permission(admin, 'change instance settings')
    g.admin_ids = [admin.id]
    author = make_user(local, 'author', local=True)
    author.ap_profile_id = f'https://{HOST}/u/author'
    community = make_community('general')
    moderator = make_user(local, 'moderator', local=True)
    make_community_member(moderator, community, is_moderator=True)
    db.session.commit()
    post = make_post(community, author, f'https://{HOST}/c/general/p/1/a-post', title='A POST')
    reply = make_post_reply(post, author, body='a reply')
    reply.ap_id = f'https://{HOST}/comment/1'
    peer = make_instance(PEER)
    reporter = make_user(peer, 'reporter')
    reporter.ap_profile_id = f'https://{PEER}/u/reporter'
    reporter.ap_fetched_at = utcnow()
    other = make_user(peer, 'other')
    other.ap_profile_id = f'https://{PEER}/u/other'
    other.ap_fetched_at = utcnow()
    db.session.commit()
    return SimpleNamespace(community=community, post=post, reply=reply, author=author, admin=admin,
                           moderator=moderator, reporter=reporter, other=other)


def flag(reporter, target_ap_id):
    return {'id': f'https://{PEER}/activities/flag/{reporter.user_name}', 'type': 'Flag',
            'actor': reporter.ap_profile_id, 'object': target_ap_id, 'summary': 'spam'}


def undo(actor, inner):
    return {'id': f'https://{PEER}/activities/undo/{uuid.uuid4().hex}', 'type': 'Undo',
            'actor': actor.ap_profile_id, 'object': inner}


def last_log():
    return ActivityPubLog.query.order_by(ActivityPubLog.id.desc()).first()


def test_an_inbound_flag_stores_its_id_on_the_report(env):
    dispatch(flag(env.reporter, env.post.ap_id))

    assert Report.query.one().ap_id == f'https://{PEER}/activities/flag/reporter'


def test_a_flag_id_longer_than_the_column_is_not_stored(env):
    """String(255). A longer id is not truncated -- a truncated id would match no Undo
    anyway -- so the report is filed without one and the fallback lookup finds it."""
    process_report(env.reporter, env.post, {'id': f'https://{PEER}/' + 'x' * 300, 'summary': 'spam'},
                   db.session)

    assert Report.query.one().ap_id is None


def test_an_undo_withdraws_an_open_post_report_and_its_notification(env):
    dispatch(flag(env.reporter, env.post.ap_id))
    assert Notification.query.filter_by(user_id=env.moderator.id).count() == 1

    dispatch(undo(env.reporter, flag(env.reporter, env.post.ap_id)))

    db.session.expire_all()
    assert Report.query.count() == 0
    assert db.session.get(Post, env.post.id).reports == 0
    assert Notification.query.filter_by(user_id=env.moderator.id).count() == 0
    log = last_log()
    assert (log.activity_type, log.result) == ('Report', 'success')


def test_an_undo_of_a_user_report_recounts_the_admins_unread_notifications(env):
    dispatch(flag(env.reporter, env.author.ap_profile_id))
    db.session.expire_all()
    assert db.session.get(User, env.admin.id).unread_notifications == 1

    dispatch(undo(env.reporter, flag(env.reporter, env.author.ap_profile_id)))

    db.session.expire_all()
    assert Report.query.count() == 0
    assert db.session.get(User, env.author.id).reports == 0
    assert Notification.query.filter_by(user_id=env.admin.id).count() == 0
    assert db.session.get(User, env.admin.id).unread_notifications == 0


def test_an_undo_of_a_reply_report_never_takes_the_counter_below_zero(env):
    """A moderator resolving another report on the same reply resets its counter to 0
    (community_moderate_report_resolve) while this report is still open."""
    dispatch(flag(env.reporter, env.reply.ap_id))
    db.session.expire_all()
    reply = db.session.get(PostReply, env.reply.id)
    assert reply.reports == 1
    reply.reports = 0
    db.session.commit()

    dispatch(undo(env.reporter, flag(env.reporter, env.reply.ap_id)))

    db.session.expire_all()
    assert Report.query.count() == 0
    assert db.session.get(PostReply, env.reply.id).reports == 0
    assert Notification.query.filter_by(user_id=env.moderator.id).count() == 0


@pytest.mark.parametrize('status', [REPORT_STATE_ESCALATED, REPORT_STATE_APPEALED, REPORT_STATE_RESOLVED,
                                    REPORT_STATE_DISCARDED],
                         ids=['escalated', 'appealed', 'resolved', 'discarded'])
def test_an_undo_of_a_report_moderators_acted_on_keeps_it_and_logs(env, status):
    dispatch(flag(env.reporter, env.post.ap_id))
    report = Report.query.one()
    report.status = status
    db.session.commit()

    dispatch(undo(env.reporter, flag(env.reporter, env.post.ap_id)))

    db.session.expire_all()
    assert Report.query.one().status == status
    log = last_log()
    assert log.result == 'ignored'
    assert log.exception_message == f'Undo Flag: report {report.id} was already acted on and is kept'


def test_only_the_reporter_may_withdraw_a_report(env):
    """The signed actor is the only identity: an Undo naming someone else's Flag id is
    refused, whatever the inner Flag's own `actor` says."""
    dispatch(flag(env.reporter, env.post.ap_id))

    dispatch(undo(env.other, flag(env.reporter, env.post.ap_id)))

    assert Report.query.count() == 1
    log = last_log()
    assert log.result == 'failure'
    assert log.exception_message == 'Undo Flag: only the reporter may withdraw a report'


def test_an_undo_without_a_flag_id_withdraws_the_newest_matching_report(env):
    """Reports filed before Report.ap_id existed carry none. The Undo then takes the newest
    report by the same reporter about the same target, and only that one."""
    process_report(env.reporter, env.post, {'summary': 'older'}, db.session)
    process_report(env.reporter, env.post, {'summary': 'newer'}, db.session)

    dispatch(undo(env.reporter, {'type': 'Flag', 'actor': env.reporter.ap_profile_id,
                                 'object': env.post.ap_id}))

    assert [report.reasons for report in Report.query.all()] == ['older']


def test_another_reporters_report_and_notification_are_untouched(env):
    dispatch(flag(env.reporter, env.post.ap_id))
    dispatch(flag(env.other, env.post.ap_id))

    dispatch(undo(env.reporter, flag(env.reporter, env.post.ap_id)))

    db.session.expire_all()
    assert [report.reporter_id for report in Report.query.all()] == [env.other.id]
    assert db.session.get(Post, env.post.id).reports == 1
    remaining = Notification.query.filter_by(user_id=env.moderator.id).all()
    assert [notification.author_id for notification in remaining] == [env.other.id]


def test_an_undo_matching_no_report_is_ignored(env):
    dispatch(undo(env.reporter, {'id': f'https://{PEER}/activities/flag/never', 'type': 'Flag',
                                 'object': 'https://peer.test/nothing'}))

    log = last_log()
    assert log.result == 'ignored'
    assert log.exception_message == 'Undo Flag: no matching report'
