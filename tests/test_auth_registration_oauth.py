"""Registration, and the three OAuth providers.

Sub-project 83, slice B -- the rest of `app/auth/routes.py`. Three defects,
all measured:

* the registration **honeypot was a bypass**: filling the hidden `email`
  field made `is_invalid_email_or_username` return "not invalid" before
  either of the checks below it ran, so a caller who filled it could take a
  reserved user name or register a role address (D1133);
* `mastodon_connect` and `mastodon_connect_callback` carried no
  `@login_required`, while the Google and Discord equivalents all do -- an
  anonymous caller was redirected to the remote instance for a flow that can
  only fail (D1135);
* all three connect callbacks flashed **`str(e)`** from the OAuth client
  straight to the visitor (D1136).

A filled honeypot is itself refused (D1134, fixed by owner ruling).
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from app import db
from app.auth.forms import RegistrationForm
from app.models import Instance, Site, User, UserRegistration
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.registration_mode = 'Open'
    site.tos_url = None
    site.application_question = ''
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    person = make_user(local, 'person', local=True, with_keys=True)
    person.email = 'person@example.com'
    person.verified = True
    person.set_password('a-good-password')
    db.session.commit()
    return app.test_client(), person


@pytest.fixture
def no_captcha():
    """`RegistrationForm` adds a `CaptchaField` with `DataRequired` unless
    `captcha_enabled` is off, so every registration row has to turn it off or
    the form refuses the submission before any route code runs -- which is
    what made the first attempt at D1133's probe measure nothing (fact 529).
    """
    with patch('app.auth.forms.get_setting', return_value=False):
        yield


def register(client, **overrides):
    data = {'user_name': 'newperson', 'hp_field': '',
            'real_email': 'new@example.com', 'password': 'a-good-password',
            'password2': 'a-good-password', 'timezone': 'UTC',
            'submit': 'Register'}
    data.update(overrides)
    return client.post('/auth/register', data=data)


# --------------------------------------------------------------------------
# D1133 -- the honeypot
# --------------------------------------------------------------------------


def test_registering(app, env, no_captcha):
    anon, person = env

    with patch('app.auth.util.send_email_verification'):
        with patch('app.auth.util.sync_user_with_ldap'):
            response = register(anon)

    assert response.status_code == 302
    assert User.query.filter_by(user_name='newperson').first() is not None


@pytest.mark.parametrize('honeypot, refusal', [
    ('', 'cannot use that user name'),
    ('i-am-a-bot@example.com', 'could not register you'),
])
def test_a_reserved_user_name_is_refused_either_way(app, env, no_captcha,
                                                    honeypot, refusal):
    """D1133. `is_invalid_email_or_username` used to open with `if
    form.email.data.strip(): return False` -- "not invalid" -- so filling the
    HONEYPOT skipped both checks below it. The field meant to catch bots was
    a bypass around the two gates. Measured: `PROBE at3 honeypot + reserved
    name "admin": users created=1 | admin exists=True`, against `PROBE at5
    reserved name, NO honeypot: users created=0`. Since D1134 a filled
    honeypot is refused before the name is looked at."""
    anon, person = env

    with patch('app.auth.util.send_email_verification'):
        with patch('app.auth.util.sync_user_with_ldap'):
            with patch('app.auth.util.flash') as flashed:
                register(anon, user_name='admin',
                         real_email='admin@example.com', hp_field=honeypot)

    assert User.query.filter_by(user_name='admin').first() is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert refusal in messages


@pytest.mark.parametrize('honeypot, refusal', [
    ('', 'cannot use that email address'),
    ('i-am-a-bot@example.com', 'could not register you'),
])
@pytest.mark.parametrize('address', ['postmaster@example.com',
                                     'abuse@example.com', 'noc@example.com'])
def test_a_role_address_is_refused_either_way(app, env, no_captcha, honeypot,
                                              refusal, address):
    """D1133's other half. A role address reaches whoever reads that mailbox,
    not one person, so it cannot own an account -- and the honeypot let it
    past. Measured: `PROBE at4 honeypot + role address: users created=1`."""
    anon, person = env

    with patch('app.auth.util.send_email_verification'):
        with patch('app.auth.util.sync_user_with_ldap'):
            with patch('app.auth.util.flash') as flashed:
                register(anon, user_name='roleaccount', real_email=address,
                         hp_field=honeypot)

    assert User.query.filter_by(user_name='roleaccount').first() is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert refusal in messages


def test_a_filled_honeypot_is_refused(app, env, no_captcha):
    """D1134, fixed (owner ruling): a registration that fills the honeypot is
    refused. The field was named `email`, exactly what an autofiller looks
    for, so it is now `hp_field`, autocomplete="off", out of the tab order and
    hidden from people and screen readers."""
    anon, person = env

    with patch('app.auth.util.send_email_verification'):
        with patch('app.auth.util.sync_user_with_ldap'):
            register(anon, user_name='botaccount',
                     real_email='bot@example.com',
                     hp_field='i-am-a-bot@example.com')

    assert User.query.filter_by(user_name='botaccount').first() is None


def test_the_honeypot_is_marked_up_so_autofill_and_people_skip_it(app, no_captcha):
    """D1134: a non-semantic name, autocomplete off, tabindex -1, aria-hidden,
    and positioned off-screen by CSS rather than an input type="hidden"."""
    with app.test_request_context('/auth/register'):
        field = str(RegistrationForm(meta={'csrf': False}).hp_field())
    template = (Path(app.root_path) / 'templates/auth/register.html').read_text()

    assert 'name="hp_field"' in field and 'type="text"' in field
    assert 'autocomplete="off"' in field
    assert 'tabindex="-1"' in field
    assert 'aria-hidden="true"' in field
    wrapper = template[:template.index('form.hp_field()')].rsplit('<div', 1)[1]
    assert 'left: -10000px' in wrapper and 'aria-hidden="true"' in wrapper
    assert 'form.email' not in template


def test_a_user_name_containing_blocked_words_is_refused(app, env,
                                                         no_captcha):
    anon, person = env

    with patch('app.auth.util.actor_contains_blocked_words',
               return_value=True):
        with patch('app.auth.util.flash') as flashed:
            register(anon, user_name='badword')

    assert User.query.filter_by(user_name='badword').first() is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'username pattern is not allowed' in messages


def test_a_blocked_referrer_is_sent_to_the_waiting_room(app, env,
                                                        no_captcha):
    """Registrations arriving from a known spam source are parked rather than
    refused outright, and the visitor is cookied so the next attempt is
    recognised."""
    anon, person = env

    with patch('app.auth.util.blocked_referrers', return_value=['spam.example']):
        with anon.session_transaction() as session:
            session['Referer'] = 'https://spam.example/x'
        response = register(anon)

    assert response.headers['Location'] == '/auth/please_wait'
    assert 'sesion' in response.headers.get('Set-Cookie', '')
    assert User.query.filter_by(user_name='newperson').first() is None


def test_a_blocked_country_is_declined(app, env, no_captcha):
    anon, person = env

    with patch('app.auth.util.get_country', return_value='XX'):
        with patch('app.auth.util.get_setting', return_value='XX'):
            response = register(anon)

    assert response.status_code == 200
    assert b'not accepting registrations from your country' in response.data
    assert User.query.filter_by(user_name='newperson').first() is None


def test_the_registration_form_renders(app, env, no_captcha):
    anon, person = env

    with patch('app.auth.routes.render_registration_form',
               return_value='rendered') as render:
        response = anon.get('/auth/register')

    assert response.status_code == 200
    assert render.call_args is not None


def test_the_terms_box_is_dropped_when_there_are_no_terms(app, env,
                                                          no_captcha):
    """Fact 432: `del form.terms` pops it from `_fields` and leaves the
    attribute at None."""
    anon, person = env

    with patch('app.auth.routes.render_registration_form',
               return_value='rendered') as render:
        anon.get('/auth/register')

    form = render.call_args.args[0]
    assert form.terms is None


def test_the_terms_box_stays_when_there_are_terms(app, env, no_captcha):
    anon, person = env
    site = db.session.get(Site, 1)
    site.tos_url = 'https://test.piefed.local/terms'
    db.session.commit()

    with patch('app.auth.routes.render_registration_form',
               return_value='rendered') as render:
        anon.get('/auth/register')

    assert render.call_args.args[0].terms is not None


def test_a_tos_url_of_only_whitespace_is_no_terms_at_all(app, env,
                                                          no_captcha):
    """`tos_url is None or not tos_url.strip()` -- the second arm is for a
    setting that was filled in and then cleared to spaces, which is not a
    link to anything."""
    anon, person = env
    site = db.session.get(Site, 1)
    site.tos_url = '   '
    db.session.commit()

    with patch('app.auth.routes.render_registration_form',
               return_value='rendered') as render:
        anon.get('/auth/register')

    assert render.call_args.args[0].terms is None


def test_the_application_question_is_dropped_on_an_open_instance(app, env,
                                                                 no_captcha):
    anon, person = env

    with patch('app.auth.routes.render_registration_form',
               return_value='rendered') as render:
        anon.get('/auth/register')

    assert render.call_args.args[0].question is None


def test_the_application_question_stays_when_applications_are_required(
        app, env, no_captcha):
    anon, person = env
    site = db.session.get(Site, 1)
    site.registration_mode = 'RequireApplication'
    db.session.commit()

    with patch('app.auth.routes.render_registration_form',
               return_value='rendered') as render:
        anon.get('/auth/register')

    assert render.call_args.args[0].question is not None


def test_an_open_instance_nobody_administers_closes_itself(app, env,
                                                           no_captcha):
    """D1137. An instance left open with no admin logged in recently is a
    spam target, so the door shuts on its own -- but the shutting was written
    to `g.site`, which `before_request` builds as `Site(**get_site_as_dict())`,
    a transient object never added to the session. The mode reverted on the
    next request, so the door never actually shut."""
    anon, person = env

    with patch('app.auth.util.no_admins_logged_in_recently', return_value=True):
        with patch('app.auth.routes.render_registration_form',
                   return_value='rendered'):
            anon.get('/auth/register')

    db.session.expire_all()
    assert db.session.get(Site, 1).registration_mode == 'Closed'


def test_an_instance_with_active_admins_stays_open(app, env, no_captcha):
    anon, person = env

    with patch('app.auth.util.no_admins_logged_in_recently',
               return_value=False):
        with patch('app.auth.routes.render_registration_form',
                   return_value='rendered'):
            anon.get('/auth/register')

    db.session.expire_all()
    assert db.session.get(Site, 1).registration_mode == 'Open'


def test_somebody_already_logged_in_is_sent_away_from_registration(app, env):
    anon, person = env

    response = as_user(app, person).get('/auth/register')

    assert response.status_code == 302
    assert '/auth/register' not in response.headers['Location']


# --------------------------------------------------------------------------
# D1135 -- the routes that connect a provider to an account
# --------------------------------------------------------------------------


@pytest.mark.parametrize('route', [
    'google_connect', 'google_connect_callback',
    'mastodon_connect', 'mastodon_connect_callback',
    'discord_connect', 'discord_connect_callback',
])
def test_connecting_a_provider_needs_an_account(app, env, route):
    """D1135. `mastodon_connect` and its callback carried no
    `@login_required` while their four siblings all did, so an anonymous
    caller was redirected to the remote instance for a flow that can only
    fail -- a real authorization burned. Measured: `PROBE au1 anonymous
    /auth/mastodon_connect: 200`, against 302 to the login page for the
    others."""
    anon, person = env

    with patch('app.auth.routes.oauth'):
        response = anon.get(f'/auth/{route}')

    assert response.status_code == 302
    assert response.headers['Location'].startswith('/auth/login?next=')


@pytest.mark.parametrize('provider', ['google', 'mastodon', 'discord'])
def test_connecting_a_provider(app, env, provider):
    anon, person = env
    client = as_user(app, person)

    with patch('app.auth.routes.oauth') as fake:
        getattr(fake, provider).get.return_value.json.return_value = {
            'id': 'remote-id-1'}
        with patch('app.auth.routes.flash') as flashed:
            response = client.get(f'/auth/{provider}_connect_callback')

    assert response.headers['Location'] == '/user/connect_oauth'
    db.session.refresh(person)
    assert getattr(person, f'{provider}_oauth_id') == 'remote-id-1'
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'connected successfully' in messages


@pytest.mark.parametrize('provider', ['google', 'mastodon', 'discord'])
def test_a_provider_account_already_taken_is_refused(app, env, provider):
    """One remote account cannot be the key to two local ones, or whoever
    holds it chooses which to log into."""
    anon, person = env
    somebody_else = make_user(instance(), 'somebodyelse', local=True)
    setattr(somebody_else, f'{provider}_oauth_id', 'remote-id-1')
    db.session.commit()
    client = as_user(app, person)

    with patch('app.auth.routes.oauth') as fake:
        getattr(fake, provider).get.return_value.json.return_value = {
            'id': 'remote-id-1'}
        with patch('app.auth.routes.flash') as flashed:
            response = client.get(f'/auth/{provider}_connect_callback')

    assert response.headers['Location'] == '/user/connect_oauth'
    db.session.refresh(person)
    assert getattr(person, f'{provider}_oauth_id') is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'already connected to another user' in messages


@pytest.mark.parametrize('provider', ['google', 'mastodon', 'discord'])
def test_reconnecting_the_same_provider_account_is_fine(app, env, provider):
    """`existing_user.id != current_user.id` -- your own connection is not
    somebody else's."""
    anon, person = env
    setattr(person, f'{provider}_oauth_id', 'remote-id-1')
    db.session.commit()
    client = as_user(app, person)

    with patch('app.auth.routes.oauth') as fake:
        getattr(fake, provider).get.return_value.json.return_value = {
            'id': 'remote-id-1'}
        with patch('app.auth.routes.flash') as flashed:
            client.get(f'/auth/{provider}_connect_callback')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'connected successfully' in messages


# --------------------------------------------------------------------------
# D1136 -- what a failure tells the visitor
# --------------------------------------------------------------------------


@pytest.mark.parametrize('provider', ['Google', 'Mastodon', 'Discord'])
def test_a_failed_connection_says_nothing_about_why(app, env, provider):
    """D1136. This used to flash `str(e)` from the OAuth client, whose text
    carries whatever the library put there -- a token, a URL with a code in
    it, an internal address. Measured: a flash reading `Failed to connect
    Mastodon account: token secret=abc123 leaked`."""
    anon, person = env
    client = as_user(app, person)

    with patch('app.auth.routes.oauth') as fake:
        getattr(fake, provider.lower()).authorize_access_token.side_effect = \
            Exception('token secret=abc123 leaked')
        with patch('app.auth.routes.flash') as flashed:
            response = client.get(f'/auth/{provider.lower()}_connect_callback')

    assert response.headers['Location'] == '/user/connect_oauth'
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'secret=abc123' not in messages
    assert f'Failed to connect {provider} account' in messages


@pytest.mark.parametrize('provider', ['Google', 'Mastodon', 'Discord'])
def test_a_failed_connection_is_logged_for_the_operator(app, env, provider):
    """The detail is not lost -- it goes where the operator can see it and
    the visitor cannot."""
    anon, person = env
    client = as_user(app, person)

    with patch('app.auth.routes.oauth') as fake:
        getattr(fake, provider.lower()).authorize_access_token.side_effect = \
            Exception('token secret=abc123 leaked')
        # The logger itself, not `current_app`: patching the proxy hands back
        # an AsyncMock for `.logger.warning` and leaves `RuntimeWarning:
        # coroutine ... was never awaited` behind (fact 531).
        with patch.object(app.logger, 'warning') as logged:
            with patch('app.auth.routes.flash'):
                client.get(f'/auth/{provider.lower()}_connect_callback')

    messages = ' '.join(str(call) for call in logged.call_args_list)
    assert 'OAuth connect failed' in messages
    assert 'secret=abc123' in messages


# --------------------------------------------------------------------------
# Starting a provider login
# --------------------------------------------------------------------------


@pytest.mark.parametrize('provider', ['google', 'mastodon', 'discord'])
def test_starting_a_provider_login(app, env, provider):
    anon, person = env

    with patch('app.auth.routes.oauth') as fake:
        getattr(fake, provider).authorize_redirect.return_value = 'redirected'
        response = anon.get(f'/auth/{provider}_login')

    assert response.data == b'redirected'
    redirect_uri = getattr(fake, provider).authorize_redirect.call_args \
        .kwargs['redirect_uri']
    assert f'{provider}_authorize' in redirect_uri


@pytest.mark.parametrize('provider', ['google', 'mastodon', 'discord'])
def test_somebody_logged_in_is_sent_to_connect_instead(app, env, provider):
    """Logging in with a provider you are already logged in for means you
    wanted to CONNECT it, not to log in again."""
    anon, person = env

    with patch('app.auth.routes.oauth'):
        response = as_user(app, person).get(f'/auth/{provider}_login')

    assert response.status_code == 302
    assert f'{provider}_connect' in response.headers['Location']


@pytest.mark.parametrize('provider, endpoint, key', [
    ('google', 'oauth2/v2/userinfo', 'google_oauth_id'),
    ('discord', 'users/@me', 'discord_oauth_id'),
])
def test_the_authorize_callback_hands_off_to_the_shared_handler(
        app, env, provider, endpoint, key):
    anon, person = env

    with patch('app.auth.routes.handle_oauth_authorize',
               return_value='handled') as handler:
        response = anon.get(f'/auth/{provider}_authorize')

    assert response.data == b'handled'
    assert handler.call_args.kwargs == {
        'provider': provider, 'user_info_endpoint': endpoint,
        'oauth_id_key': key}


# --------------------------------------------------------------------------
# The Mastodon registration form
# --------------------------------------------------------------------------


def test_mastodon_authorize_without_a_session_hands_off(app, env):
    """With nothing in the session this is the first leg, and the shared
    handler does the work."""
    anon, person = env

    with patch('app.auth.routes.handle_oauth_authorize',
               return_value='handled') as handler:
        response = anon.get('/auth/mastodon_authorize')

    assert response.data == b'handled'
    assert handler.call_args.kwargs['provider'] == 'mastodon'


def test_mastodon_authorize_logs_in_an_account_that_already_exists(app, env):
    anon, person = env
    person.mastodon_oauth_id = 'remote-id-1'
    db.session.commit()
    with anon.session_transaction() as session:
        session['user_info'] = {'id': 'remote-id-1', 'username': 'person'}

    with patch('app.auth.routes.finalize_user_login',
               return_value='logged in') as finalize:
        response = anon.post('/auth/mastodon_authorize',
                             data={'email': 'person@example.com',
                                   'submit': 'Set email'})

    assert response.data == b'logged in'
    assert finalize.call_args.args[0].id == person.id


def test_mastodon_authorize_refuses_a_banned_account(app, env):
    anon, person = env
    person.mastodon_oauth_id = 'remote-id-1'
    person.banned = True
    db.session.commit()
    with anon.session_transaction() as session:
        session['user_info'] = {'id': 'remote-id-1', 'username': 'person'}

    with patch('app.auth.routes.finalize_user_login') as finalize:
        # `handle_banned_user` lives in app/auth/util.py and flashes there.
        with patch('app.auth.util.flash') as flashed:
            response = anon.post('/auth/mastodon_authorize',
                                 data={'email': 'person@example.com',
                                       'submit': 'Set email'})

    assert response.status_code == 302
    assert finalize.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'You have been banned' in messages


def test_mastodon_authorize_refuses_a_deleted_account(app, env):
    anon, person = env
    person.mastodon_oauth_id = 'remote-id-1'
    person.deleted = True
    db.session.commit()
    with anon.session_transaction() as session:
        session['user_info'] = {'id': 'remote-id-1', 'username': 'person'}

    with patch('app.auth.routes.finalize_user_login') as finalize:
        with patch('app.auth.routes.flash') as flashed:
            response = anon.post('/auth/mastodon_authorize',
                                 data={'email': 'person@example.com',
                                       'submit': 'Set email'})

    assert response.headers['Location'] == '/auth/login'
    assert finalize.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'account has been deleted' in messages


def test_mastodon_authorize_registers_a_new_account(app, env):
    anon, person = env
    with anon.session_transaction() as session:
        session['user_info'] = {'id': 'remote-id-2', 'username': 'newcomer'}

    with patch('app.auth.routes.initialize_new_user') as initialize:
        initialize.return_value.communities.return_value = []
        with patch('app.auth.routes.redirect_next_page',
                   return_value='next page'):
            response = anon.post('/auth/mastodon_authorize',
                                 data={'email': 'newcomer@example.com',
                                       'submit': 'Set email'})

    assert response.data == b'next page'
    assert initialize.call_args.args[0] == 'newcomer@example.com'
    assert initialize.call_args.args[1] == 'newcomer'
    assert initialize.call_args.args[2] == 'mastodon_oauth_id'


def test_a_new_mastodon_account_needing_approval_waits(app, env):
    anon, person = env
    site = db.session.get(Site, 1)
    site.registration_mode = 'RequireApplication'
    site.application_question = 'why?'
    db.session.commit()
    with anon.session_transaction() as session:
        session['user_info'] = {'id': 'remote-id-2', 'username': 'newcomer'}

    with patch('app.auth.routes.initialize_new_user') as initialize:
        initialize.return_value.registration_application.id = 7
        with patch('app.auth.routes.task_selector') as task:
            response = anon.post('/auth/mastodon_authorize',
                                 data={'email': 'newcomer@example.com',
                                       'submit': 'Set email'})

    assert response.headers['Location'] == '/auth/please_wait'
    assert task.call_args.args[0] == 'check_application'
    assert task.call_args.kwargs['application_id'] == 7


def test_applications_with_no_question_do_not_go_to_the_queue(app, env):
    """`RequireApplication` AND a question: with the mode set but no question
    to answer there is nothing for a moderator to read, so the registration
    completes instead of waiting on a review that cannot happen."""
    anon, person = env
    site = db.session.get(Site, 1)
    site.registration_mode = 'RequireApplication'
    site.application_question = ''
    db.session.commit()
    with anon.session_transaction() as session:
        session['user_info'] = {'id': 'remote-id-2', 'username': 'newcomer'}

    with patch('app.auth.routes.initialize_new_user') as initialize:
        initialize.return_value.communities.return_value = []
        with patch('app.auth.routes.task_selector') as task:
            with patch('app.auth.routes.redirect_next_page',
                       return_value='next page'):
                response = anon.post('/auth/mastodon_authorize',
                                     data={'email': 'newcomer@example.com',
                                           'submit': 'Set email'})

    assert response.data == b'next page'
    assert task.call_args is None


def test_the_session_is_cleared_once_the_form_is_submitted(app, env):
    """`user_info` is the half-finished OAuth handshake; leaving it behind
    would let the next form submission reuse it."""
    anon, person = env
    with anon.session_transaction() as session:
        session['user_info'] = {'id': 'remote-id-2', 'username': 'newcomer'}

    with patch('app.auth.routes.initialize_new_user') as initialize:
        initialize.return_value.communities.return_value = []
        with patch('app.auth.routes.redirect_next_page', return_value='x'):
            anon.post('/auth/mastodon_authorize',
                      data={'email': 'newcomer@example.com',
                            'submit': 'Set email'})

    with anon.session_transaction() as session:
        assert 'user_info' not in session


def test_a_get_with_a_session_still_hands_off(app, env):
    """The form is only processed on POST; a GET with the session set is the
    person arriving at the form, not submitting it."""
    anon, person = env
    with anon.session_transaction() as session:
        session['user_info'] = {'id': 'remote-id-2', 'username': 'newcomer'}

    with patch('app.auth.routes.handle_oauth_authorize',
               return_value='handled') as handler:
        response = anon.get('/auth/mastodon_authorize')

    assert response.data == b'handled'
    assert handler.call_args is not None
