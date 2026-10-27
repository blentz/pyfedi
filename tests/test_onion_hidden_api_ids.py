"""The alpha API's actions on an id in the body or query answer an onion row off the onion exactly as an id nobody
holds (follow-up F6, re-review N1 to N5, N-m5, N-m6). Ruling: one shared API finder applies `hidden_here` and raises
what a missing row raises, so the two answers are one; no endpoint keeps its own copy of the rule."""
import pytest
from sqlalchemy.exc import NoResultFound

from app import db
from app.api.alpha.views import user_view
from app.local_only import found_here, hidden_report_clause, prime_reporters, reporter_hidden_here
from app.models import ChatMessage, CommunityBan, CommunityFlair, CommunityMember, Conversation, Post, Notification, \
    PostReply, Report, User, UserBlock, UserFlair, UserFollower, UserNote, UserRegistration, read_posts
from tests.conftest import forget_login
from tests.factories import grant_permission, make_post_reply
from tests.test_hidden_routes import talking  # noqa: F401 -- a fixture
from tests.test_instance_visible_here import HOSTS, scene  # noqa: F401 -- scene is a fixture
from tests.test_local_only_actors_hidden import ONION_ART, ONION_BOB, world  # noqa: F401 -- world is a fixture
from tests.test_onion_hidden_on_clearnet import onion  # noqa: F401 -- a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.test_onion_hidden_on_clearnet import queries_in
from tests.two_domains import FIRST, ONION, default_request, onion_request, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

NOBODY = 2 ** 30   # an id no row holds


@pytest.fixture
def alpha(app, onion, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    grant_permission(onion.admin, 'ban users')
    grant_permission(onion.admin, 'administer all communities')
    grant_permission(onion.admin, 'administer all users')
    return onion


def call(app, viewer, method, path, json=None, host=FIRST):
    forget_login()
    client = app.test_client()
    headers = {'Authorization': f'Bearer {viewer.encode_jwt_token()}'}
    if method == 'GET':
        return client.get(path, base_url=HOSTS[host], headers=headers)
    return client.open(path, method=method, base_url=HOSTS[host], headers=headers, json=json)


def assert_same(app, viewer, method, path, hidden, missing):
    """`hidden` and `missing` are the bodies (or for a GET, the query strings) naming a hidden row and no row."""
    if method == 'GET':
        found, absent = call(app, viewer, 'GET', f'{path}?{hidden}'), call(app, viewer, 'GET', f'{path}?{missing}')
    else:
        found, absent = call(app, viewer, method, path, hidden), call(app, viewer, method, path, missing)
    assert (found.status_code, found.get_data()) == (absent.status_code, absent.get_data()), \
        (path, found.status_code, found.get_data()[:300], absent.get_data()[:300])
    assert 400 <= found.status_code < 500, path   # accepted (round 3): an id nobody holds is 400- or 404-shaped
    return found


def body_ids(world):
    """Each listed endpoint, who calls it, and the body naming the hidden row and the one naming no row."""
    bob, art, post, reply = world.onion_bob.id, world.onion_art.id, world.post.id, world.reply.id
    first_art = world.first_art.id
    admin, user = world.admin, world.first_bob
    return [
        ('N1 comment', user, 'POST', '/api/alpha/comment', {'post_id': post, 'body': 'PROBE'},
         {'post_id': NOBODY, 'body': 'PROBE'}),
        ('N1 comment under', user, 'POST', '/api/alpha/comment',
         {'post_id': world.first_post.id, 'parent_id': reply, 'body': 'PROBE'},
         {'post_id': world.first_post.id, 'parent_id': NOBODY, 'body': 'PROBE'}),
        ('N2 block', user, 'POST', '/api/alpha/user/block', {'person_id': bob, 'block': True},
         {'person_id': NOBODY, 'block': True}),
        ('N2 unblock', user, 'POST', '/api/alpha/user/block', {'person_id': bob, 'block': False},
         {'person_id': NOBODY, 'block': False}),
        ('N2 note', user, 'POST', '/api/alpha/user/note', {'person_id': bob, 'note': 'PROBE'},
         {'person_id': NOBODY, 'note': 'PROBE'}),
        ('N2 ban', admin, 'POST', '/api/alpha/user/ban', {'person_id': bob}, {'person_id': NOBODY}),
        ('N2 unban', admin, 'POST', '/api/alpha/user/unban', {'person_id': bob}, {'person_id': NOBODY}),
        ('N3 follow', user, 'POST', '/api/alpha/user/follow', {'user_id': bob}, {'user_id': NOBODY}),
        ('N3 unfollow', user, 'POST', '/api/alpha/user/unfollow', {'user_id': bob}, {'user_id': NOBODY}),
        ('N3 mod a person', admin, 'POST', '/api/alpha/community/mod',
         {'community_id': first_art, 'person_id': bob, 'added': True},
         {'community_id': first_art, 'person_id': NOBODY, 'added': True}),
        ('N3 unmod a person', admin, 'POST', '/api/alpha/community/mod',
         {'community_id': first_art, 'person_id': bob, 'added': False},
         {'community_id': first_art, 'person_id': NOBODY, 'added': False}),
        ('N3 mod a community', admin, 'POST', '/api/alpha/community/mod',
         {'community_id': art, 'person_id': admin.id, 'added': True},
         {'community_id': NOBODY, 'person_id': admin.id, 'added': True}),
        ('N3 set_flair', user, 'POST', '/api/alpha/user/set_flair', {'community_id': art, 'flair_text': 'PROBE'},
         {'community_id': NOBODY, 'flair_text': 'PROBE'}),
        ('N3 replies', user, 'GET', '/api/alpha/post/replies', f'parent_id={reply}', f'parent_id={NOBODY}'),
    ]


@pytest.mark.parametrize('index', range(14))
def test_a_body_id_naming_an_onion_row_answers_as_one_nobody_holds(app, alpha, index):
    name, viewer, method, path, hidden, missing = body_ids(alpha)[index]
    assert_same(app, viewer, method, path, hidden, missing)


def test_nothing_is_written_for_an_onion_row(app, alpha):
    for _, viewer, method, path, hidden, _missing in body_ids(alpha):
        call(app, viewer, method, path, hidden)
    bob, art = alpha.onion_bob.id, alpha.onion_art.id
    assert PostReply.query.filter_by(body='PROBE').count() == 0
    assert UserBlock.query.filter_by(blocked_id=bob).count() == 0
    assert UserNote.query.filter_by(target_id=bob).count() == 0
    assert UserFollower.query.filter_by(remote_user_id=bob).count() == 0
    assert UserFlair.query.filter_by(community_id=art).count() == 0
    db.session.expire_all()
    assert not alpha.onion_bob.banned


def test_on_the_onion_the_same_actions_reach_them(app, alpha):
    assert call(app, alpha.first_bob, 'POST', '/api/alpha/user/note', {'person_id': alpha.onion_bob.id, 'note': 'n'},
                host=ONION).status_code == 200
    assert call(app, alpha.first_bob, 'GET', f'/api/alpha/post/replies?parent_id={alpha.reply.id}',
                host=ONION).status_code == 200
    assert call(app, alpha.first_bob, 'POST', '/api/alpha/comment', {'post_id': alpha.post.id, 'body': 'ONIONSIDE'},
                host=ONION).status_code == 200


def test_marking_onion_posts_read_writes_nothing_on_the_first_domain(app, alpha):
    """N-m6: the batch form."""
    call(app, alpha.first_bob, 'POST', '/api/alpha/post/mark_as_read',
         {'post_ids': [alpha.post.id, alpha.first_post.id], 'read': True})
    read = {row.read_post_id for row in db.session.execute(read_posts.select().where(
        read_posts.c.user_id == alpha.first_bob.id))}
    assert read == {alpha.first_post.id}


# --- N3: the moderator list a mod change answers ---------------------------------------------------------------

def test_a_mod_change_answers_a_moderator_list_without_onion_accounts(app, alpha):
    db.session.execute(db.text('INSERT INTO community_member (user_id, community_id, is_moderator, is_owner, is_banned)'
                               ' VALUES (:u, :c, true, false, false)'),
                       {'u': alpha.onion_bob.id, 'c': alpha.first_art.id})
    db.session.commit()
    response = call(app, alpha.admin, 'POST', '/api/alpha/community/mod',
                    {'community_id': alpha.first_art.id, 'person_id': alpha.first_bob.id, 'added': True})
    assert response.status_code == 200 and ONION not in response.get_data(as_text=True)


# --- N4: report lists -------------------------------------------------------------------------------------------

@pytest.fixture
def onion_in_reply(alpha):
    alpha.onion_in_reply = make_post_reply(alpha.first_post, alpha.onion_bob, 'ONIONCOMMENT')
    alpha.onion_in_reply.local_only = True
    db.session.commit()
    return alpha


@pytest.fixture
def reports(onion_in_reply, talking):  # noqa: F811
    onion = onion_in_reply
    message = ChatMessage.query.filter_by(conversation_id=onion.with_onion.id).first()
    rows = [Report(reasons='spam', description='REPORTED', type=1, reporter_id=onion.first_bob.id, status=0,
                   suspect_post_id=onion.in_first.id, in_community_id=onion.first_art.id, source_instance_id=1,
                   suspect_user_id=onion.onion_bob.id, targets={'suspect_post_id': onion.in_first.id}),
            Report(reasons='spam', description='REPORTED', type=2, reporter_id=onion.first_bob.id, status=0,
                   suspect_post_id=onion.first_post.id, suspect_post_reply_id=onion.onion_in_reply.id,
                   in_community_id=onion.first_art.id, source_instance_id=1, suspect_user_id=onion.onion_bob.id,
                   targets={'suspect_comment_id': onion.onion_in_reply.id}),
            Report(reasons='spam', description='REPORTED', type=4, reporter_id=onion.first_bob.id, status=0,
                   suspect_conversation_id=onion.with_onion.id, source_instance_id=1, suspect_user_id=onion.onion_bob.id,
                   targets={'suspect_message_id': message.id}),
            Report(reasons='spam', description='CONTROL', type=1, reporter_id=onion.first_bob.id, status=0,
                   suspect_post_id=onion.first_post.id, in_community_id=onion.first_art.id, source_instance_id=1,
                   suspect_user_id=onion.first_bob.id, targets={'suspect_post_id': onion.first_post.id})]
    db.session.add_all(rows)
    db.session.commit()
    onion.reports = rows
    return onion


LISTS = ['/api/alpha/post/report/list', '/api/alpha/comment/report/list', '/api/alpha/private_message/report/list',
         '/api/alpha/private_message/conversation/report/list']


@pytest.mark.parametrize('path', LISTS)
def test_the_report_lists_leave_out_reports_about_onion_rows_on_the_first_domain(app, reports, path):
    body = call(app, reports.admin, 'GET', path).get_data(as_text=True)
    for mark in (ONION, 'ONIONINFIRST', 'ONIONCOMMENT', 'ONIONCHAT', f'"id":{reports.reports[0].id},'):
        assert mark not in body, (path, mark)
    if path == LISTS[0]:
        assert 'CONTROL' in body or 'FIRSTPOST' in body   # the control


def test_on_the_onion_the_report_lists_show_them(app, reports):
    body = call(app, reports.admin, 'GET', LISTS[0], host=ONION).get_data(as_text=True)
    assert 'ONIONINFIRST' in body


def test_resolving_a_report_about_an_onion_row_answers_as_a_report_nobody_holds(app, reports):
    for path, report in (('/api/alpha/post/report/resolve', reports.reports[0]),
                         ('/api/alpha/comment/report/resolve', reports.reports[1]),
                         ('/api/alpha/private_message/report/resolve', reports.reports[2])):
        assert_same(app, reports.admin, 'PUT', path, {'report_id': report.id, 'resolved': True},
                    {'report_id': NOBODY, 'resolved': True})


# --- N5: replies and mentions -------------------------------------------------------------------------------------

@pytest.fixture
def replied(alpha):
    onion = alpha
    mine = make_post_reply(onion.post, onion.first_bob, 'MINE')
    theirs = make_post_reply(onion.post, onion.onion_bob, 'ONIONANSWER')
    theirs.local_only = True
    db.session.add_all([
        Notification(user_id=onion.first_bob.id, title='t', url=f'/comment/{theirs.id}', notif_type=4,
                     subtype='new_reply_on_followed_comment', author_id=onion.onion_bob.id,
                     targets={'comment_id': theirs.id, 'post_id': onion.post.id}),
        Notification(user_id=onion.first_bob.id, title='t', url=f'/comment/{theirs.id}', notif_type=6,
                     subtype='comment_mention', author_id=onion.onion_bob.id,
                     targets={'comment_id': theirs.id, 'post_id': onion.post.id})])
    onion.first_bob.unread_notifications = 2
    db.session.commit()
    onion.mine, onion.theirs = mine, theirs
    return onion


@pytest.mark.parametrize('path', ['/api/alpha/user/replies', '/api/alpha/user/mentions',
                                  '/api/alpha/user/replies?unread_only=false'])
def test_the_apis_replies_and_mentions_leave_out_onion_replies_on_the_first_domain(app, replied, path):
    body = call(app, replied.first_bob, 'GET', path).get_data(as_text=True)
    assert 'ONIONANSWER' not in body and ONION_BOB not in body, path
    assert 'ONIONANSWER' in call(app, replied.first_bob, 'GET', path, host=ONION).get_data(as_text=True)


def test_the_unread_count_leaves_them_out_on_the_first_domain(app, replied):
    counts = call(app, replied.first_bob, 'GET', '/api/alpha/user/unread_count').get_json()
    assert (counts['replies'], counts['mentions'], counts['other']) == (0, 0, 0), counts
    counts = call(app, replied.first_bob, 'GET', '/api/alpha/user/unread_count', host=ONION).get_json()
    assert (counts['replies'], counts['mentions']) == (1, 1), counts


# --- N-m5: a message of a conversation with an onion account, by its id -------------------------------------------------

def message_ids(world):
    sent = ChatMessage.query.filter_by(conversation_id=world.with_onion.id, sender_id=world.first_bob.id).first()
    got = ChatMessage.query.filter_by(conversation_id=world.with_onion.id, recipient_id=world.first_bob.id).first()
    conversation = world.with_onion.id
    return [('/api/alpha/private_message/mark_as_read', 'POST', {'private_message_id': got.id, 'read': True}),
            ('/api/alpha/private_message', 'PUT', {'private_message_id': sent.id, 'content': 'PROBE'}),
            ('/api/alpha/private_message/delete', 'POST', {'private_message_id': sent.id, 'deleted': True}),
            ('/api/alpha/private_message/report', 'POST', {'private_message_id': got.id, 'reason': 'PROBE'}),
            ('/api/alpha/private_message/conversation/leave', 'POST', {'conversation_id': conversation}),
            ('/api/alpha/private_message/conversation/report', 'POST',
             {'conversation_id': conversation, 'reason': 'PROBE'})]


@pytest.fixture
def chatted(alpha, talking):  # noqa: F811
    db.session.add(ChatMessage(conversation_id=alpha.with_onion.id, sender_id=alpha.first_bob.id,
                               recipient_id=alpha.onion_bob.id, body='MINE', body_html='<p>MINE</p>'))
    db.session.commit()
    return alpha


@pytest.mark.parametrize('index', range(6))
def test_a_message_of_a_conversation_with_an_onion_account_answers_as_one_nobody_holds(app, chatted, index):
    path, method, hidden = message_ids(chatted)[index]
    key = 'conversation_id' if 'conversation_id' in hidden else 'private_message_id'
    assert_same(app, chatted.first_bob, method, path, hidden, {**hidden, key: NOBODY})


# --- N6: the community's banned lists ------------------------------------------------------------------------

def test_a_communitys_banned_list_names_no_onion_account_on_the_first_domain(app, alpha, monkeypatch):
    db.session.add(CommunityBan(community_id=alpha.first_art.id, user_id=alpha.onion_bob.id, banned_by=alpha.admin.id,
                                reason='BANNEDONION'))
    db.session.add(CommunityBan(community_id=alpha.first_art.id, user_id=alpha.first_bob.id, banned_by=alpha.admin.id,
                                reason='BANNEDFIRST'))
    db.session.commit()
    body = call(app, alpha.admin, 'GET', f'/api/alpha/community/moderate/bans?community_id={alpha.first_art.id}'
                ).get_data(as_text=True)
    assert ONION not in body and f'"id":{alpha.first_bob.id}' in body.replace(' ', '')
    on_onion = call(app, alpha.admin, 'GET', f'/api/alpha/community/moderate/bans?community_id={alpha.first_art.id}',
                    host=ONION).get_data(as_text=True)
    assert f'"id":{alpha.onion_bob.id}' in on_onion.replace(' ', '')


# --- the finder itself ----------------------------------------------------------------------------------------------

def test_the_finder_answers_a_hidden_conversation_or_message_and_junk_ids_as_none(app, chatted):
    message = ChatMessage.query.filter_by(conversation_id=chatted.with_onion.id).first()
    with default_request(app):
        assert found_here(Conversation, chatted.with_onion.id) is None
        assert found_here(ChatMessage, message.id) is None
        assert found_here(Conversation, chatted.with_admin.id) is not None   # the control
        for junk in ('abc', None, 0, -1, 2 ** 40):
            assert found_here(Post, junk) is None, junk
    with onion_request(app):
        assert found_here(ChatMessage, message.id) is not None


@pytest.mark.parametrize('variant', [2, 4, 5])
def test_the_profile_an_action_answers_with_is_refused_for_a_hidden_account(app, alpha, variant):
    with default_request(app):
        with pytest.raises(NoResultFound):
            user_view(alpha.onion_bob.id, variant=variant, user_id=alpha.first_bob.id)


# --- an onion account's report names no reporter off the onion (round 3, concern 4) -----------------------------

@pytest.fixture
def reported_by_onion(alpha, talking):  # noqa: F811
    onion = alpha
    reply = make_post_reply(onion.first_post, onion.first_bob, 'FIRSTCOMMENT')
    message = ChatMessage.query.filter_by(conversation_id=onion.with_admin.id).first()
    targets = {'reporter_user_name': ONION_BOB, 'reporter_id': onion.onion_bob.id}
    onion.by_onion = [
        Report(reasons='spam', description='BYONION', type=1, reporter_id=onion.onion_bob.id, status=0,
               suspect_post_id=onion.first_post.id, suspect_user_id=onion.first_bob.id,
               in_community_id=onion.first_art.id, source_instance_id=1,
               targets={**targets, 'suspect_post_id': onion.first_post.id}),
        Report(reasons='spam', description='BYONION', type=2, reporter_id=onion.onion_bob.id, status=0,
               suspect_post_id=onion.first_post.id, suspect_post_reply_id=reply.id, suspect_user_id=onion.first_bob.id,
               in_community_id=onion.first_art.id, source_instance_id=1,
               targets={**targets, 'suspect_comment_id': reply.id}),
        Report(reasons='spam', description='BYONION', type=4, reporter_id=onion.onion_bob.id, status=0,
               suspect_conversation_id=onion.with_admin.id, suspect_user_id=onion.admin.id, source_instance_id=1,
               targets={**targets, 'suspect_message_id': message.id})]
    db.session.add_all(onion.by_onion)
    db.session.commit()
    return onion


@pytest.mark.parametrize('path', LISTS)
def test_a_report_list_names_no_onion_reporter_on_the_first_domain(app, reported_by_onion, path):
    response = call(app, reported_by_onion.admin, 'GET', path)
    body = response.get_data(as_text=True)
    assert response.status_code == 200 and 'spam' in body, (path, body[:300])   # listed, but by no one named
    bob = reported_by_onion.onion_bob.id
    for mark in (ONION, f'"creator_id":{bob},', f'"creator_id":{bob}}}'):
        assert mark not in body.replace(' ', ''), (path, mark)


def test_on_the_onion_the_report_list_names_the_reporter(app, reported_by_onion):
    body = call(app, reported_by_onion.admin, 'GET', LISTS[2], host=ONION).get_data(as_text=True)
    assert f'"id":{reported_by_onion.onion_bob.id},' in body.replace(' ', '')


# --- round 4 ------------------------------------------------------------------------------------------------------

def test_setting_an_onion_notifications_state_answers_as_one_nobody_holds(app, replied):
    """R1: the answer rendered the notification in full, with the onion author and the reply."""
    notification = Notification.query.filter_by(user_id=replied.first_bob.id).first()
    assert_same(app, replied.first_bob, 'PUT', '/api/alpha/user/notification_state',
                {'notif_id': notification.id, 'read_state': True}, {'notif_id': NOBODY, 'read_state': True})
    db.session.expire_all()
    assert not notification.read   # nothing written
    on_onion = call(app, replied.first_bob, 'PUT', '/api/alpha/user/notification_state',
                    {'notif_id': notification.id, 'read_state': True}, host=ONION)
    assert on_onion.status_code == 200 and 'ONIONANSWER' in on_onion.get_data(as_text=True)


def test_the_comment_report_list_of_an_onion_community_answers_as_one_nobody_holds(app, alpha):
    """R2: a non-moderator got `incorrect login` for an onion community and `community not found` for none."""
    for viewer in (alpha.admin, alpha.first_bob):
        assert_same(app, viewer, 'GET', '/api/alpha/comment/report/list', f'community_id={alpha.onion_art.id}',
                    f'community_id={NOBODY}')


@pytest.fixture
def applied(alpha):
    grant_permission(alpha.admin, 'approve registrations')
    db.session.add_all([UserRegistration(user_id=alpha.onion_bob.id, answer='ONIONAPPLIES', status=0),
                        UserRegistration(user_id=alpha.first_bob.id, answer='FIRSTAPPLIES', status=0)])
    db.session.commit()
    return alpha


@pytest.mark.parametrize('pending_only', ['true', 'false'])
def test_the_apis_registration_queue_lists_no_onion_applicant_on_the_first_domain(app, applied, pending_only):
    """R3: the queue carried each onion applicant's email beside the onion name."""
    path = f'/api/alpha/admin/registration_application/list?pending_only={pending_only}&sort=Old'
    body = call(app, applied.admin, 'GET', path).get_data(as_text=True)
    assert 'FIRSTAPPLIES' in body and 'ONIONAPPLIES' not in body and ONION not in body
    assert 'ONIONAPPLIES' in call(app, applied.admin, 'GET', path, host=ONION).get_data(as_text=True)


def test_approving_or_denying_an_onion_applicant_answers_as_one_nobody_holds(app, applied):
    for approve in (True, False):
        assert_same(app, applied.admin, 'PUT', '/api/alpha/admin/registration_application/approve',
                    {'user_id': applied.onion_bob.id, 'approve': approve}, {'user_id': NOBODY, 'approve': approve})
    db.session.expire_all()
    assert db.session.get(User, applied.onion_bob.id) is not None   # not denied, so not deleted
    assert UserRegistration.query.filter_by(user_id=applied.onion_bob.id, status=0).count() == 1


def test_the_unread_totals_leave_out_onion_notifications_on_the_first_domain(app, replied):
    """m1: a count the list never fills tells that someone on the onion addressed this account."""
    bob = replied.first_bob
    assert call(app, bob, 'GET', '/api/alpha/user/notifications_count').get_json()['count'] == 0
    counts = call(app, bob, 'GET', '/api/alpha/user/notifications?status=All').get_json()['counts']
    assert (counts['total'], counts['unread'], counts['read']) == (0, 0, 0), counts
    assert call(app, bob, 'GET', '/api/alpha/user/notifications_count', host=ONION).get_json()['count'] == 2
    counts = call(app, bob, 'GET', '/api/alpha/user/notifications?status=All', host=ONION).get_json()['counts']
    assert (counts['total'], counts['unread']) == (2, 2), counts


def test_the_web_badge_leaves_out_onion_notifications_on_the_first_domain(app, replied):
    badge = 'unreadNotificationDisplay">2<'
    body = Visitor(app, replied.first_bob, FIRST).get('/communities').get_data(as_text=True)
    assert badge not in body and 'unreadNotifications = 0;' in body
    assert badge in Visitor(app, replied.first_bob, ONION).get('/communities').get_data(as_text=True)


@pytest.mark.parametrize('path', ['/api/alpha/post/list', '/api/alpha/post/list2'])
def test_a_post_list_of_a_private_onion_community_answers_as_one_nobody_holds(app, alpha, path):
    """m2: a private onion community answered `membership required` where one nobody holds listed nothing."""
    alpha.onion_art.private = True
    CommunityMember.query.filter_by(user_id=alpha.first_bob.id, community_id=alpha.onion_art.id).delete()
    db.session.commit()
    for hidden, missing in ((f'community_id={alpha.onion_art.id}', f'community_id={NOBODY}'),
                            (f'community_name={ONION_ART}', 'community_name=nosuchart')):
        found, absent = call(app, alpha.first_bob, 'GET', f'{path}?{hidden}'), \
            call(app, alpha.first_bob, 'GET', f'{path}?{missing}')
        assert (found.status_code, found.get_data()) == (absent.status_code, absent.get_data()), hidden
    on_onion = call(app, alpha.first_bob, 'GET', f'{path}?community_id={alpha.onion_art.id}', host=ONION)
    assert 'membership required' in on_onion.get_data(as_text=True)


def test_the_notification_counts_match_a_world_without_the_onion_ones(app, replied):
    """Security scan, round 4: read onion notifications counted in `total` and `read`, so the total told how many
    there were. A mix of hidden read, hidden unread and visible rows counts as if the hidden ones did not exist."""
    bob = replied.first_bob
    hidden_read = Notification(user_id=bob.id, title='t', url=f'/comment/{replied.theirs.id}', notif_type=4,
                               subtype='new_reply_on_followed_comment', author_id=replied.onion_bob.id, read=True,
                               targets={'comment_id': replied.theirs.id, 'post_id': replied.post.id})
    visible = [Notification(user_id=bob.id, title='t', url=f'/comment/{replied.mine.id}', notif_type=4,
                            subtype='new_reply_on_followed_comment', author_id=bob.id, read=read,
                            targets={'comment_id': replied.mine.id, 'post_id': replied.first_post.id})
               for read in (True, True, False)]
    db.session.add_all([hidden_read, *visible])
    bob.unread_notifications = 3   # the two hidden unread from `replied`, and one visible
    db.session.commit()
    counts = call(app, bob, 'GET', '/api/alpha/user/notifications?status=All').get_json()['counts']
    assert (counts['total'], counts['unread'], counts['read']) == (3, 1, 2), counts
    assert call(app, bob, 'GET', '/api/alpha/user/notifications_count').get_json()['count'] == 1
    counts = call(app, bob, 'GET', '/api/alpha/user/notifications?status=All', host=ONION).get_json()['counts']
    assert (counts['total'], counts['unread'], counts['read']) == (6, 3, 3), counts


def test_the_report_clause_tests_each_reports_own_targets(app, reports):
    """m5: an uncorrelated IN (SELECT id FROM post_reply WHERE hidden) ran the hidden test over every local-only
    reply on each list. The clause is EXISTS correlated with the report's own row, and still finds the same reports."""
    with default_request(app):
        clause = hidden_report_clause()
        assert ' IN (SELECT' not in str(clause.compile()).replace('\n', ' ')
        hidden = {report.id for report in Report.query.filter(clause)}
    assert hidden == {report.id for report in reports.reports[:3]}


REPORTERS = {LISTS[0]: 'app.api.alpha.utils.post.prime_reporters',
             LISTS[1]: 'app.api.alpha.utils.reply.prime_reporters',
             LISTS[2]: 'app.api.alpha.utils.private_message.prime_reporters',
             LISTS[3]: 'app.api.alpha.utils.private_message.prime_reporters'}


@pytest.mark.parametrize('path', LISTS)
def test_a_report_list_reads_its_reporters_in_one_query(app, reported_by_onion, path, monkeypatch):
    """m5: reporter_hidden_here read each report's reporter with its own query."""
    calls = []
    monkeypatch.setattr(REPORTERS[path], lambda items: calls.append(len(list(items))) or prime_reporters(items))
    assert call(app, reported_by_onion.admin, 'GET', path).status_code == 200
    assert calls == [1], calls   # once for the page


def test_primed_reporters_cost_no_query_each(app, reported_by_onion):
    db.session.expire_all()
    with default_request(app):
        rows = Report.query.all()
        prime_reporters(rows)
        cost = queries_in(lambda: [reporter_hidden_here(report) for report in rows])
    assert [statement for statement in cost if not statement.startswith('SELECT site_domain')] == []   # cached


@pytest.mark.parametrize('method, path', [('PUT', '/api/alpha/community/flair'),
                                          ('POST', '/api/alpha/community/flair/delete')])
def test_editing_or_deleting_an_onion_communitys_flair_answers_as_a_flair_nobody_holds(app, alpha, method, path):
    """m8: a clearnet admin could edit or delete an onion community's flair, and anyone could tell its id from a
    missing one."""
    flair = CommunityFlair(community_id=alpha.onion_art.id, flair='ONIONFLAIR')
    db.session.add(flair)
    db.session.commit()
    for viewer in (alpha.admin, alpha.first_bob):
        found = call(app, viewer, method, path, {'flair_id': flair.id, 'flair_title': 'PROBE'})
        absent = call(app, viewer, method, path, {'flair_id': NOBODY, 'flair_title': 'PROBE'})
        assert (found.status_code, found.get_data()) == \
            (absent.status_code, absent.get_data().replace(str(NOBODY).encode(), str(flair.id).encode()))
    db.session.expire_all()
    assert db.session.get(CommunityFlair, flair.id).flair == 'ONIONFLAIR'   # neither edited nor deleted
