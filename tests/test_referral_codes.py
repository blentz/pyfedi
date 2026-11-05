"""Referral-code registration (spec docs/superpowers/specs/2026-10-31-referral-codes-design.md, rulings RC1-RC13)."""
import uuid
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Instance, Referral, Role, Site, SiteDomain, User, UserRegistration, role_grant
from app.referral import (REFERRAL_MODE, derive_code, generate_code, may_generate, open_registrations, record_referral,
                          referral_link, referral_namespace, referral_qr_svg, referrer_for, remaining_signups)
from app.storefront import clear_domain_cache, default_domain, domain_by_id, registration_mode_on
from app.utils import get_setting, set_setting
from tests.factories import staff_role, make_instance, make_user
from tests.test_admin_routes_entry import _language, _valid_misc_payload, csrf, login

pytestmark = pytest.mark.usefixtures('site')

SERVER = 'test.piefed.local'


def _mode(mode=REFERRAL_MODE):
    site = db.session.get(Site, 1)
    site.registration_mode = mode
    site.private_instance = False
    site.tos_url = None
    site.application_question = 'Enter your referral code'
    db.session.commit()


def _role(user, name, weight):
    role = staff_role() if name == 'Staff' else Role.query.filter_by(name=name).first()
    if role is None:
        role = Role(name=name, built_in=name == 'Admin')   # Admin is the built-in role (RB16)
        db.session.add(role)
        db.session.commit()
    db.session.execute(role_grant.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()


@pytest.fixture
def people(app, db_session):
    """founder (id 1, admin), an Admin-role user, a Staff-role user and a plain local user, all verified."""
    _mode()
    local = Instance.query.filter_by(domain=SERVER).first() or make_instance(SERVER, software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    admin = make_user(local, 'adminuser', local=True)
    _role(admin, 'Admin', 0)
    staff = make_user(local, 'staffuser', local=True)
    _role(staff, 'Staff', 5)
    plain = make_user(local, 'plainuser', local=True)
    remote = make_user(make_instance('remote.example'), 'faraway')
    return {'founder': founder, 'admin': admin, 'staff': staff, 'plain': plain, 'remote': remote}


def _code(user):
    generate_code(user)
    db.session.commit()
    return user.referral_code


# --------------------------------------------------------------------------
# RC2: the code
# --------------------------------------------------------------------------

def test_the_namespace_is_uuid5_of_the_server_name(app):
    with app.app_context():
        assert referral_namespace() == uuid.uuid5(uuid.NAMESPACE_DNS, SERVER)


def test_the_code_is_a_uuid5_of_name_and_nonce(app):
    with app.app_context():
        code = derive_code('alice', 'n0nce')
        assert code == str(uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_DNS, SERVER), 'alice:n0nce'))
        assert uuid.UUID(code).version == 5


def test_the_bare_username_uuid_is_not_the_code(people):
    """RC2: uuid5(ns, username) is computable by anyone, so it must not be a working code."""
    code = _code(people['plain'])
    assert code != str(uuid.uuid5(referral_namespace(), 'plainuser'))
    assert referrer_for(str(uuid.uuid5(referral_namespace(), 'plainuser')), default_domain()) is None


def test_regenerating_changes_the_code_and_kills_the_old_one(people):
    set_setting('referral_generators', 'all')
    user = people['plain']
    first = _code(user)
    second = _code(user)
    assert first != second
    assert referrer_for(first, default_domain()) is None
    assert referrer_for(second, default_domain()).id == user.id


# --------------------------------------------------------------------------
# RC8: who may generate, checked at use time
# --------------------------------------------------------------------------

@pytest.mark.parametrize('setting, allowed', [
    ('admins', {'founder', 'admin'}),
    ('staff', {'founder', 'admin', 'staff'}),
    ('all', {'founder', 'admin', 'staff', 'plain'}),
])
def test_who_may_generate(people, setting, allowed):
    set_setting('referral_generators', setting)
    assert {name for name, user in people.items() if may_generate(user)} == allowed


def test_the_default_is_admins_only(people):
    assert get_setting('referral_generators', None) is None
    assert may_generate(people['admin']) is True
    assert may_generate(people['staff']) is False


@pytest.mark.parametrize('attribute, value', [('banned', True), ('deleted', True), ('verified', False)])
def test_a_banned_deleted_or_unverified_user_may_not(people, attribute, value):
    set_setting('referral_generators', 'all')
    user = people['plain']
    code = _code(user)
    setattr(user, attribute, value)
    db.session.commit()
    assert may_generate(user) is False
    assert referrer_for(code, default_domain()) is None


def test_narrowing_the_setting_disables_codes_and_widening_restores_them(people):
    set_setting('referral_generators', 'all')
    code = _code(people['plain'])
    set_setting('referral_generators', 'admins')
    assert referrer_for(code, default_domain()) is None
    set_setting('referral_generators', 'all')
    assert referrer_for(code, default_domain()).id == people['plain'].id


@pytest.mark.parametrize('text', ['', 'not-a-uuid', '0f6c3a52-8d1e-4b7a-9c2f-5e4d3b2a1c0e', '   '])
def test_unknown_or_malformed_codes_are_refused(people, text):
    _code(people['admin'])
    assert referrer_for(text, default_domain()) is None


def test_a_code_is_read_case_and_space_insensitively(people):
    code = _code(people['admin'])
    assert referrer_for(f'  {code.upper()} ', default_domain()).id == people['admin'].id


# --------------------------------------------------------------------------
# RC4: caps
# --------------------------------------------------------------------------

def _referred(referrer, name):
    user = make_user(Instance.query.filter_by(domain=SERVER).one(), name, local=True)
    record_referral(referrer, user)
    db.session.commit()
    return user


@pytest.mark.parametrize('per_code, per_user, expected', [
    (0, 0, (None, None)),   # both unlimited
    (5, 0, (4, None)),      # only the code is capped
    (0, 7, (6, 6)),         # only the lifetime is capped: the code can take no more than the lifetime has left
    (5, 7, (4, 6)),
    (5, 2, (1, 1)),         # the lifetime is the tighter limit
    (1, 7, (0, 6)),         # the code is used up; a regenerated one would have 6
])
def test_remaining_signups_per_code_and_lifetime(people, per_code, per_user, expected):
    set_setting('referral_generators', 'all')
    set_setting('referral_max_per_code', per_code)
    set_setting('referral_max_per_user', per_user)
    plain = people['plain']
    _code(plain)
    _referred(plain, 'left1')
    assert remaining_signups(plain) == expected


def test_remaining_signups_never_goes_negative_when_a_cap_is_lowered(people):
    set_setting('referral_generators', 'all')
    plain = people['plain']
    _code(plain)
    _referred(plain, 'over1')
    _referred(plain, 'over2')
    set_setting('referral_max_per_code', 1)
    set_setting('referral_max_per_user', 1)
    assert remaining_signups(plain) == (0, 0)


def test_remaining_signups_is_unlimited_for_admins(people):
    set_setting('referral_max_per_code', 1)
    set_setting('referral_max_per_user', 1)
    admin = people['admin']
    _code(admin)
    _referred(admin, 'byadmin')
    assert remaining_signups(admin) == (None, None)


def test_the_per_code_cap_and_regenerate_resets_it(people):
    set_setting('referral_generators', 'all')
    set_setting('referral_max_per_code', 2)
    user = people['plain']
    code = _code(user)
    _referred(user, 'one')
    assert referrer_for(code, default_domain()) is not None
    _referred(user, 'two')
    assert referrer_for(code, default_domain()) is None
    fresh = _code(user)
    assert referrer_for(fresh, default_domain()).id == user.id


def test_the_lifetime_cap_survives_regenerate(people):
    set_setting('referral_generators', 'all')
    set_setting('referral_max_per_user', 2)
    user = people['plain']
    _code(user)
    _referred(user, 'one')
    _code(user)
    _referred(user, 'two')
    assert referrer_for(_code(user), default_domain()) is None


def test_admins_are_exempt_from_both_caps(people):
    set_setting('referral_max_per_code', 1)
    set_setting('referral_max_per_user', 1)
    admin = people['admin']
    code = _code(admin)
    _referred(admin, 'one')
    _referred(admin, 'two')
    assert referrer_for(code, default_domain()).id == admin.id


def test_zero_caps_mean_unlimited(people):
    set_setting('referral_generators', 'all')
    set_setting('referral_max_per_code', 0)
    set_setting('referral_max_per_user', 0)
    user = people['plain']
    code = _code(user)
    for name in ('a1', 'a2', 'a3'):
        _referred(user, name)
    assert referrer_for(code, default_domain()) is not None


def test_record_referral_stores_the_code_used(people):
    admin = people['admin']
    code = _code(admin)
    referred = _referred(admin, 'newbie')
    row = Referral.query.filter_by(referred_id=referred.id).one()
    assert (row.referrer_id, row.code) == (admin.id, code)


# --------------------------------------------------------------------------
# RC9 / RC10: the referrer's home domain
# --------------------------------------------------------------------------

def _shop(registration='anyone'):
    row = SiteDomain(domain='shop.example', is_default=False, registration=registration)
    db.session.add(row)
    db.session.commit()
    clear_domain_cache()
    return row


def test_the_link_uses_the_referrers_home_domain(people):
    shop = _shop()
    admin = people['admin']
    admin.site_domain_id = shop.id
    code = _code(admin)
    with patch('app.referral.url_for', return_value=f'/auth/register?ref={code}'):
        assert referral_link(admin) == f'https://shop.example/auth/register?ref={code}'


def test_a_code_works_only_on_its_referrers_domain(people):
    shop = _shop()
    admin = people['admin']
    admin.site_domain_id = shop.id
    code = _code(admin)
    assert referrer_for(code, default_domain()) is None
    assert referrer_for(code, domain_by_id(shop.id)).id == admin.id


def test_the_qr_is_an_svg_of_the_link():
    svg = referral_qr_svg('https://test.piefed.local/auth/register?ref=x')
    assert svg.lstrip().startswith('<svg') and '</svg>' in svg


def test_the_qr_svg_has_its_own_white_background():
    """A transparent QR is unreadable on the dark theme: the light modules and the quiet zone are painted white."""
    svg = referral_qr_svg('https://test.piefed.local/auth/register?ref=x')
    assert '<path fill="#fff"' in svg


# --------------------------------------------------------------------------
# RC6: what is advertised
# --------------------------------------------------------------------------

def test_referral_mode_advertises_require_application_and_closed_registrations(people):
    site = db.session.get(Site, 1)
    assert registration_mode_on(default_domain(), site) == 'RequireApplication'
    assert open_registrations(default_domain(), site) is False
    site.registration_mode = 'Open'
    assert open_registrations(default_domain(), site) is True
    site.registration_mode = 'Closed'
    assert open_registrations(default_domain(), site) is False


def test_nodeinfo_says_registrations_are_not_open(app, people):
    from app import cache
    cache.clear()
    body = app.test_client().get('/nodeinfo/2.1').get_json()
    assert body['openRegistrations'] is False


# --------------------------------------------------------------------------
# RC3 / RC5 / RC7: the web form
# --------------------------------------------------------------------------

@pytest.fixture
def no_captcha():
    with patch('app.auth.forms.captcha_enabled', return_value=False):
        yield


def _register(client, question, name='newperson', **extra):
    data = {'user_name': name, 'hp_field': '', 'real_email': f'{name}@example.com', 'password': 'a-good-password',
            'password2': 'a-good-password', 'timezone': 'UTC', 'question': question, 'submit': 'Register'}
    data.update(extra)
    with patch('app.auth.util.send_email_verification'), patch('app.auth.util.sync_user_with_ldap'):
        return client.post('/auth/register', data=data)


@pytest.mark.parametrize('question', ['', 'please let me in', '0f6c3a52-8d1e-4b7a-9c2f-5e4d3b2a1c0e'])
def test_a_missing_or_wrong_code_is_refused_outright(app, people, no_captcha, question):
    _code(people['admin'])
    with patch('app.auth.util.render_template', return_value='rendered') as rt:
        _register(app.test_client(), question)
    assert User.query.filter_by(user_name='newperson').first() is None
    assert UserRegistration.query.count() == 0
    errors = rt.call_args.kwargs['form'].question.errors
    assert errors and (not question or 'Invalid referral code' in errors)


def test_a_valid_code_creates_an_approved_account_and_a_referral(app, people, no_captcha):
    admin = people['admin']
    code = _code(admin)
    client = app.test_client()
    response = _register(client, code)
    user = User.query.filter_by(user_name='newperson').one()
    assert response.status_code == 302
    assert UserRegistration.query.filter_by(user_id=user.id).count() == 0
    assert not user.waiting_for_approval()
    row = Referral.query.filter_by(referred_id=user.id).one()
    assert (row.referrer_id, row.code) == (admin.id, code)


def test_a_capped_code_is_refused_at_the_form(app, people, no_captcha):
    set_setting('referral_generators', 'all')
    set_setting('referral_max_per_code', 1)
    plain = people['plain']
    code = _code(plain)
    _register(app.test_client(), code, name='first1')
    assert User.query.filter_by(user_name='first1').count() == 1
    with patch('app.auth.util.render_template', return_value='rendered') as rt:
        _register(app.test_client(), code, name='second2')
    assert User.query.filter_by(user_name='second2').count() == 0
    assert 'Invalid referral code' in rt.call_args.kwargs['form'].question.errors


def test_other_gates_still_apply(app, people, no_captcha):
    """RC7: a valid code does not get past the honeypot."""
    code = _code(people['admin'])
    _register(app.test_client(), code, hp_field='bot@example.com')
    assert User.query.filter_by(user_name='newperson').first() is None


def test_the_ref_parameter_prefills_the_form_and_is_kept_in_the_session(app, people, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    code = _code(people['admin'])
    client = app.test_client()
    text = client.get(f'/auth/register?ref={code}').get_data(as_text=True)
    assert code in text
    assert 'Enter your referral code' in text
    with client.session_transaction() as sess:
        assert sess['referral_code'] == code


def test_the_form_shows_in_referral_mode(app, people, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    text = app.test_client().get('/auth/register').get_data(as_text=True)
    assert 'Registration is closed' not in text
    assert 'Create new account' in text


def test_a_referral_opens_a_staff_only_domain(app, people, no_captcha):
    """RC11: a valid code lets someone sign up where public registration is off; RC10: the account lands there."""
    shop = _shop('closed')
    admin = people['admin']
    admin.site_domain_id = shop.id
    code = _code(admin)
    client = app.test_client()
    data = {'user_name': 'shopper', 'hp_field': '', 'real_email': 'shopper@example.com',
            'password': 'a-good-password', 'password2': 'a-good-password', 'timezone': 'UTC', 'question': code,
            'submit': 'Register'}
    with patch('app.auth.util.send_email_verification'), patch('app.auth.util.sync_user_with_ldap'):
        client.post('/auth/register', data=data, base_url='https://shop.example')
    user = User.query.filter_by(user_name='shopper').one()
    assert user.site_domain_id == shop.id
    assert Referral.query.filter_by(referred_id=user.id).one().referrer_id == admin.id


def test_a_staff_only_domain_stays_closed_without_a_code(app, people, no_captcha):
    _shop('closed')
    client = app.test_client()
    data = {'user_name': 'shopper', 'hp_field': '', 'real_email': 'shopper@example.com',
            'password': 'a-good-password', 'password2': 'a-good-password', 'timezone': 'UTC', 'question': 'hi',
            'submit': 'Register'}
    with patch('app.auth.util.send_email_verification'), patch('app.auth.util.sync_user_with_ldap'), \
            patch('app.auth.util.render_template', return_value='rendered'):
        client.post('/auth/register', data=data, base_url='https://shop.example')
    assert User.query.filter_by(user_name='shopper').count() == 0


# --------------------------------------------------------------------------
# RC5: OAuth
# --------------------------------------------------------------------------

def test_oauth_signup_is_refused_without_a_session_code(app, people):
    from app.auth.oauth_util import refuse_new_oauth_account
    with app.test_request_context('/'):
        g.site = db.session.get(Site, 1)
        assert refuse_new_oauth_account() is not None


def test_oauth_signup_with_a_session_code_is_allowed_and_recorded(app, people):
    from flask import session
    from app.auth.oauth_util import initialize_new_user, refuse_new_oauth_account
    admin = people['admin']
    code = _code(admin)
    with app.test_request_context('/'):
        g.site = db.session.get(Site, 1)
        session['referral_code'] = code
        assert refuse_new_oauth_account() is None
        with patch('app.auth.oauth_util.finalize_user_setup'), patch('app.auth.oauth_util.login_user'):
            user = initialize_new_user('oauthy@example.com', 'oauthy', 'discord_oauth_id', {'id': 'd-1'},
                                       '127.0.0.1', '')
        assert 'referral_code' not in session
    assert Referral.query.filter_by(referred_id=user.id).one().referrer_id == admin.id


# --------------------------------------------------------------------------
# Admin UI
# --------------------------------------------------------------------------

@pytest.fixture
def admin_client(app, people):
    client = app.test_client()
    login(client, people['founder'])
    client.token = csrf(app, client)   # before any request: generate_csrf caches its token on g
    return client


def test_misc_offers_the_mode_and_the_new_settings(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the page renders form.csrf_token
    text = admin_client.get('/admin/misc').get_data(as_text=True)
    assert 'value="ReferralCode"' in text
    assert 'Who can generate referral codes?' in text
    assert 'admin_misc_referral.js' in text
    assert 'data-referral-prompt="Enter your referral code"' in text


def test_misc_saves_the_new_settings(app, admin_client):
    language = _language()
    payload = _valid_misc_payload(language)
    payload.update({'registration_mode': REFERRAL_MODE, 'referral_generators': 'staff',
                    'referral_max_per_code': '5', 'referral_max_per_user': '20'})
    payload.update({'submit': 'Save', 'csrf_token': admin_client.token})
    with patch('app.admin.routes.render_template', return_value='rendered') as rt:
        admin_client.post('/admin/misc', data=payload)
    assert rt.call_args.kwargs['form'].errors == {}
    assert db.session.get(Site, 1).registration_mode == REFERRAL_MODE
    assert get_setting('referral_generators') == 'staff'
    assert get_setting('referral_max_per_code') == 5
    assert get_setting('referral_max_per_user') == 20


def test_misc_leaves_the_referral_settings_alone_when_they_are_not_sent(app, admin_client):
    set_setting('referral_generators', 'all')
    set_setting('referral_max_per_code', 3)
    set_setting('referral_max_per_user', 9)
    payload = _valid_misc_payload(_language())
    for field in ('referral_generators', 'referral_max_per_code', 'referral_max_per_user'):
        payload.pop(field, None)
    payload.update({'submit': 'Save', 'csrf_token': admin_client.token, 'referral_max_per_code': ''})
    with patch('app.admin.routes.render_template', return_value='rendered') as rt:
        admin_client.post('/admin/misc', data=payload)
    assert rt.call_args.kwargs['form'].errors == {}
    assert (get_setting('referral_generators'), get_setting('referral_max_per_code'),
            get_setting('referral_max_per_user')) == ('all', 3, 9)


def test_misc_ignores_an_unknown_generator_value(app, admin_client):
    payload = _valid_misc_payload(_language())
    payload.update({'submit': 'Save', 'csrf_token': admin_client.token, 'referral_generators': 'everyone'})
    with patch('app.admin.routes.render_template', return_value='rendered'):
        admin_client.post('/admin/misc', data=payload)
    assert get_setting('referral_generators', None) is None


def test_the_admin_referral_list(app, admin_client, people):
    admin = people['admin']
    _code(admin)
    _referred(admin, 'listed1')
    text = admin_client.get('/admin/referrals').get_data(as_text=True)
    assert 'listed1' in text and 'adminuser' in text
    filtered = admin_client.get('/admin/referrals?by=nobody').get_data(as_text=True)
    assert 'listed1' not in filtered


def test_the_admin_referral_list_is_refused_to_ordinary_users(app, people):
    client = app.test_client()
    login(client, people['plain'])
    response = client.get('/admin/referrals')
    assert response.status_code in (302, 403)
    assert 'listed1' not in response.get_data(as_text=True)


# --------------------------------------------------------------------------
# User settings
# --------------------------------------------------------------------------

def test_the_settings_page_is_refused_to_someone_not_allowed(app, people):
    client = app.test_client()
    login(client, people['plain'])
    assert client.get('/user/settings/referrals').status_code == 404


def test_the_settings_page_is_refused_outside_referral_mode(app, people):
    _mode('Open')
    client = app.test_client()
    login(client, people['admin'])
    assert client.get('/user/settings/referrals').status_code == 404


def test_generate_regenerate_and_qr(app, people):
    admin = people['admin']
    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)
    assert 'Generate referral code' in client.get('/user/settings/referrals').get_data(as_text=True)
    client.post('/user/settings/referrals/generate', data={'csrf_token': token})
    first = db.session.get(User, admin.id).referral_code
    assert first
    text = client.get('/user/settings/referrals').get_data(as_text=True)
    assert first in text and '<svg' in text and f'/auth/register?ref={first}' in text
    client.post('/user/settings/referrals/generate', data={'csrf_token': token})
    db.session.expire_all()
    assert db.session.get(User, admin.id).referral_code != first
    png = client.get('/user/settings/referrals/qr.png')
    assert png.status_code == 200 and png.mimetype == 'image/png'


def test_generate_needs_csrf(app, people):
    client = app.test_client()
    login(client, people['admin'])
    client.post('/user/settings/referrals/generate', data={})
    assert db.session.get(User, people['admin'].id).referral_code is None


# --------------------------------------------------------------------------
# Outside referral mode nothing changes
# --------------------------------------------------------------------------

def test_an_open_registration_records_no_referral(app, people, no_captcha):
    _mode('Open')
    _register(app.test_client(), '')
    user = User.query.filter_by(user_name='newperson').one()
    assert Referral.query.filter_by(referred_id=user.id).count() == 0


def test_the_open_form_keeps_its_own_question_label(app, people):
    _mode('Open')
    with patch('app.auth.util.render_template', return_value='rendered') as rt:
        app.test_client().get('/auth/register')
    assert rt.call_args.kwargs['registration_open'] is True
    assert rt.call_args.kwargs['form'].question is None   # an open site asks nothing


def test_a_staff_only_domain_is_closed_outside_referral_mode(app, people):
    _mode('Open')
    _shop('closed')
    with patch('app.auth.util.render_template', return_value='rendered') as rt:
        app.test_client().get('/auth/register', base_url='https://shop.example')
    assert rt.call_args.kwargs['registration_open'] is False


def test_a_staff_only_domain_advertises_closed(people):
    shop = _shop('closed')
    site = db.session.get(Site, 1)
    assert registration_mode_on(domain_by_id(shop.id), site) == 'Closed'
    assert open_registrations(domain_by_id(shop.id), site) is False


def test_oauth_signup_outside_referral_mode_is_unchanged(app, people):
    from app.auth.oauth_util import initialize_new_user, refuse_new_oauth_account
    _mode('Open')
    with app.test_request_context('/'):
        g.site = db.session.get(Site, 1)
        assert refuse_new_oauth_account() is None
        with patch('app.auth.oauth_util.finalize_user_setup'), patch('app.auth.oauth_util.login_user'):
            user = initialize_new_user('plainoauth@example.com', 'plainoauth', 'discord_oauth_id', {'id': 'd-2'},
                                       '127.0.0.1', '')
    assert Referral.query.filter_by(referred_id=user.id).count() == 0


def test_oauth_account_creation_without_a_usable_code_is_refused(app, people):
    """initialize_new_user asks again under the row lock; a code that stopped working in between makes no account."""
    from app.auth.oauth_util import initialize_new_user
    with app.test_request_context('/'):
        g.site = db.session.get(Site, 1)
        with pytest.raises(ValueError):
            initialize_new_user('late@example.com', 'late', 'discord_oauth_id', {'id': 'd-3'}, '127.0.0.1', '')
    assert User.query.filter_by(email='late@example.com').count() == 0


def test_the_qr_png_needs_a_code(app, people):
    client = app.test_client()
    login(client, people['admin'])
    assert client.get('/user/settings/referrals/qr.png').status_code == 404


# --------------------------------------------------------------------------
# Every branch of the two new templates
# --------------------------------------------------------------------------

def test_the_settings_page_shows_counts_against_the_caps_for_a_capped_user(app, people):
    set_setting('referral_generators', 'all')
    set_setting('referral_max_per_code', 5)
    set_setting('referral_max_per_user', 7)
    plain = people['plain']
    _code(plain)
    _referred(plain, 'counted1')
    client = app.test_client()
    login(client, plain)
    text = ' '.join(client.get('/user/settings/referrals').get_data(as_text=True).split())
    assert 'Signups with this code: 1 / 5' in text
    assert 'All your referrals: 1 / 7' in text
    assert 'Signups left with this code: 4' in text and 'Signups left in total: 6' in text
    assert 'Regenerate referral code' in text and 'Referral code' in text


def test_the_settings_page_shows_uncapped_counts(app, people):
    set_setting('referral_generators', 'all')
    plain = people['plain']
    _code(plain)
    client = app.test_client()
    login(client, plain)
    text = ' '.join(client.get('/user/settings/referrals').get_data(as_text=True).split())
    assert 'Signups with this code: 0.' in text and 'All your referrals: 0.' in text
    assert 'Signups left with this code: unlimited' in text and 'Signups left in total: unlimited' in text


def test_the_settings_page_shows_the_admin_exemption(app, people):
    """One logged-in user per test: requests share the session's app context, so flask_login's g._login_user
    from one client is still current for the next."""
    _code(people['admin'])
    client = app.test_client()
    login(client, people['admin'])
    assert 'Admins have no limit' in client.get('/user/settings/referrals').get_data(as_text=True)


def test_the_settings_nav_links_the_page_for_someone_allowed(app, people):
    client = app.test_client()
    login(client, people['admin'])
    assert '/user/settings/referrals' in client.get('/user/passkeys').get_data(as_text=True)


def test_the_settings_nav_hides_the_page_from_someone_not_allowed(app, people):
    client = app.test_client()
    login(client, people['plain'])
    assert '/user/settings/referrals' not in client.get('/user/passkeys').get_data(as_text=True)


def test_the_admin_list_pages_and_names_a_deleted_referrer(app, admin_client, people):
    admin = people['admin']
    _code(admin)
    local = Instance.query.filter_by(domain=SERVER).one()
    for n in range(101):
        user = make_user(local, f'bulk{n}', local=True)
        record_referral(admin, user)
    orphan = make_user(local, 'orphan', local=True)
    db.session.add(Referral(referrer_id=None, referred=orphan, code=admin.referral_code))
    db.session.commit()
    first = admin_client.get('/admin/referrals').get_data(as_text=True)
    assert 'Next page' in first and 'Previous page' not in first
    assert '(deleted)' in first
    second = admin_client.get('/admin/referrals?page=2').get_data(as_text=True)
    assert 'Previous page' in second and 'Next page' not in second
    filtered = admin_client.get('/admin/referrals?by=adminuser').get_data(as_text=True)
    assert '>All<' in filtered and '(deleted)' not in filtered
    assert 'by=adminuser' in first   # each referrer links to their own rows


def test_the_admin_list_says_when_there_are_none(app, admin_client):
    assert 'No referrals yet.' in admin_client.get('/admin/referrals').get_data(as_text=True)


def test_the_admin_navs_link_the_list_only_in_referral_mode(app, admin_client):
    assert '/admin/referrals' in admin_client.get('/admin/referrals').get_data(as_text=True)
    _mode('Open')
    from app import cache
    from app.utils import get_site_as_dict
    cache.delete_memoized(get_site_as_dict)
    assert '/admin/referrals' not in admin_client.get('/admin/users').get_data(as_text=True)


def test_a_google_callback_without_a_code_makes_no_account(app, people):
    """RC5 through the provider callback, not only the helper."""
    client = app.test_client()
    with patch('app.auth.oauth_util.get_token_and_user_info',
               return_value=('a-token', {'id': 'g-1', 'email': 'fromgoogle@example.com', 'username': 'Goo'})):
        response = client.get('/auth/google_authorize')
    assert response.headers['Location'] == '/auth/login'
    assert User.query.filter_by(email='fromgoogle@example.com').count() == 0


def test_a_google_callback_with_a_code_makes_a_referred_account(app, people):
    code = _code(people['admin'])
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['referral_code'] = code
    with patch('app.auth.oauth_util.get_token_and_user_info',
               return_value=('a-token', {'id': 'g-2', 'email': 'withcode@example.com', 'username': 'Coded'})), \
            patch('app.auth.oauth_util.finalize_user_setup'):
        client.get('/auth/google_authorize')
    user = User.query.filter_by(email='withcode@example.com').one()
    assert Referral.query.filter_by(referred_id=user.id).one().referrer_id == people['admin'].id


def test_the_mastodon_email_step_without_a_code_makes_no_account(app, people):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['user_info'] = {'id': 'm-1', 'username': 'tooter'}
    response = client.post('/auth/mastodon_authorize', data={'email': 'tooter@example.com', 'submit': 'Set email'})
    assert response.headers['Location'] == '/auth/login'
    assert User.query.filter_by(email='tooter@example.com').count() == 0


# --------------------------------------------------------------------------
# RC5: the API's register endpoint (a stub today, D1181) must never become a way round referral mode
# --------------------------------------------------------------------------

@pytest.mark.parametrize('with_code', [False, True])
def test_api_register_never_creates_an_account_in_referral_mode(app, people, monkeypatch, with_code):
    """POST /api/alpha/user/register refuses everything while it is unimplemented. Whoever implements it must check
    `answer` with app.referral.referrer_for and call record_referral on success, as the web form does: without a code
    no account may be made, and an account made with one must carry its Referral row."""
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    code = _code(people['admin']) if with_code else ''
    before = User.query.count()
    response = app.test_client().post('/api/alpha/user/register', json={
        'username': 'apiperson', 'password': 'a-good-password', 'password_verify': 'a-good-password',
        'email': 'apiperson@example.com', 'answer': code})
    created = User.query.filter_by(user_name='apiperson').first()
    assert response.status_code != 404   # the route is really there: a 404 would pass every row below vacuously
    if not with_code:
        assert created is None and User.query.count() == before
        assert response.status_code >= 400
    elif created is not None:   # an implemented endpoint: a valid code may create the account, but only as a referral
        assert Referral.query.filter_by(referred_id=created.id).one().referrer_id == people['admin'].id
    else:
        assert response.status_code >= 400
