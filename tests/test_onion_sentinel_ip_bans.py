"""The onion accounts' shared sentinel IP counts as no IP in the IP-ban feature (onion unlinkability, goal 2a):
it joins no accounts as "same IP", offers no "ban IP" option, and is never banned. A real address behaves as before."""
from unittest.mock import patch

from flask_login import login_user

from app import db
from app.constants import SRC_API
from app.models import IpBan
from app.onion import ONION_ACCOUNT_IP
from app.shared.user import ban_user
from app.user.utils import _get_user_same_ip
from tests.factories import bearer, make_user
from tests.test_shared_user_bans import _recording_task_selector, _seed_ban_scenario
from tests.test_user_moderation import env, moderator_of_users  # noqa: F401

REAL_IP = '203.0.113.7'


def same_ip_pair(ip):
    s = _seed_ban_scenario()
    s.target.ip_address = ip
    other = make_user(s.instance, 'altaccount', local=True)
    other.ip_address = ip
    db.session.commit()
    return s, other


def test_same_ip_list_empty_for_onion_accounts(app, db_session):
    s, other = same_ip_pair(ONION_ACCOUNT_IP)
    with app.test_request_context('/'):
        login_user(db.session.get(type(s.admin), 1))   # user 1 is an admin
        assert _get_user_same_ip(s.target) == []


def test_ban_ip_never_bans_the_sentinel(app, db_session):
    s = _seed_ban_scenario()
    s.target.ip_address = ONION_ACCOUNT_IP
    db.session.commit()
    with app.test_request_context('/'), _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': False, 'ban_ip_address': True, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))
    assert IpBan.query.count() == 0


def test_ban_form_hides_ip_option_without_a_real_ip(app, site, env):
    client, founder, viewer, target = env
    moderator_of_users(viewer)
    target.ip_address = ONION_ACCOUNT_IP
    db.session.commit()
    with patch('app.user.routes.render_template', return_value='rendered') as render:
        client.get(f'/u/{target.user_name}/ban')
    form = render.call_args.kwargs['form']
    assert form.ip_address.data is False
    assert form.ip_address.render_kw == {'disabled': True}


def test_clearnet_same_ip_and_ip_ban_unchanged(app, db_session):
    s, other = same_ip_pair(REAL_IP)
    with app.test_request_context('/'):
        login_user(db.session.get(type(s.admin), 1))
        assert _get_user_same_ip(s.target) == [other]
    with app.test_request_context('/'), _recording_task_selector():
        ban_user({'person_id': s.target.id, 'purge_content': False, 'ban_ip_address': True, 'reason': 'spam'},
                 SRC_API, bearer(s.admin))
    assert [ban.ip_address for ban in IpBan.query.all()] == [REAL_IP]
