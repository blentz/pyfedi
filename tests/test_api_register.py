"""POST /api/alpha/user/register and GET /api/alpha/user/get_captcha: app-based registration.

The web form's rules over a JSON body (app.auth.util.register_from_api). Rulings 2026-10-31: the captcha is Lemmy's
get_captcha; registering is agreeing to the terms; the onion refuses; a jwt comes back when the account is usable now.
"""
from unittest.mock import patch

import jwt as pyjwt
import pytest

from app import db
from app.utils import captcha_store
from app.models import Instance, Referral, Role, Site, SiteDomain, User, UserRegistration, role_grant
from app.referral import REFERRAL_MODE, generate_code
from app.storefront import clear_domain_cache
from app.utils import set_setting
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

SERVER = 'test.piefed.local'


def _site(mode='Open', question='', tos=None):
    site = db.session.get(Site, 1)
    site.registration_mode = mode
    site.application_question = question
    site.private_instance = False
    site.tos_url = tos
    db.session.commit()


@pytest.fixture
def api(app, db_session, monkeypatch):
    """Open, no captcha, no email verification; the founder exists so the new account is never user 1."""
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    _site()
    set_setting('captcha_enabled', False)
    set_setting('email_verification', False)
    local = Instance.query.filter_by(domain=SERVER).first() or make_instance(SERVER, software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    with patch('app.auth.util.finalize_user_setup') as finalized, patch('app.auth.util.sync_user_with_ldap') as ldap, \
            patch('app.auth.util.send_email_verification') as emailed, \
            patch('app.auth.util.handle_abandoned_open_instance'):
        yield {'client': app.test_client(), 'finalized': finalized, 'ldap': ldap, 'emailed': emailed,
               'founder': founder, 'local': local}


def _body(**overrides):
    body = {'username': 'appuser', 'password': 'a-good-password', 'password_verify': 'a-good-password',
            'email': 'appuser@example.com'}
    body.update(overrides)
    return body


def _post(api, **overrides):
    return api['client'].post('/api/alpha/user/register', json=_body(**overrides))


def _refused(response, message):
    assert response.status_code == 400, response.get_json()
    assert response.get_json()['message'] == message
    assert User.query.filter_by(user_name='appuser').first() is None


# --------------------------------------------------------------------------
# A usable account, and what the response says
# --------------------------------------------------------------------------

def test_an_open_signup_returns_a_jwt_for_the_new_account(app, api):
    response = _post(api)
    assert response.status_code == 200
    body = response.get_json()
    user = User.query.filter_by(user_name='appuser').one()
    claims = pyjwt.decode(body['jwt'], app.config['SECRET_KEY'], algorithms=['HS256'])
    assert str(claims['sub']) == str(user.id)
    assert body['registration_created'] is False and body['verify_email_sent'] is False
    assert user.email == 'appuser@example.com' and user.verified
    api['finalized'].assert_called_once_with(user)
    api['ldap'].assert_called_once_with(user, 'a-good-password')


def test_a_tos_url_does_not_block_an_app_signup(app, api):
    """Registering is agreeing (ruling): the request has no terms field."""
    _site(tos='https://example.com/terms')
    assert _post(api).status_code == 200


def test_email_verification_holds_back_the_jwt(app, api):
    set_setting('email_verification', True)
    body = _post(api).get_json()
    user = User.query.filter_by(user_name='appuser').one()
    assert body['jwt'] is None and body['verify_email_sent'] is True and body['registration_created'] is False
    api['emailed'].assert_called_once_with(user)
    api['finalized'].assert_not_called()


def test_an_application_is_created_and_holds_back_the_jwt(app, api):
    _site('RequireApplication', 'Why join?')
    set_setting('ban_check_servers', 'other.example')
    with patch('app.auth.util.task_selector') as task:
        body = _post(api, answer='I like it here').get_json()
    user = User.query.filter_by(user_name='appuser').one()
    application = UserRegistration.query.filter_by(user_id=user.id).one()
    assert application.answer == 'I like it here'
    assert body == {'jwt': None, 'registration_created': True, 'verify_email_sent': False}
    task.assert_called_once_with('check_application', application_id=application.id)
    api['finalized'].assert_not_called()


def test_an_application_without_ban_check_servers_queues_no_task(app, api):
    _site('RequireApplication', 'Why join?')
    with patch('app.auth.util.task_selector') as task:
        assert _post(api, answer='hello').get_json()['registration_created'] is True
    task.assert_not_called()


def test_an_application_needs_an_answer(app, api):
    _site('RequireApplication', 'Why join?')
    _refused(_post(api), 'question: This field is required.')


def test_a_banned_address_gets_an_account_but_no_jwt(app, api):
    """The web form's behaviour: the row is written banned. A banned account is never handed a token."""
    with patch('app.auth.util.user_ip_banned', return_value=True):
        body = _post(api).get_json()
    assert body['jwt'] is None
    assert User.query.filter_by(user_name='appuser').one().banned is True


# --------------------------------------------------------------------------
# Closed doors
# --------------------------------------------------------------------------

@pytest.mark.parametrize('mode, question', [('Closed', ''), ('RequireApplication', '')])
def test_a_closed_site_refuses(app, api, mode, question):
    _site(mode, question)
    _refused(_post(api, answer='hi'), 'registration_closed')


def test_the_onion_refuses(app, api):
    with patch('app.auth.util.is_onion_request', return_value=True):
        _refused(_post(api), 'registration_closed')


def _shop(registration):
    row = SiteDomain(domain='shop.example', is_default=False, registration=registration)
    db.session.add(row)
    db.session.commit()
    clear_domain_cache()
    return row


def test_a_staff_only_domain_refuses_outside_referral_mode(app, api):
    _shop('closed')
    response = api['client'].post('/api/alpha/user/register', json=_body(), base_url='https://shop.example')
    _refused(response, 'registration_closed')


def test_the_alpha_api_switch_still_applies(app, api, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'false')
    monkeypatch.setattr(app, 'debug', False)
    response = _post(api)
    assert response.status_code == 400
    assert User.query.filter_by(user_name='appuser').first() is None


# --------------------------------------------------------------------------
# The web form's validation, field by field
# --------------------------------------------------------------------------

@pytest.mark.parametrize('overrides, message', [
    ({'password_verify': 'something-else'}, 'password2: Field must be equal to password.'),
    ({'email': ''}, 'real_email: This field is required.'),
    ({'email': 'not-an-address'}, 'real_email: Invalid email address.'),
    ({'username': 'a b'}, 'user_name: User names cannot contain spaces.'),
    ({'password': 'short', 'password_verify': 'short'}, 'password: Field must be between 8 and 129 characters long.'),
])
def test_the_forms_own_validators_answer(app, api, overrides, message):
    _refused(_post(api, **overrides), message)


def test_a_taken_name_or_address_is_refused(app, api):
    taken = make_user(api['local'], 'appuser', local=True)
    taken.email = 'someone@example.com'
    db.session.commit()
    response = _post(api)
    assert response.get_json()['message'] == 'user_name: An account with this user name already exists.'
    response = _post(api, username='othername', email='someone@example.com')
    assert response.get_json()['message'] == 'real_email: An account with this email address already exists.'


@pytest.mark.parametrize('overrides, message', [
    ({'honeypot': 'bot'}, 'Sorry, we could not register you.'),
    ({'email': 'abuse@example.com'}, 'Sorry, you cannot use that email address'),
    ({'username': 'admin'}, 'Sorry, you cannot use that user name'),
])
def test_the_web_forms_refusals_answer(app, api, overrides, message):
    response = _post(api, **overrides)
    assert response.status_code == 400 and response.get_json()['message'] == message
    assert User.query.count() == 1


def test_blocked_name_patterns_are_refused(app, api):
    with patch('app.auth.util.actor_contains_blocked_words', return_value=True):
        _refused(_post(api), 'Sorry, this username pattern is not allowed.')


def test_a_declined_country_is_refused(app, api):
    with patch('app.auth.util.get_country', return_value='XX'), \
            patch('app.auth.util.is_country_blocked', return_value=True):
        _refused(_post(api), 'registration_declined')


def test_the_name_is_normalised_as_the_web_form_does(app, api):
    with patch('app.auth.util.normalize_utf', side_effect=lambda name: name) as normalised:
        assert _post(api).status_code == 200
    normalised.assert_called_once_with('appuser')


# --------------------------------------------------------------------------
# Captcha (ruling: Lemmy's get_captcha)
# --------------------------------------------------------------------------

def test_get_captcha_is_null_when_none_is_asked(app, api):
    response = api['client'].get('/api/alpha/user/get_captcha')
    assert response.status_code == 200 and response.get_json() == {'ok': None}


def test_get_captcha_refuses_on_the_onion(app, api):
    set_setting('captcha_enabled', True)
    with patch('app.api.alpha.utils.user.is_onion_request', return_value=True):
        response = api['client'].get('/api/alpha/user/get_captcha')
    assert response.status_code == 400 and response.get_json()['message'] == 'registration_closed'


def test_get_captcha_returns_lemmys_shape(app, api):
    set_setting('captcha_enabled', True)
    body = api['client'].get('/api/alpha/user/get_captcha').get_json()['ok']
    assert set(body) == {'png', 'wav', 'uuid'}
    assert len(body['uuid']) == 24
    assert not body['png'].startswith('data:') and '\n' not in body['png'] and '\n' not in body['wav']


def _solved_captcha(api):
    uuid = api['client'].get('/api/alpha/user/get_captcha').get_json()['ok']['uuid']
    code = captcha_store().get('captcha_' + uuid)
    return uuid, code.decode() if isinstance(code, bytes) else code


def test_a_captcha_is_required_when_enabled(app, api):
    set_setting('captcha_enabled', True)
    _refused(_post(api), 'captcha_incorrect')
    uuid, _code = _solved_captcha(api)
    _refused(_post(api, captcha_uuid=uuid, captcha_answer='0000'), 'captcha_incorrect')


def test_a_solved_captcha_registers_once(app, api):
    set_setting('captcha_enabled', True)
    uuid, code = _solved_captcha(api)
    assert _post(api, captcha_uuid=uuid, captcha_answer=code).status_code == 200
    second = _post(api, username='another', email='another@example.com', captcha_uuid=uuid, captcha_answer=code)
    assert second.get_json()['message'] == 'captcha_incorrect'


# --------------------------------------------------------------------------
# Referral-code mode
# --------------------------------------------------------------------------

@pytest.fixture
def referrer(api):
    _site(REFERRAL_MODE, 'Enter your referral code')
    admin = make_user(api['local'], 'adminuser', local=True)
    role = Role.query.filter_by(name='Admin').first() or Role(name='Admin', built_in=True)
    db.session.add(role)
    db.session.commit()
    db.session.execute(role_grant.insert().values(user_id=admin.id, role_id=role.id))
    generate_code(admin)
    db.session.commit()
    return admin


def test_referral_mode_needs_a_code(app, api, referrer):
    _refused(_post(api), 'question: This field is required.')
    _refused(_post(api, answer='0f6c3a52-8d1e-4b7a-9c2f-5e4d3b2a1c0e'), 'invalid_referral_code')


def test_a_valid_code_registers_a_referred_usable_account(app, api, referrer):
    body = _post(api, answer=referrer.referral_code).get_json()
    user = User.query.filter_by(user_name='appuser').one()
    assert body['jwt'] and body['registration_created'] is False
    assert Referral.query.filter_by(referred_id=user.id).one().referrer_id == referrer.id
    assert UserRegistration.query.filter_by(user_id=user.id).count() == 0


def test_a_code_opens_its_referrers_staff_only_domain(app, api, referrer):
    shop = _shop('closed')
    referrer.site_domain_id = shop.id
    generate_code(referrer)
    db.session.commit()
    response = api['client'].post('/api/alpha/user/register', json=_body(answer=referrer.referral_code),
                                  base_url='https://shop.example')
    assert response.status_code == 200
    assert User.query.filter_by(user_name='appuser').one().site_domain_id == shop.id
