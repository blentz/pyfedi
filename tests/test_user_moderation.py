"""Banning, blocking, reporting and deleting an account.

Sub-project 81, slice A -- the first of `app/user/routes.py`. Five defects,
all measured:

* a BANNED account could still file reports, each of which writes a Report row
  and a Notification for every admin (D1033) -- the abuse `community_report`
  was fixed against, at the other report route;
* reporting YOURSELF created a real report, notified every admin and
  incremented `user.reports`, the counter the admin queue reads (D1034);
* `user_block_instance` on a LOCAL profile blocked this instance for the
  caller, and said 'Content from None will be hidden.' (D1035);
* `delete_account`'s "this user cannot be deleted" guard sat on the GET branch
  only, so user 1 could delete their own account by POSTing the form (D1036);
* reporting a user whose `instance_id` is NULL was a 500 (D1037).

`find_local_user` matches on `ap_profile_id` or `alt_user_name`
(`app/activitypub/actor.py:29`), and `make_user` leaves both None for a local
account -- so `/u/<name>` resolves to nothing and every route here answers 404
before its own logic runs. The fixture sets `ap_profile_id`, which is what
registration does in production.
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import REPORT_TYPE_USER
from app.models import (Community, CommunityMember, Instance, Notification,
                        Report, Role, Site, User, UserBlock, user_role)
from tests.factories import (grant_permission, make_community,
                             make_community_member, make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """Fact 355."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def as_user(app, user):
    client = app.test_client()
    login(client, user)
    return client


def resolvable(user):
    """What registration sets and the factory does not."""
    user.ap_profile_id = f'https://test.piefed.local/u/{user.user_name}'
    db.session.commit()
    return user


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    viewer = make_user(local, 'viewer', local=True)
    target = make_user(local, 'target', local=True)
    db.session.commit()
    for user in (founder, viewer, target):
        resolvable(user)
    return as_user(app, viewer), founder, viewer, target


def a_remote_user(name='remote', host='other.example'):
    """`make_user` sets `ap_id` and `ap_profile_id` for a remote account but
    NOT `ap_domain`, which `user_block_instance` reads -- unlike
    `make_community` and `make_feed`, which do set it."""
    other = Instance.query.filter_by(domain=host).first() or make_instance(
        host, software='piefed')
    user = make_user(other, name)
    user.ap_domain = host
    db.session.commit()
    return user


def moderator_of_users(user):
    grant_permission(user, 'ban users')
    db.session.commit()
    return user


# --------------------------------------------------------------------------
# D1033, D1034, D1037 -- reporting a profile
# --------------------------------------------------------------------------


def report_payload(token, **overrides):
    data = {'reasons': ['1'], 'description': 'spam', 'report_remote': 'y',
            'submit': 'Report', 'csrf_token': token}
    data.update(overrides)
    return data


def test_a_profile_can_be_reported(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/report',
                           data=report_payload(token))

    assert response.status_code == 302
    report = Report.query.one()
    assert report.suspect_user_id == target.id
    assert report.reporter_id == viewer.id
    assert report.type == REPORT_TYPE_USER
    assert report.description == 'spam'
    db.session.refresh(target)
    assert target.reports == 1


def test_reporting_notifies_every_admin(app, env):
    """The Notification per admin is the whole cost of a report, and the
    reason a banned account must not be able to file one."""
    client, founder, viewer, target = env
    token = csrf(app, client)

    client.post(f'/u/{target.user_name}/report', data=report_payload(token))

    notifications = Notification.query.filter_by(user_id=founder.id).all()
    assert len(notifications) == 1
    assert notifications[0].title == 'Reported user'
    db.session.refresh(founder)
    assert founder.unread_notifications == 1


def test_a_banned_account_cannot_report(app, env):
    """D1033. Measured before the fix: `PROBE u1 reports created: 1 / admin
    notifications: 1`, from an account already barred from posting."""
    client, founder, viewer, target = env
    viewer.banned = True
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/report',
                           data=report_payload(token))

    assert response.status_code == 302
    assert Report.query.count() == 0
    assert Notification.query.count() == 0


def test_you_cannot_report_yourself(app, env):
    """D1034. `user.reports` is the counter the "already assessed" message and
    the admin queue both read, so a self-report is a free way to spend
    moderator attention. Measured: `PROBE u2 reports created: 1`."""
    client, founder, viewer, target = env
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        response = client.post(f'/u/{viewer.user_name}/report',
                               data=report_payload(token))

    assert response.status_code == 302
    assert Report.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot report yourself' in messages


def test_reporting_a_user_with_no_instance_row(app, env):
    """D1037. `User.instance_id` is nullable and `source_instance.domain` was
    read unguarded -- `AttributeError: 'NoneType' object has no attribute
    'domain'`, measured.

    The guard that carries this row is the one on `.domain`; the companion
    `if user.instance_id` before `db.session.get` only avoids a SQLAlchemy
    warning, and reverting it alone leaves every row here green. Recorded
    rather than pretended otherwise."""
    client, founder, viewer, target = env
    target.instance_id = None
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/report',
                           data=report_payload(token))

    assert response.status_code == 302
    report = Report.query.one()
    assert report.targets['source_instance_domain'] is None


def test_a_banned_user_cannot_be_reported(app, env):
    """A banned account is not reportable, and the refusal comes from the
    LOOKUP rather than from `if user and not user.banned:` further down:
    `find_local_user` filters `banned=False` unless `allow_banned=True`
    (`app/activitypub/actor.py:29`), which only the unban route passes. So the
    answer is 404, and the `not user.banned` guard below is reachable only for
    a remote account whose ban arrived after it was found."""
    client, founder, viewer, target = env
    target.banned = True
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/report',
                           data=report_payload(token))

    assert response.status_code == 404
    assert Report.query.count() == 0


def test_a_user_whose_reports_are_ignored_says_so(app, env):
    """`user.reports == -1` is how a moderator marks further reports
    unnecessary, and the page says so before the form is submitted."""
    client, founder, viewer, target = env
    target.reports = -1
    db.session.commit()

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template', return_value='rendered'):
            client.get(f'/u/{target.user_name}/report')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'no further reports are necessary' in messages


def test_a_report_for_an_ignored_user_is_not_stored(app, env):
    """The same flag, checked again after submission -- and this arm is the
    one that decides, because the flash above is only a message."""
    client, founder, viewer, target = env
    target.reports = -1
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/report',
                           data=report_payload(token))

    assert response.status_code == 302
    assert Report.query.count() == 0


def test_the_report_form_defaults_to_forwarding(app, env):
    """`elif request.method == 'GET': form.report_remote.data = True` -- a
    report about a remote account is worth sending to the instance that hosts
    it, so the box starts ticked."""
    client, founder, viewer, target = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/u/{target.user_name}/report')

    assert render.call_args.kwargs['form'].report_remote.data is True


def test_reporting_an_unknown_profile_is_a_404(app, env):
    client, founder, viewer, target = env

    response = client.get('/u/nobody/report')

    assert response.status_code == 404


def test_a_remote_profile_can_be_reported_by_handle(app, env):
    """The `'@' in actor` arm resolves a remote account by its handle rather
    than by a local URL."""
    client, founder, viewer, target = env
    remote = a_remote_user()
    token = csrf(app, client)

    response = client.post('/u/remote@other.example/report',
                           data=report_payload(token))

    assert response.status_code == 302
    assert Report.query.one().suspect_user_id == remote.id


# --------------------------------------------------------------------------
# D1035 -- blocking an instance from a profile
# --------------------------------------------------------------------------


def test_blocking_a_remote_users_instance(app, env):
    client, founder, viewer, target = env
    remote = a_remote_user()
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance') as block:
        response = client.post('/u/remote@other.example/block_instance',
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert block.call_args.args[0] == remote.instance_id


def test_you_cannot_block_your_own_instance(app, env):
    """D1035. A local profile's `instance_id` is this instance, so this
    blocked the whole site for the caller -- and the message it flashed was
    'Content from None will be hidden.', because a local user has no
    `ap_domain`. Measured: `PROBE u3 block_remote_instance called with:
    (1, 1)`."""
    client, founder, viewer, target = env
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance') as block:
        with patch('app.user.routes.flash') as flashed:
            response = client.post(f'/u/{target.user_name}/block_instance',
                                   data={'csrf_token': token})

    assert response.status_code == 302
    assert block.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot block this instance' in messages


def test_blocking_the_instance_of_an_unknown_profile_is_a_404(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    response = client.post('/u/nobody/block_instance',
                           data={'csrf_token': token})

    assert response.status_code == 404


def test_blocking_an_instance_from_htmx_redirects_home(app, env):
    """The htmx arm answers with `HX-Redirect` rather than a 302, and lands on
    the index when the current page is the blocked instance's or a profile --
    both of which are about to be empty."""
    client, founder, viewer, target = env
    a_remote_user()
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance'):
        response = client.post('/u/remote@other.example/block_instance',
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true',
                                        'HX-Current-Url': 'https://test.piefed.local/u/remote@other.example'})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == '/home'


def test_blocking_an_instance_from_elsewhere_stays_put(app, env):
    """The `else` arm: a page that is not about that instance keeps the
    reader where they were."""
    client, founder, viewer, target = env
    a_remote_user()
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance'):
        response = client.post('/u/remote@other.example/block_instance',
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true',
                                        'HX-Current-Url': 'https://test.piefed.local/c/general'})

    assert response.headers['HX-Redirect'] == 'https://test.piefed.local/c/general'


# --------------------------------------------------------------------------
# Blocking and unblocking a person
# --------------------------------------------------------------------------


def test_blocking_a_person(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/block',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert UserBlock.query.filter_by(blocker_id=viewer.id,
                                     blocked_id=target.id).count() == 1


def test_blocking_someone_twice_stores_one_block(app, env):
    """`if not existing_block:` -- the button is idempotent, and
    `UserBlock` has no unique constraint to catch a second row."""
    client, founder, viewer, target = env
    token = csrf(app, client)

    client.post(f'/u/{target.user_name}/block', data={'csrf_token': token})
    client.post(f'/u/{target.user_name}/block', data={'csrf_token': token})

    assert UserBlock.query.filter_by(blocker_id=viewer.id,
                                     blocked_id=target.id).count() == 1


def test_blocking_someone_drops_their_notification_subscriptions(app, env):
    """The raw DELETE alongside the block: staying subscribed to somebody you
    have just blocked would keep notifying you about them."""
    from app.models import NotificationSubscription

    client, founder, viewer, target = env
    db.session.add(NotificationSubscription(user_id=target.id,
                                            entity_id=viewer.id, type=0,
                                            name='a subscription'))
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/u/{target.user_name}/block', data={'csrf_token': token})

    assert NotificationSubscription.query.count() == 0


def test_you_cannot_block_yourself(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        client.post(f'/u/{viewer.user_name}/block', data={'csrf_token': token})

    assert UserBlock.query.count() == 0
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot block yourself' in messages


def test_blocking_an_unknown_profile_is_a_404(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    response = client.post('/u/nobody/block', data={'csrf_token': token})

    assert response.status_code == 404


def test_unblocking_a_person(app, env):
    client, founder, viewer, target = env
    db.session.add(UserBlock(blocker_id=viewer.id, blocked_id=target.id))
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/unblock',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert UserBlock.query.count() == 0


def test_unblocking_someone_who_is_not_blocked(app, env):
    """`if existing_block:` -- the message is the same either way, so the row
    has to assert that nothing was deleted rather than what was said."""
    client, founder, viewer, target = env
    other_block = UserBlock(blocker_id=target.id, blocked_id=viewer.id)
    db.session.add(other_block)
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/u/{target.user_name}/unblock', data={'csrf_token': token})

    assert UserBlock.query.count() == 1


def test_you_cannot_unblock_yourself(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        client.post(f'/u/{viewer.user_name}/unblock',
                    data={'csrf_token': token})

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot unblock yourself' in messages


def test_unblocking_an_unknown_profile_is_a_404(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    response = client.post('/u/nobody/unblock', data={'csrf_token': token})

    assert response.status_code == 404


@pytest.mark.parametrize('current_url, expected', [
    ('https://test.piefed.local/user/settings', 'https://test.piefed.local/user/settings'),
    ('https://test.piefed.local/c/general', '/home'),
])
def test_blocking_from_htmx_decides_where_to_land(app, env, current_url,
                                                  expected):
    """A block made from the settings page leaves the reader there; one made
    from a page that is about to lose its content sends them home."""
    client, founder, viewer, target = env
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/block',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': current_url})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == expected


def test_unblocking_from_htmx_stays_put(app, env):
    client, founder, viewer, target = env
    db.session.add(UserBlock(blocker_id=viewer.id, blocked_id=target.id))
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/unblock',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': 'https://test.piefed.local/user/settings'})

    assert response.headers['HX-Redirect'] == 'https://test.piefed.local/user/settings'


# --------------------------------------------------------------------------
# Banning and unbanning
# --------------------------------------------------------------------------


def test_a_moderator_can_open_the_ban_form(app, env):
    client, founder, viewer, target = env
    moderator_of_users(viewer)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/u/{target.user_name}/ban')

    assert response.status_code == 200
    assert render.call_args.kwargs['user'].id == target.id


def test_the_ban_form_opens_with_both_boxes_ticked(app, env):
    """`ip_address` and `purge` default to on, because the usual case is a
    spam account whose content should go with it."""
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    grant_permission(viewer, 'manage users')
    target.ip_address = '203.0.113.5'
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/u/{target.user_name}/ban')

    form = render.call_args.kwargs['form']
    assert form.ip_address.data is True
    assert form.purge.data is True


def test_purging_is_disabled_without_the_permission(app, env):
    """`if not user_access('manage users', ...)` -- deleting the account's
    content is a heavier right than banning it, and the two are separate."""
    client, founder, viewer, target = env
    moderator_of_users(viewer)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/u/{target.user_name}/ban')

    form = render.call_args.kwargs['form']
    assert form.purge.data is False
    assert form.purge.render_kw == {'disabled': True}


def test_banning_an_ip_is_disabled_when_there_is_none(app, env):
    """An account that has never had an IP recorded cannot have one banned,
    and the box would otherwise silently do nothing."""
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    target.ip_address = None
    db.session.commit()

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/u/{target.user_name}/ban')

    form = render.call_args.kwargs['form']
    assert form.ip_address.data is False
    assert form.ip_address.render_kw == {'disabled': True}


def test_submitting_the_ban_form_bans(app, env):
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    token = csrf(app, client)

    with patch('app.shared.user.ban_user') as ban:
        response = client.post(f'/u/{target.user_name}/ban',
                               data={'reason': 'spam', 'submit': 'Ban',
                                     'csrf_token': token})

    assert response.status_code == 302
    assert ban.call_args.args[0].person_id == target.id


def test_you_cannot_ban_yourself(app, env):
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    token = csrf(app, client)

    with patch('app.shared.user.ban_user') as ban:
        with patch('app.user.routes.flash') as flashed:
            response = client.post(f'/u/{viewer.user_name}/ban',
                                   data={'reason': 'spam', 'submit': 'Ban',
                                         'csrf_token': token})

    assert response.status_code == 302
    assert ban.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot ban yourself' in messages


def make_admin(user):
    """`User.is_admin()` looks for a role NAMED 'Admin', not a permission."""
    role = Role.query.filter_by(name='Admin').first() or Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return user


def test_a_moderator_cannot_ban_an_administrator(app, env):
    """D1178, fixed (owner ruling 2026-09-30): only an admin may ban an admin,
    on the web as on the API."""
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    make_admin(target)
    token = csrf(app, client)

    with patch('app.shared.user.ban_user') as ban:
        with patch('app.user.routes.flash') as flashed:
            response = client.post(f'/u/{target.user_name}/ban',
                                   data={'reason': 'spam', 'submit': 'Ban',
                                         'csrf_token': token})

    assert response.status_code == 302
    assert ban.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot ban an administrator' in messages


def test_an_administrator_can_ban_another(app, env):
    client, founder, viewer, target = env
    moderator_of_users(make_admin(viewer))  # a real Admin role carries 'ban users'
    make_admin(target)
    token = csrf(app, client)

    with patch('app.shared.user.ban_user') as ban:
        client.post(f'/u/{target.user_name}/ban',
                    data={'reason': 'spam', 'submit': 'Ban', 'csrf_token': token})

    assert ban.call_args.args[0].person_id == target.id


def test_nobody_can_ban_user_1(app, env):
    """D1178: not even another admin may ban the founder."""
    client, founder, viewer, target = env
    moderator_of_users(make_admin(viewer))
    token = csrf(app, client)

    with patch('app.shared.user.ban_user') as ban:
        response = client.post(f'/u/{founder.user_name}/ban',
                               data={'reason': 'spam', 'submit': 'Ban',
                                     'csrf_token': token})

    assert response.status_code == 302
    assert ban.call_args is None


def test_someone_without_the_permission_cannot_ban(app, env):
    client, founder, viewer, target = env

    response = client.get(f'/u/{target.user_name}/ban')

    assert response.status_code == 401


def test_banning_an_unknown_profile_is_a_404(app, env):
    client, founder, viewer, target = env
    moderator_of_users(viewer)

    response = client.get('/u/nobody/ban')

    assert response.status_code == 404


def test_unbanning_a_user(app, env):
    """`allow_banned=True` on the lookup is what makes this route work at all
    -- the account it acts on is by definition banned, and the ordinary
    lookup filters those out."""
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    target.banned = True
    db.session.commit()
    token = csrf(app, client)

    with patch('app.shared.user.unban_user') as unban:
        response = client.post(f'/u/{target.user_name}/unban',
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert unban.call_args.args[0] == {'person_id': target.id}


def test_you_cannot_unban_yourself(app, env):
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    token = csrf(app, client)

    with patch('app.shared.user.unban_user') as unban:
        with patch('app.user.routes.flash') as flashed:
            client.post(f'/u/{viewer.user_name}/unban',
                        data={'csrf_token': token})

    assert unban.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot unban yourself' in messages


def test_someone_without_the_permission_cannot_unban(app, env):
    client, founder, viewer, target = env
    target.banned = True
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/unban',
                           data={'csrf_token': token})

    assert response.status_code == 401


def test_unbanning_an_unknown_profile_is_a_404(app, env):
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    token = csrf(app, client)

    response = client.post('/u/nobody/unban', data={'csrf_token': token})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Deleting somebody else's account
# --------------------------------------------------------------------------


def test_an_administrator_can_delete_an_account(app, env):
    client, founder, viewer, target = env
    grant_permission(viewer, 'manage users')
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/delete',
                           data={'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(target)
    assert target.deleted is True
    assert target.banned is True
    assert target.deleted_by == viewer.id


def test_deleting_an_account_is_recorded_in_the_modlog(app, env):
    from app.models import ModLog

    client, founder, viewer, target = env
    grant_permission(viewer, 'manage users')
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/u/{target.user_name}/delete', data={'csrf_token': token})

    assert ModLog.query.filter_by(action='delete_user').count() == 1


def test_the_first_account_cannot_be_deleted(app, env):
    """User 1 is the instance's first administrator; an instance that lost it
    would have no way back."""
    client, founder, viewer, target = env
    grant_permission(viewer, 'manage users')
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/u/{founder.user_name}/delete', data={'csrf_token': token})

    db.session.refresh(founder)
    assert founder.deleted is False


def test_you_cannot_delete_yourself_from_here(app, env):
    """There is a separate route for that, and it asks for confirmation."""
    client, founder, viewer, target = env
    grant_permission(viewer, 'manage users')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        client.post(f'/u/{viewer.user_name}/delete',
                    data={'csrf_token': token})

    db.session.refresh(viewer)
    assert viewer.deleted is False
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot delete yourself' in messages


def test_deleting_a_remote_instance_admin_says_so(app, env):
    """`user.is_instance_admin()` is read AFTER `delete_dependencies()`, and
    that is the only reason this warning can still fire: it is a column on the
    user row, not a role. The sibling warning about role permissions cannot
    fire, which is recorded as D1038 rather than covered."""
    client, founder, viewer, target = env
    grant_permission(viewer, 'manage users')
    remote = a_remote_user()
    remote.instance_id = Instance.query.filter_by(
        domain='other.example').first().id
    db.session.commit()
    token = csrf(app, client)

    with patch('app.models.User.is_instance_admin', return_value=True):
        with patch('app.user.routes.flash') as flashed:
            client.post('/u/remote@other.example/delete',
                        data={'csrf_token': token})

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'remote instance admin' in messages


def test_someone_without_the_permission_cannot_delete_an_account(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    response = client.post(f'/u/{target.user_name}/delete',
                           data={'csrf_token': token})

    assert response.status_code == 401
    db.session.refresh(target)
    assert target.deleted is False


def test_deleting_an_unknown_profile_is_a_404(app, env):
    client, founder, viewer, target = env
    grant_permission(viewer, 'manage users')
    db.session.commit()
    token = csrf(app, client)

    response = client.post('/u/nobody/delete', data={'csrf_token': token})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1036 -- deleting your own account
# --------------------------------------------------------------------------


def test_deleting_your_own_account(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    with patch('app.user.routes.send_deletion_requests') as requests:
        response = client.post('/delete_account',
                               data={'submit': 'Delete',
                                     'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(viewer)
    assert viewer.banned is True
    assert viewer.email == f'deleted_{viewer.id}@deleted.com'
    assert viewer.deleted_by == viewer.id
    assert requests.delay.call_args.args == (viewer.id,)


def test_deleting_your_own_account_logs_you_out(app, env):
    """`logout_user()` is what stops the session outliving the account -- the
    row is banned, not removed, so a surviving session would still be able to
    read as them. Asserted by asking for a page that requires a login, rather
    than by reading the session cookie."""
    client, founder, viewer, target = env
    token = csrf(app, client)

    with patch('app.user.routes.send_deletion_requests'):
        client.post('/delete_account',
                    data={'submit': 'Delete', 'csrf_token': token})

    after = client.get('/user/settings')

    assert after.status_code == 302
    assert '/auth/login' in after.headers['Location']


def test_the_first_account_cannot_delete_itself(app, env):
    """D1036. This guard sat on the GET branch ONLY, so the refusal it states
    was advice rather than a rule -- user 1 could delete their own account by
    POSTing the form directly. Measured: `PROBE u4 founder banned? True email:
    deleted_1@deleted.com`."""
    client, founder, viewer, target = env
    admin = as_user(app, founder)
    token = csrf(app, admin)

    with patch('app.user.routes.send_deletion_requests') as requests:
        response = admin.post('/delete_account',
                              data={'submit': 'Delete', 'csrf_token': token})

    assert response.status_code == 302
    db.session.refresh(founder)
    assert founder.banned is False
    assert founder.email == 'founder@example.com'
    assert requests.delay.call_args is None


def test_the_first_account_is_refused_the_form_too(app, env):
    client, founder, viewer, target = env
    admin = as_user(app, founder)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = admin.get('/delete_account')

    assert response.status_code == 302
    assert render.call_args is None


def test_the_deletion_form_is_shown_to_everyone_else(app, env):
    client, founder, viewer, target = env

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/delete_account')

    assert response.status_code == 200
    # `current_user` is a LocalProxy, and reading `.id` off the one the mock
    # captured AFTER the request has ended answers None rather than raising.
    # Assert on the template instead.
    assert render.call_args.args[0] == 'user/delete_account.html'


def test_the_deletion_task_unsubscribes_and_federates(app, env):
    """`send_deletion_requests` leaves every community and sends a Delete to
    every instance that is neither dormant nor gone -- and never to instance
    1, which is this one."""
    from app.user.routes import send_deletion_requests

    client, founder, viewer, target = env
    community = make_community('general')
    db.session.commit()
    make_community_member(viewer, community)
    other = make_instance('other.example', software='piefed')
    other.inbox = 'https://other.example/inbox'
    dormant = make_instance('dormant.example', software='piefed')
    dormant.inbox = 'https://dormant.example/inbox'
    dormant.dormant = True
    db.session.commit()

    with patch('app.user.routes.unsubscribe_from_community') as unsubscribe:
        with patch('app.user.routes.send_post_request') as send:
            with patch('app.models.Instance.online', return_value=True):
                send_deletion_requests(viewer.id)

    assert unsubscribe.call_args.args[0].id == community.id
    assert [call.args[0] for call in send.call_args_list] == [
        'https://other.example/inbox']
    db.session.refresh(viewer)
    assert viewer.deleted is True


def test_the_deletion_task_survives_a_missing_user(app, env):
    """`if user:` -- the task is queued and the row may be gone by the time it
    runs."""
    from app.user.routes import send_deletion_requests

    client, founder, viewer, target = env

    with patch('app.user.routes.send_post_request') as send:
        send_deletion_requests(9999)

    assert send.call_args is None


def test_deleting_an_account_with_role_permissions_says_so(app, env):
    """D1038. This warning was dead code: `is_admin()` and `is_staff()` both
    walk `self.roles`, and they were read AFTER `delete_dependencies()`, which
    executes `DELETE FROM "user_role" WHERE user_id = ...`. So the only
    account that could ever trigger it was user 1, whom the route refuses to
    delete two lines above. The flags are now read first."""
    client, founder, viewer, target = env
    grant_permission(viewer, 'manage users')
    grant_permission(target, 'change instance settings')
    target.roles[0].name = 'Admin'
    db.session.commit()
    assert target.is_admin()
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        client.post(f'/u/{target.user_name}/delete',
                    data={'csrf_token': token})

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'role permissions' in messages


def test_deleting_an_ordinary_account_says_nothing_extra(app, env):
    """The control: the two warnings must not fire for an account that held
    neither role permissions nor an instance-admin flag."""
    client, founder, viewer, target = env
    grant_permission(viewer, 'manage users')
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.flash') as flashed:
        client.post(f'/u/{target.user_name}/delete',
                    data={'csrf_token': token})

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'role permissions' not in messages
    assert 'instance admin' not in messages


# --------------------------------------------------------------------------
# The remaining arms: remote handles, debug mode, and the two unblock routes
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path, extra', [
    ('ban', {'reason': 'spam', 'submit': 'Ban'}),
    ('block', {}),
    ('unblock', {}),
])
def test_a_remote_handle_resolves_for_every_action(app, env, path, extra):
    """Each of these routes has two lookup arms -- `'@' in actor` and the
    local URL -- and the handle arm is a separate line in each function."""
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    remote = a_remote_user()
    token = csrf(app, client)
    data = {'csrf_token': token}
    data.update(extra)

    with patch('app.shared.user.ban_user'):
        response = client.post(f'/u/remote@other.example/{path}', data=data)

    assert response.status_code == 302


def test_unbanning_a_remote_account_by_handle(app, env):
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    remote = a_remote_user()
    remote.banned = True
    db.session.commit()
    token = csrf(app, client)

    with patch('app.shared.user.unban_user') as unban:
        response = client.post('/u/remote@other.example/unban',
                               data={'csrf_token': token})

    assert response.status_code == 302
    assert unban.call_args.args[0] == {'person_id': remote.id}


def test_blocking_an_instance_from_its_own_page_goes_home(app, env):
    """The first arm of the htmx branch: the reader is ON the blocked
    instance's page, named by `user.ap_domain`."""
    client, founder, viewer, target = env
    a_remote_user()
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance'):
        response = client.post('/u/remote@other.example/block_instance',
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true',
                                        'HX-Current-Url': 'https://other.example/some/page'})

    assert response.headers['HX-Redirect'] == '/home'


def test_deleting_your_own_account_runs_the_task_inline_in_debug(app, env):
    """`if current_app.debug:` -- a developer gets the federation attempt in
    the request rather than on a worker."""
    client, founder, viewer, target = env
    token = csrf(app, client)
    app.debug = True
    try:
        with patch('app.user.routes.send_deletion_requests') as requests:
            client.post('/delete_account',
                        data={'submit': 'Delete', 'csrf_token': token})
    finally:
        app.debug = False

    assert requests.call_args.args == (viewer.id,)
    assert requests.delay.call_args is None


def test_unblocking_a_community(app, env):
    from app.models import CommunityBlock

    client, founder, viewer, target = env
    community = make_community('general')
    db.session.commit()
    db.session.add(CommunityBlock(user_id=viewer.id, community_id=community.id))
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/user/community/{community.id}/unblock',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert CommunityBlock.query.count() == 0


def test_unblocking_a_community_that_is_not_blocked(app, env):
    from app.models import CommunityBlock

    client, founder, viewer, target = env
    community = make_community('general')
    db.session.commit()
    db.session.add(CommunityBlock(user_id=target.id,
                                  community_id=community.id))
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/user/community/{community.id}/unblock',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert CommunityBlock.query.count() == 1


def test_unblocking_an_unknown_community_is_a_404(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    response = client.post('/user/community/9999/unblock',
                           data={'csrf_token': token})

    assert response.status_code == 404


@pytest.mark.parametrize('current_url, expected', [
    ('https://test.piefed.local/user/settings',
     'https://test.piefed.local/user/settings'),
    ('https://test.piefed.local/c/general', '/home'),
])
def test_unblocking_a_community_from_htmx(app, env, current_url, expected):
    from app.models import CommunityBlock

    client, founder, viewer, target = env
    community = make_community('general')
    db.session.commit()
    db.session.add(CommunityBlock(user_id=viewer.id, community_id=community.id))
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/user/community/{community.id}/unblock',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': current_url})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == expected


def a_flair(community, name='Question'):
    from app.models import CommunityFlair

    flair = CommunityFlair(community_id=community.id, flair=name,
                           text_color='#000000', background_color='#ffffff')
    db.session.add(flair)
    db.session.commit()
    return flair


def test_unblocking_flair(app, env):
    from app.models import CommunityFlairBlock

    client, founder, viewer, target = env
    community = make_community('general')
    db.session.commit()
    flair = a_flair(community)
    db.session.add(CommunityFlairBlock(user_id=viewer.id,
                                       community_id=community.id,
                                       community_flair_id=flair.id))
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/user/flair/{flair.id}/unblock',
                           data={'csrf_token': token})

    assert response.status_code == 302
    assert CommunityFlairBlock.query.count() == 0


def test_unblocking_flair_that_is_not_blocked(app, env):
    from app.models import CommunityFlairBlock

    client, founder, viewer, target = env
    community = make_community('general')
    db.session.commit()
    flair = a_flair(community)
    db.session.add(CommunityFlairBlock(user_id=target.id,
                                       community_id=community.id,
                                       community_flair_id=flair.id))
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/user/flair/{flair.id}/unblock', data={'csrf_token': token})

    assert CommunityFlairBlock.query.count() == 1


def test_unblocking_unknown_flair_is_a_404(app, env):
    client, founder, viewer, target = env
    token = csrf(app, client)

    response = client.post('/user/flair/9999/unblock',
                           data={'csrf_token': token})

    assert response.status_code == 404


@pytest.mark.parametrize('current_url, expected', [
    ('https://test.piefed.local/user/settings',
     'https://test.piefed.local/user/settings'),
    ('https://test.piefed.local/c/general', '/home'),
])
def test_unblocking_flair_from_htmx(app, env, current_url, expected):
    from app.models import CommunityFlairBlock

    client, founder, viewer, target = env
    community = make_community('general')
    db.session.commit()
    flair = a_flair(community)
    db.session.add(CommunityFlairBlock(user_id=viewer.id,
                                       community_id=community.id,
                                       community_flair_id=flair.id))
    db.session.commit()
    token = csrf(app, client)

    response = client.post(f'/user/flair/{flair.id}/unblock',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true',
                                    'HX-Current-Url': current_url})

    assert response.status_code == 200
    assert response.headers['HX-Redirect'] == expected


def test_blocking_an_instance_from_another_profile_goes_home(app, env):
    """The middle arm: the reader is on SOME profile page -- not one on the
    instance being blocked -- and that page may be about to lose its author,
    so they go home rather than stay."""
    client, founder, viewer, target = env
    a_remote_user()
    token = csrf(app, client)

    with patch('app.user.routes.block_remote_instance'):
        response = client.post('/u/remote@other.example/block_instance',
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true',
                                        'HX-Current-Url': 'https://test.piefed.local/u/target'})

    assert response.headers['HX-Redirect'] == '/home'


def test_a_banned_remote_account_cannot_be_reported(app, env):
    """`if user and not user.banned:` is reachable only for a REMOTE account.
    `find_local_user` filters `banned=False`, so a banned local profile is a
    404 before this line; `find_remote_actor` does not filter at all for a
    user URL (`app/activitypub/actor.py:91`), so a banned remote account is
    found and this guard is what stops the report."""
    client, founder, viewer, target = env
    remote = a_remote_user()
    remote.banned = True
    db.session.commit()
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        response = client.post('/u/remote@other.example/report',
                               data=report_payload(token))

    assert response.status_code == 200
    assert Report.query.count() == 0


def test_unblocking_does_not_touch_somebody_elses_block(app, env):
    """The unblock query is keyed on `blocker_id` as well as `blocked_id`.
    Without the first half it would delete whichever block of that person it
    found -- somebody else's."""
    client, founder, viewer, target = env
    db.session.add(UserBlock(blocker_id=founder.id, blocked_id=target.id))
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/u/{target.user_name}/unblock', data={'csrf_token': token})

    remaining = UserBlock.query.one()
    assert remaining.blocker_id == founder.id


def test_the_deletion_task_never_federates_to_this_instance(app, env):
    """`instance.id != 1` -- instance 1 is this one, and its own row can carry
    an inbox. Without the check the server would POST a Delete about its own
    user to itself. The row has to GIVE instance 1 an inbox, because a row
    that leaves it None is passed by `instance.inbox` regardless."""
    from app.user.routes import send_deletion_requests

    client, founder, viewer, target = env
    local = Instance.query.filter_by(domain='test.piefed.local').one()
    local.inbox = 'https://test.piefed.local/inbox'
    other = make_instance('other.example', software='piefed')
    other.inbox = 'https://other.example/inbox'
    db.session.commit()

    with patch('app.user.routes.send_post_request') as send:
        with patch('app.models.Instance.online', return_value=True):
            send_deletion_requests(viewer.id)

    assert [call.args[0] for call in send.call_args_list] == [
        'https://other.example/inbox']
