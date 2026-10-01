"""Login, registration, and the two links an email can carry.

Sub-project 83, slice A -- the credential flows of `app/auth/routes.py`.
Everything an attacker reaches BEFORE holding an account lives here. Three
defects, all measured:

* the resend-verification form answered **"No user found with that email
  address."** for an address it does not know and "If an account exists, a
  link has been sent" for one it does, so the pair told a caller which
  addresses are registered (D1129);
* a password-reset link worked **more than once** -- the token is a stateless
  JWT and nothing about using it changed anything, so whoever came by the link
  afterwards took the account from the person who had just secured it
  (D1130);
* the login form said "No account exists with that user name." for an unknown
  name and "Invalid password" for a known one -- username enumeration on the
  most-probed form on the site, while the API arm of the same flow already
  answered one way for both (D1131).
"""
from unittest.mock import patch

import pytest

from app import db
from app.models import Instance, Site, User, UserRegistration
from app.utils import utcnow
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


# --------------------------------------------------------------------------
# D1131 -- the login form
# --------------------------------------------------------------------------


def flashes(response):
    return response.data.decode()


def login_with(client, name, password):
    return client.post('/auth/login',
                       data={'user_name': name, 'password': password,
                             'timezone': 'UTC', 'submit': 'Log In'},
                       follow_redirects=False)


def test_logging_in(app, env):
    anon, person = env

    response = login_with(anon, 'person', 'a-good-password')

    assert response.status_code == 302
    with anon.session_transaction() as session:
        # `login_user` stores `user.get_id()`, which is the column value --
        # an int here, not the string a hand-built session carries (fact 526).
        assert str(session['_user_id']) == str(person.id)


def test_logging_in_by_email(app, env):
    """`find_user` accepts the address as well as the name, so an account
    whose name you have forgotten is still reachable."""
    anon, person = env

    response = login_with(anon, 'person@example.com', 'a-good-password')

    assert response.status_code == 302
    with anon.session_transaction() as session:
        # `login_user` stores `user.get_id()`, which is the column value --
        # an int here, not the string a hand-built session carries (fact 526).
        assert str(session['_user_id']) == str(person.id)


def test_logging_in_by_email_ignores_its_case(app, env):
    """D584, fixed (owner ruling). `find_user` matched the address exactly, so
    an address typed with different capitals did not log in on the web while
    the API, which lowered both sides, accepted it. One finder now serves both."""
    anon, person = env

    response = login_with(anon, 'Person@Example.COM', 'a-good-password')

    assert response.status_code == 302
    with anon.session_transaction() as session:
        assert str(session['_user_id']) == str(person.id)


@pytest.mark.parametrize('name, password', [
    ('person', 'the-wrong-password'),
    ('nobody-at-all', 'a-good-password'),
])
def test_a_failed_login_says_the_same_thing_either_way(app, env, name,
                                                       password):
    """D1131. The form used to say "No account exists with that user name."
    for a name it does not know and "Invalid password" for one it does -- so
    the pair told a caller which accounts exist here, one guess at a time, on
    the most-probed form on the site. The API arm of the same flow already
    raised a single `incorrect_login` for both (fact 478)."""
    anon, person = env

    with patch('app.auth.util.flash') as flashed:
        response = login_with(anon, name, password)

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Invalid user name or password' in messages
    assert 'No account exists' not in messages
    with anon.session_transaction() as session:
        assert '_user_id' not in session


def test_the_failure_message_offers_the_reset_link(app, env):
    """The link is useful to everyone who has genuinely forgotten a password
    -- including the OAuth-created accounts that have none at all -- and,
    because it is shown for every failure, it distinguishes nothing."""
    anon, person = env

    with patch('app.auth.util.flash') as flashed:
        login_with(anon, 'person', 'the-wrong-password')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert '/auth/reset_password_request' in messages


def test_an_account_with_no_password_answers_the_same_way(app, env):
    """The old code offered a different message when `password_hash` was
    None, which said both that the account exists and that it has no
    password."""
    anon, person = env
    person.password_hash = None
    db.session.commit()

    with patch('app.auth.util.flash') as flashed:
        login_with(anon, 'person', 'anything-at-all')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Invalid user name or password' in messages


def test_a_deleted_account_answers_the_same_way(app, env):
    anon, person = env
    person.deleted = True
    db.session.commit()

    with patch('app.auth.util.flash') as flashed:
        response = login_with(anon, 'person', 'a-good-password')

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Invalid user name or password' in messages
    with anon.session_transaction() as session:
        assert '_user_id' not in session


def test_a_banned_account_is_told_so(app, env):
    """A ban is not a secret from the person banned -- they already know the
    account exists -- and the message is what stops them retrying."""
    anon, person = env
    person.banned = True
    db.session.commit()

    with patch('app.auth.util.flash') as flashed:
        response = login_with(anon, 'person', 'a-good-password')

    assert response.status_code == 302
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'You have been banned' in messages
    with anon.session_transaction() as session:
        assert '_user_id' not in session


def test_an_unverified_account_is_sent_to_check_its_email(app, env):
    anon, person = env
    person.verified = False
    db.session.commit()

    response = login_with(anon, 'person', 'a-good-password')

    assert response.headers['Location'] == '/auth/check_email'


def test_an_account_waiting_for_approval_is_told_to_wait(app, env):
    anon, person = env
    db.session.add(UserRegistration(user_id=person.id, status=0))
    db.session.commit()

    response = login_with(anon, 'person', 'a-good-password')

    assert response.headers['Location'] == '/auth/please_wait'


def test_the_login_page_renders(app, env):
    anon, person = env

    with patch('app.auth.routes.render_login_form',
               return_value='rendered') as render:
        response = anon.get('/auth/login')

    assert response.status_code == 200
    assert render.call_args is not None


def test_somebody_already_logged_in_is_sent_on(app, env):
    anon, person = env

    response = as_user(app, person).get('/auth/login')

    assert response.status_code == 302
    assert '/auth/login' not in response.headers['Location']


def test_logging_out(app, env):
    anon, person = env
    client = as_user(app, person)

    response = client.get('/auth/logout')

    assert response.status_code == 302
    with client.session_transaction() as session:
        assert '_user_id' not in session


def test_logging_out_clears_low_bandwidth_mode(app, env):
    """The cookie outlives the session, so a shared browser would otherwise
    keep the last person's setting."""
    anon, person = env

    response = as_user(app, person).get('/auth/logout')

    assert 'low_bandwidth=0' in response.headers['Set-Cookie']


# --------------------------------------------------------------------------
# D1129 -- the resend-verification form
# --------------------------------------------------------------------------


def resend_to(client, email):
    return client.post('/auth/resend_email',
                       data={'email': email,
                             'submit': 'Resend verification email'})


@pytest.mark.parametrize('email', ['person@example.com', 'nobody@example.com'])
def test_the_resend_form_says_the_same_thing_either_way(app, env, email):
    """D1129. It used to say "No user found with that email address." for an
    address it does not know, while the success path says "If an account
    exists, a link has been sent". Measured: `PROBE ar2 unknown address says:
    True`."""
    anon, person = env

    with patch('app.auth.routes.send_email_verification'):
        with patch('app.auth.routes.flash') as flashed:
            response = resend_to(anon, email)

    assert response.headers['Location'] == '/auth/check_email'
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'If an account exists' in messages
    assert 'No user found' not in messages


def test_a_known_address_is_actually_sent_the_email(app, env):
    anon, person = env
    person.verified = False
    db.session.commit()

    with patch('app.auth.routes.send_email_verification') as sent:
        resend_to(anon, 'person@example.com')

    assert sent.call_args.args[0].id == person.id


def test_an_unknown_address_is_sent_nothing(app, env):
    """The message is the same; what differs is that nothing is sent, which
    is what stops the form being used to mail strangers."""
    anon, person = env

    with patch('app.auth.routes.send_email_verification') as sent:
        resend_to(anon, 'nobody@example.com')

    assert sent.call_args is None


def test_an_account_with_no_verification_token_is_given_one(app, env):
    """Verification is impossible without a token, so the resend mints one
    rather than sending a link that cannot work."""
    anon, person = env
    person.verification_token = None
    db.session.commit()

    with patch('app.auth.routes.send_email_verification'):
        resend_to(anon, 'person@example.com')

    db.session.refresh(person)
    assert person.verification_token


def test_a_mail_server_that_refuses_says_so(app, env):
    anon, person = env

    with patch('app.auth.routes.send_email_verification',
               side_effect=Exception('smtp is down')):
        with patch('app.auth.routes.flash') as flashed:
            response = resend_to(anon, 'person@example.com')

    assert response.headers['Location'] == '/auth/resend_email'
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Problem sending email' in messages


def test_the_resend_form_renders(app, env):
    anon, person = env

    with patch('app.auth.routes.render_template',
               return_value='rendered') as render:
        response = anon.get('/auth/resend_email')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'auth/resend_email_request.html'


def test_somebody_logged_in_has_nothing_to_resend(app, env):
    anon, person = env

    response = as_user(app, person).get('/auth/resend_email')

    assert response.status_code == 302


# --------------------------------------------------------------------------
# D1130 -- the password-reset link
# --------------------------------------------------------------------------


def reset_with(client, token, password='a-brand-new-password'):
    return client.post(f'/auth/reset_password/{token}',
                       data={'password': password, 'password2': password,
                             'submit': 'Set password'})


def test_resetting_a_password(app, env):
    anon, person = env
    token = person.get_reset_password_token()

    response = reset_with(anon, token)

    assert response.status_code == 302
    db.session.refresh(person)
    assert person.check_password('a-brand-new-password')


def test_a_reset_link_works_only_once(app, env):
    """D1130. The token is a stateless JWT, so nothing about using one changed
    anything: the same link reset the password again, and again, until it
    expired. Measured: `PROBE ar3 first reset worked: True | same token
    reused: True`. The link outlives the reset in browser history, in a
    forwarded message, on a shared device."""
    anon, person = env
    token = person.get_reset_password_token()
    reset_with(anon, token, 'the-first-new-password')

    response = reset_with(anon, token, 'the-second-new-password')

    db.session.refresh(person)
    assert person.check_password('the-first-new-password')
    assert not person.check_password('the-second-new-password')
    assert response.headers['Location'] == '/home'


def test_a_link_issued_before_another_reset_is_spent_too(app, env):
    """Two links in flight: using either one spends both, because both were
    issued against the password that has now changed."""
    anon, person = env
    first = person.get_reset_password_token()
    second = person.get_reset_password_token()
    reset_with(anon, first, 'the-first-new-password')

    reset_with(anon, second, 'the-second-new-password')

    db.session.refresh(person)
    assert person.check_password('the-first-new-password')


def test_a_link_is_spent_by_any_other_password_change(app, env):
    """The token is bound to the password it was issued against, so changing
    the password anywhere else -- the settings page, an admin action --
    invalidates a link already in the post."""
    anon, person = env
    token = person.get_reset_password_token()
    person.set_password('changed-somewhere-else')
    db.session.commit()

    reset_with(anon, token, 'from-the-stale-link')

    db.session.refresh(person)
    assert person.check_password('changed-somewhere-else')


def test_a_token_with_no_fingerprint_is_refused(app, env):
    """Tokens minted before the fingerprint existed carry no `pw` claim.
    They are refused rather than honoured -- one more click on "forgot
    password" for their holders, and the window closed for everybody else."""
    import jwt
    from time import time
    anon, person = env
    legacy = jwt.encode({'reset_password': person.id, 'exp': time() + 600},
                        app.config['SECRET_KEY'], algorithm='HS256')

    reset_with(anon, legacy, 'from-a-legacy-link')

    db.session.refresh(person)
    assert person.check_password('a-good-password')


def test_a_token_for_an_account_that_is_gone(app, env):
    anon, person = env
    token = person.get_reset_password_token()
    person_id = person.id
    db.session.delete(person)
    db.session.commit()

    response = anon.post(f'/auth/reset_password/{token}',
                         data={'password': 'x' * 12, 'password2': 'x' * 12,
                               'submit': 'Set password'})

    assert response.status_code == 302
    assert db.session.get(User, person_id) is None


def test_a_token_that_is_not_a_token(app, env):
    anon, person = env

    response = anon.get('/auth/reset_password/not-a-real-token')

    assert response.status_code == 302
    assert response.headers['Location'] == '/home'


def test_an_expired_token(app, env):
    anon, person = env
    token = person.get_reset_password_token(expires_in=-1)

    reset_with(anon, token, 'too-late')

    db.session.refresh(person)
    assert person.check_password('a-good-password')


def test_the_reset_form_renders_for_a_good_token(app, env):
    anon, person = env
    token = person.get_reset_password_token()

    with patch('app.auth.routes.render_template',
               return_value='rendered') as render:
        response = anon.get(f'/auth/reset_password/{token}')

    assert response.status_code == 200
    assert render.call_args.kwargs['domain'] == app.config['SERVER_NAME']


def test_somebody_logged_in_does_not_need_a_reset_link(app, env):
    anon, person = env
    token = person.get_reset_password_token()

    response = as_user(app, person).get(f'/auth/reset_password/{token}')

    assert response.status_code == 302


# --------------------------------------------------------------------------
# Asking for the reset link
# --------------------------------------------------------------------------


def request_reset(client, email):
    return client.post('/auth/reset_password_request',
                       data={'email': email,
                             'submit': 'Request password reset'})


def test_asking_for_a_reset_link(app, env):
    anon, person = env

    with patch('app.auth.routes.send_password_reset_email') as sent:
        response = request_reset(anon, 'person@example.com')

    assert response.status_code == 302
    assert sent.call_args.args[0].id == person.id


@pytest.mark.parametrize('email', ['person@example.com', 'nobody@example.com'])
def test_the_reset_request_says_the_same_thing_either_way(app, env, email):
    """This end already answered the same way for both -- it is what D1129's
    sibling was measured against."""
    anon, person = env

    with patch('app.auth.routes.send_password_reset_email'):
        with patch('app.auth.routes.flash') as flashed:
            request_reset(anon, email)

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'If an account exists' in messages


def test_an_unknown_address_is_sent_no_reset_link(app, env):
    anon, person = env

    with patch('app.auth.routes.send_password_reset_email') as sent:
        request_reset(anon, 'nobody@example.com')

    assert sent.call_args is None


@pytest.mark.parametrize('email', ['postmaster@example.com',
                                   'abuse@example.com', 'noc@example.com'])
def test_role_addresses_are_refused(app, env, email):
    """A reset mailed to a role address reaches whoever reads that mailbox,
    which is not the account holder."""
    anon, person = env

    with patch('app.auth.routes.send_password_reset_email') as sent:
        with patch('app.auth.routes.flash') as flashed:
            request_reset(anon, email)

    assert sent.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'cannot use that email address' in messages


def test_the_reset_request_form_renders(app, env):
    anon, person = env

    with patch('app.auth.routes.render_template',
               return_value='rendered') as render:
        response = anon.get('/auth/reset_password_request')

    assert response.status_code == 200
    assert render.call_args.args[0] == 'auth/reset_password_request.html'


def test_somebody_logged_in_does_not_need_a_reset(app, env):
    anon, person = env

    response = as_user(app, person).get('/auth/reset_password_request')

    assert response.status_code == 302


# --------------------------------------------------------------------------
# The email-verification link
# --------------------------------------------------------------------------


def test_verifying_an_email_address(app, env):
    anon, person = env
    person.verified = False
    person.verification_token = 'a-token'
    person.private_key = None
    db.session.commit()

    with patch('app.auth.routes.finalize_user_setup') as finalized:
        response = anon.get('/auth/verify_email/a-token')

    assert response.status_code == 302
    db.session.refresh(person)
    assert person.verified is True
    assert finalized.call_args.args[0].id == person.id


def test_verifying_rotates_the_token(app, env):
    """The link is single-use: the token it carried is replaced as it is
    spent."""
    anon, person = env
    person.verified = False
    person.verification_token = 'a-token'
    db.session.commit()

    with patch('app.auth.routes.finalize_user_setup'):
        anon.get('/auth/verify_email/a-token')

    db.session.refresh(person)
    assert person.verification_token != 'a-token'


def test_clicking_the_link_twice_is_harmless(app, env):
    """People double-click links in email."""
    anon, person = env
    person.verified = True
    person.verification_token = 'a-token'
    db.session.commit()

    with patch('app.auth.routes.finalize_user_setup') as finalized:
        with patch('app.auth.routes.flash') as flashed:
            response = anon.get('/auth/verify_email/a-token')

    assert response.headers['Location'] == '/auth/login'
    assert finalized.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Thank you for verifying' in messages


def test_a_banned_account_cannot_verify(app, env):
    anon, person = env
    person.verified = False
    person.banned = True
    person.verification_token = 'a-token'
    db.session.commit()

    with patch('app.auth.routes.flash') as flashed:
        response = anon.get('/auth/verify_email/a-token')

    assert response.headers['Location'] == '/home'
    db.session.refresh(person)
    assert person.verified is False
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'You have been banned' in messages


def test_a_token_nobody_holds(app, env):
    anon, person = env

    with patch('app.auth.routes.flash') as flashed:
        response = anon.get('/auth/verify_email/not-anybodys-token')

    assert response.headers['Location'] == '/home'
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'validation failed' in messages


def test_verifying_moves_an_application_into_the_queue(app, env):
    """An application sits at -1 until the address is verified; only then is
    it worth an admin's attention, and only then are they told."""
    anon, person = env
    person.verified = False
    person.verification_token = 'a-token'
    db.session.commit()
    application = UserRegistration(user_id=person.id, status=-1)
    db.session.add(application)
    db.session.commit()

    with patch('app.auth.routes.notify_admins_of_registration') as notified:
        response = anon.get('/auth/verify_email/a-token')

    db.session.refresh(application)
    assert application.status == 0
    assert notified.call_args.args[0].id == application.id
    assert response.headers['Location'] == '/auth/please_wait'


def test_an_application_already_in_the_queue_is_not_announced_again(app, env):
    """The lookup is filtered to `status=-1` -- an application still waiting
    on its email address. One already at 0 is in the moderators' queue, and
    verifying again (a second click, a changed address) must not put it there
    twice or tell them twice."""
    anon, person = env
    person.verified = False
    person.verification_token = 'a-token'
    db.session.commit()
    application = UserRegistration(user_id=person.id, status=0)
    db.session.add(application)
    db.session.commit()

    with patch('app.auth.routes.notify_admins_of_registration') as notified:
        anon.get('/auth/verify_email/a-token')

    assert notified.call_args is None
    db.session.refresh(application)
    assert application.status == 0


def test_an_account_that_already_has_keys_is_not_set_up_again(app, env):
    """Changing an email address runs this same path, and finalising again
    would reset the account's keys -- which is how it would stop federating.
    """
    anon, person = env
    person.verified = False
    person.verification_token = 'a-token'
    db.session.commit()
    assert person.private_key is not None

    with patch('app.auth.routes.finalize_user_setup') as finalized:
        anon.get('/auth/verify_email/a-token')

    assert finalized.call_args is None


def test_verifying_logs_the_person_in(app, env):
    anon, person = env
    person.verified = False
    person.verification_token = 'a-token'
    db.session.commit()

    with patch('app.auth.routes.finalize_user_setup'):
        anon.get('/auth/verify_email/a-token')

    with anon.session_transaction() as session:
        # `login_user` stores `user.get_id()`, which is the column value --
        # an int here, not the string a hand-built session carries (fact 526).
        assert str(session['_user_id']) == str(person.id)


def test_a_verified_applicant_is_told_when_approval_is_needed(app, env):
    anon, person = env
    site = db.session.get(Site, 1)
    site.registration_mode = 'RequireApplication'
    person.verified = False
    person.verification_token = 'a-token'
    db.session.commit()

    with patch('app.auth.routes.send_registration_approved_email') as sent:
        with patch('app.auth.routes.finalize_user_setup'):
            anon.get('/auth/verify_email/a-token')

    assert sent.call_args.args[0].id == person.id


def test_an_empty_token_is_not_a_token(app, env):
    anon, person = env

    assert anon.get('/auth/verify_email/').status_code in (301, 308, 404)


# --------------------------------------------------------------------------
# The pages the flows redirect to
# --------------------------------------------------------------------------


@pytest.mark.parametrize('path, marker', [
    ('/auth/please_wait', b'Account under review'),
    ('/auth/check_email', b'Check your email'),
    ('/auth/not_trustworthy', b'low reputation'),
])
def test_the_waiting_rooms_render(app, env, path, marker):
    anon, person = env

    response = anon.get(path)

    assert response.status_code == 200
    assert marker in response.data


def test_the_validation_required_page(app, env):
    anon, person = env

    response = anon.get('/auth/validation_required')

    assert response.status_code == 200


def test_the_permission_denied_page(app, env):
    anon, person = env

    response = anon.get('/auth/permission_denied')

    assert response.status_code == 200
