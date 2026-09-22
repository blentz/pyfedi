"""Signing up with an OAuth provider.

Sub-project 83, slice C -- `app/auth/oauth_util.py` and the Mastodon form arm
of `app/auth/routes.py`. Six defects, all measured:

* every first-time Google or Discord sign-up ended in `TypeError: The view
  function ... did not return a valid response`, because
  `handle_user_verification` returned None on the ordinary registration path
  -- after the account had been created and logged in (D1138);
* `find_new_username` took the email's local part VERBATIM as the user name,
  so an OAuth sign-in minted names the registration form refuses: `admin`,
  and names outside USER_NAME_CHARSET_RE (D1139);
* the same function's uniqueness test was case SENSITIVE while every other
  one is not, so `person` was created beside `Person` (D1140);
* the Mastodon arm asks the visitor for an email, because Mastodon does not
  supply one, and nothing checked whether that address was already somebody's
  -- two accounts ended up holding one (D1141);
* a visitor on a banned IP had no account yet, so nothing refused them: a row
  was written with `banned=True` and then logged in (D1142);
* a provider that hands back no email at all answered `AttributeError:
  'NoneType' object has no attribute 'lower'` (D1143).
"""
from unittest.mock import MagicMock, patch

import pytest

from app import db
from app.auth.oauth_util import (can_user_register, current_user_is_banned,
                                 current_user_is_deleted, find_new_username,
                                 get_token_and_user_info, initialize_new_user,
                                 is_country_blocked, refuse_banned_visitor)
from app.models import Instance, Site, User, UserRegistration
from tests.factories import make_community, make_local_feed, make_user, make_instance

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
    site.application_question = ''
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    person = make_user(local, 'Person', local=True)
    person.email = 'person@example.com'
    db.session.commit()
    return app.test_client(), person


def oauth_says(**user_info):
    """What the provider told us. `get_token_and_user_info` is the seam: it
    holds every network call the three providers make."""
    info = {'id': 'remote-1', 'email': 'someone@example.com',
            'username': 'Someone'}
    info.update(user_info)
    return patch('app.auth.oauth_util.get_token_and_user_info',
                 return_value=('a-token', info))


def no_setup():
    """`finalize_user_setup` generates keys and writes files; every row that
    registers somebody stands it down."""
    return patch('app.auth.oauth_util.finalize_user_setup')


# --------------------------------------------------------------------------
# D1139, D1140 -- the name an OAuth signup gets
# --------------------------------------------------------------------------


def test_the_email_local_part_becomes_the_user_name(app, env):
    """The feature itself, which the guards below must not break."""
    client, person = env

    assert find_new_username('newcomer@example.com') == 'newcomer'


def test_a_reserved_user_name_cannot_be_taken_through_oauth(app, env):
    """D1139. `admin` is the name `process_registration_form` reserves, and
    `find_new_username` answered it happily. Measured: `PROBE av1 admin
    exists: True`."""
    client, person = env

    assert find_new_username('admin@evil.example') != 'admin'


def test_characters_outside_the_charset_are_dropped(app, env):
    """D1139. USER_NAME_CHARSET_RE is letters, digits and underscore, because
    a local user name is interpolated into an actor URL, a webfinger answer
    and a feed regex. Measured: `PROBE av2 names: ['founder', 'Person',
    'we.ird+chars!']`."""
    client, person = env

    assert find_new_username('we.ird+chars!@evil.example') == 'weirdchars'


def test_a_name_too_short_to_register_is_replaced(app, env):
    """`Length(min=3)` is the registration form's rule. A local part that
    cannot meet it after the charset is applied gets a random name instead."""
    client, person = env

    name = find_new_username('a.b@example.com')

    assert len(name) == 10
    assert name != 'ab'


def test_a_blocked_word_in_the_local_part_is_replaced(app, env):
    """The registration path refuses these outright; here there is nobody to
    tell, so the signup gets a name that does not carry the word."""
    client, person = env

    with patch('app.auth.oauth_util.actor_contains_blocked_words',
               return_value=True):
        name = find_new_username('spamword@example.com')

    assert name != 'spamword'
    assert len(name) == 10


def test_a_name_already_held_in_another_case_is_not_reused(app, env):
    """D1140. The check was `User.user_name == local_part` -- case sensitive
    -- while `find_user` lowers both sides and takes `.first()`. Measured:
    `PROBE av3 person-ish names: ['Person', 'person']`."""
    client, person = env

    name = find_new_username('person@other.example')

    assert name.lower() != 'person'
    assert name.startswith('person')


def test_a_community_name_is_not_available_either(app, env):
    """`RegistrationForm.validate_user_name` refuses a name a community
    holds; the OAuth path never asked."""
    client, person = env
    make_community('general')
    db.session.commit()

    assert find_new_username('general@example.com') != 'general'


def test_a_feed_name_is_not_available_either(app, env):
    """The third of the form's three queries."""
    client, person = env
    make_local_feed('newsfeed')
    db.session.commit()

    assert find_new_username('newsfeed@example.com') != 'newsfeed'


def test_a_remote_account_of_the_same_name_does_not_block_it(app, env):
    """`ap_id == None` in all three queries: only LOCAL names are ours to
    hand out."""
    client, person = env
    make_user(make_instance('remote.example'), 'remoteperson')
    db.session.commit()

    assert find_new_username('remoteperson@example.com') == 'remoteperson'


def test_a_name_that_stays_taken_falls_back_to_a_random_one(app, env):
    """A thousand attempts and then `gibberish(10)`, so the loop cannot spin
    forever on an instance where every candidate collides."""
    client, person = env

    with patch('app.auth.oauth_util.user_name_is_taken', return_value=True):
        name = find_new_username('newcomer@example.com')

    assert len(name) == 10
    assert not name.startswith('newcomer')


# --------------------------------------------------------------------------
# The small helpers
# --------------------------------------------------------------------------


def test_no_country_is_not_a_blocked_country(app, env):
    client, person = env

    assert is_country_blocked('') is False


def test_a_declined_country_is_blocked(app, env):
    client, person = env

    with patch('app.auth.oauth_util.get_setting', return_value='xx\nyy'):
        assert is_country_blocked('XX') is True


def test_a_country_that_is_not_on_the_list_is_allowed(app, env):
    client, person = env

    with patch('app.auth.oauth_util.get_setting', return_value='xx\nyy'):
        assert is_country_blocked('ZZ') is False


def test_an_unconfigured_provider_yields_nothing(app, env):
    """`getattr(oauth, provider, None)` is None when the instance has not
    configured that provider, and the caller flashes one generic failure."""
    client, person = env

    with patch('app.auth.oauth_util.oauth', MagicMock(spec=[])):
        assert get_token_and_user_info('nosuch', 'endpoint') == (None, None)


def test_the_token_and_the_profile_come_back_together(app, env):
    client, person = env
    provider = MagicMock()
    provider.authorize_access_token.return_value = 'a-token'
    provider.get.return_value.json.return_value = {'id': 'remote-1'}

    with patch('app.auth.oauth_util.oauth', MagicMock(google=provider)):
        token, user_info = get_token_and_user_info('google', 'endpoint')

    assert token == 'a-token'
    assert user_info == {'id': 'remote-1'}


def test_a_provider_that_raises_yields_nothing(app, env):
    """Whatever the OAuth client raises stays inside this function: the
    caller gets one message that names nothing (D1136's shape)."""
    client, person = env
    provider = MagicMock()
    provider.authorize_access_token.side_effect = Exception('token secret=abc')

    with patch('app.auth.oauth_util.oauth', MagicMock(google=provider)):
        assert get_token_and_user_info('google', 'endpoint') == (None, None)


def test_nobody_logged_in_is_neither_banned_nor_deleted(app, env):
    client, person = env

    with app.test_request_context('/'):
        assert current_user_is_banned() is False
        assert current_user_is_deleted() is False


def test_a_banned_account_is_banned(app, env):
    client, person = env
    person.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.auth.oauth_util.current_user', person):
            assert current_user_is_banned() is True


def test_a_banned_ip_bans_the_account_on_it(app, env):
    client, person = env

    with app.test_request_context('/'):
        with patch('app.auth.oauth_util.current_user', person):
            with patch('app.auth.oauth_util.user_ip_banned', return_value=True):
                assert current_user_is_banned() is True


def test_a_deleted_account_is_deleted(app, env):
    client, person = env
    person.deleted = True
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.auth.oauth_util.current_user', person):
            assert current_user_is_deleted() is True


def test_an_open_instance_lets_anybody_register(app, env):
    client, person = env

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        assert can_user_register() is True


def test_a_closed_instance_sends_them_to_the_login_page(app, env):
    client, person = env
    db.session.get(Site, 1).registration_mode = 'Closed'
    db.session.commit()

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with patch('app.auth.oauth_util.flash') as flashed:
            response = can_user_register()

    assert response.headers['Location'] == '/auth/login'
    assert 'closed' in str(flashed.call_args.args[0])


def test_applications_with_no_question_are_closed_too(app, env):
    """`RequireApplication` with nothing to ask is a mode nobody can satisfy,
    so it is treated as closed."""
    client, person = env
    site = db.session.get(Site, 1)
    site.registration_mode = 'RequireApplication'
    site.application_question = ''
    db.session.commit()

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with patch('app.auth.oauth_util.flash'):
            response = can_user_register()

    assert response.headers['Location'] == '/auth/login'


# --------------------------------------------------------------------------
# D1142 -- a visitor with no account yet was never asked about the ban
# --------------------------------------------------------------------------


def test_an_ordinary_visitor_is_not_refused(app, env):
    client, person = env

    with app.test_request_context('/'):
        assert refuse_banned_visitor() is None


def test_a_banned_ip_may_not_register(app, env):
    """D1142. `handle_oauth_authorize` sends an EXISTING banned account to
    `handle_banned_user`; a visitor who had no account yet was written into
    the database with `banned=True` and then logged in. Measured: `PROBE av5
    outcome: ... | created: True | banned: True`."""
    client, person = env

    with app.test_request_context('/'):
        with patch('app.auth.oauth_util.user_ip_banned', return_value=True):
            with patch('app.auth.oauth_util.flash') as flashed:
                response = refuse_banned_visitor()

    assert response.headers['Location'] == '/auth/login'
    assert 'banned' in str(flashed.call_args.args[0])


def test_a_banned_cookie_may_not_register_either(app, env):
    client, person = env

    with app.test_request_context('/'):
        with patch('app.auth.oauth_util.user_cookie_banned', return_value=True):
            with patch('app.auth.oauth_util.flash'):
                response = refuse_banned_visitor()

    assert response.headers['Location'] == '/auth/login'


def test_a_banned_visitor_gets_no_account_from_the_provider(app, env):
    """The same refusal through the whole route."""
    client, person = env
    before = User.query.count()

    with oauth_says(email='brand-new@example.com'):
        with no_setup():
            with patch('app.auth.oauth_util.user_ip_banned', return_value=True):
                response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/auth/login'
    assert User.query.count() == before


# --------------------------------------------------------------------------
# D1138, D1143 -- what the callback answers
# --------------------------------------------------------------------------


def test_a_brand_new_account_is_registered_and_sent_to_onboarding(app, env):
    """D1138. This used to `return None`, and a Flask view that returns None
    is a 500 -- after the account had been created and logged in. Measured:
    `PROBE av4 outcome: TypeError: The view function for
    'auth.google_authorize' did not return a valid response ... | user
    created: True`."""
    client, person = env

    with oauth_says(email='brand-new@example.com'):
        with no_setup():
            response = client.get('/auth/google_authorize')

    assert response.status_code == 302
    assert response.headers['Location'] == '/auth/filter_selection'
    made = User.query.filter_by(email='brand-new@example.com').first()
    assert made is not None
    assert made.google_oauth_id == 'remote-1'


def test_a_provider_that_gives_us_no_email_is_refused(app, env):
    """D1143. Mastodon's `accounts/verify_credentials` does not return an
    email, and `email.lower()` answered `AttributeError: 'NoneType' object
    has no attribute 'lower'`. Measured: `PROBE av7 outcome: AttributeError:
    'NoneType' object has no attribute 'lower'`."""
    client, person = env

    with oauth_says(email=None):
        with no_setup():
            with patch('app.auth.oauth_util.flash') as flashed:
                response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/auth/login'
    assert 'email address' in str(flashed.call_args.args[0])
    assert User.query.filter_by(google_oauth_id='remote-1').first() is None


def test_an_email_that_is_already_somebodys_is_refused(app, env):
    """The provider-supplied end of D1141, which has always been checked."""
    client, person = env

    with oauth_says(email='person@example.com'):
        with no_setup():
            with patch('app.auth.oauth_util.flash') as flashed:
                response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/auth/login'
    assert 'already exists' in str(flashed.call_args.args[0])


def test_a_failed_handshake_says_nothing_about_why(app, env):
    client, person = env

    with patch('app.auth.oauth_util.get_token_and_user_info',
               return_value=(None, None)):
        with patch('app.auth.oauth_util.flash') as flashed:
            response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/auth/login'
    assert 'OAuth server' in str(flashed.call_args.args[0])


def test_a_closed_instance_refuses_the_callback_too(app, env):
    client, person = env
    db.session.get(Site, 1).registration_mode = 'Closed'
    db.session.commit()

    with oauth_says(email='brand-new@example.com'):
        with patch('app.auth.oauth_util.flash'):
            response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/auth/login'
    assert User.query.filter_by(email='brand-new@example.com').first() is None


def test_a_declined_country_stops_the_callback(app, env):
    client, person = env

    with oauth_says(email='brand-new@example.com'):
        with patch('app.auth.oauth_util.is_country_blocked', return_value=True):
            with patch('app.auth.oauth_util.flash'):
                response = client.get('/auth/google_authorize')

    assert response.status_code == 200
    assert b'not accepting registrations from your country' in response.data


def test_an_account_that_already_has_this_provider_id_is_logged_in(app, env):
    """`finalize_user_login`: the last-seen, IP and country columns are
    written before the session is handed out."""
    client, person = env
    person.google_oauth_id = 'remote-1'
    person.last_seen = None
    db.session.commit()

    with oauth_says():
        response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/home'
    assert person.last_seen is not None


def test_a_banned_account_is_refused_at_the_callback(app, env):
    client, person = env
    person.google_oauth_id = 'remote-1'
    person.banned = True
    db.session.commit()

    with oauth_says():
        with patch('app.auth.util.flash') as flashed:
            response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/auth/login'
    assert 'banned' in str(flashed.call_args.args[0])


def test_the_founder_is_not_locked_out_by_an_ip_ban(app, env):
    """`user.id != 1`: whoever set the instance up keeps a way back in."""
    client, person = env
    founder = db.session.get(User, 1)
    founder.google_oauth_id = 'remote-1'
    db.session.commit()

    with oauth_says():
        with patch('app.auth.oauth_util.user_ip_banned', return_value=True):
            response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/home'


def test_a_deleted_account_is_refused_at_the_callback(app, env):
    client, person = env
    person.google_oauth_id = 'remote-1'
    person.deleted = True
    db.session.commit()

    with oauth_says():
        with patch('app.auth.oauth_util.flash') as flashed:
            response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/auth/login'
    assert 'deleted' in str(flashed.call_args.args[0])


def test_an_application_instance_sends_a_new_account_to_the_queue(app, env):
    """The other arm of `handle_user_verification`: the account exists but
    waits, and the ban-check task is asked about it."""
    client, person = env
    site = db.session.get(Site, 1)
    site.registration_mode = 'RequireApplication'
    site.application_question = 'Why do you want to join?'
    db.session.commit()

    with oauth_says(email='brand-new@example.com'):
        with no_setup():
            with patch('app.auth.oauth_util.task_selector') as task:
                response = client.get('/auth/google_authorize')

    assert response.headers['Location'] == '/auth/please_wait'
    made = User.query.filter_by(email='brand-new@example.com').first()
    assert UserRegistration.query.filter_by(user_id=made.id).first() is not None
    assert task.call_args.args[0] == 'check_application'


# --------------------------------------------------------------------------
# initialize_new_user
# --------------------------------------------------------------------------


def test_a_provider_that_gives_no_display_name_gets_the_user_name(app, env):
    """"Sometimes OAuth doesn't give us one" -- the row falls back rather
    than leaving a blank byline."""
    client, person = env

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with no_setup():
            user = initialize_new_user('newcomer@example.com', '  ',
                                       'google_oauth_id', {'id': 'remote-2'},
                                       '127.0.0.1', 'GB')

    assert user.title == user.user_name == 'newcomer'


def test_content_warning_instances_start_people_seeing_everything(app, env):
    """`CONTENT_WARNING` means the instance warns rather than hides, so the
    new account does not start with NSFW filtered out."""
    client, person = env

    with app.test_request_context('/'):
        from flask import g
        g.site = db.session.get(Site, 1)
        with no_setup():
            with patch.dict(app.config, {'CONTENT_WARNING': True}):
                user = initialize_new_user('newcomer@example.com', 'New',
                                           'google_oauth_id',
                                           {'id': 'remote-2'},
                                           '127.0.0.1', 'GB')

    assert user.hide_nsfw == 0


# --------------------------------------------------------------------------
# D1141 -- the Mastodon form, the one arm that asks for an email
# --------------------------------------------------------------------------


def test_the_mastodon_form_is_shown_once_the_provider_answers(app, env):
    """Mastodon gives no email, so `handle_oauth_authorize` renders a form
    for one and parks the profile in the session."""
    client, person = env

    with oauth_says(email=None, id='remote-9'):
        response = client.get('/auth/mastodon_authorize')

    assert response.status_code == 200
    with client.session_transaction() as session:
        assert session['user_info']['id'] == 'remote-9'


def test_an_email_somebody_else_holds_is_refused_on_the_form(app, env):
    """D1141. Nothing checked the address this form collects, so two
    accounts ended up holding one. Measured: `PROBE av6 status: 302 |
    accounts holding that email: 2 | names: ['Person', 'person']`.
    Login-by-email, the reset request and the resend form all take
    `.first()`."""
    client, person = env
    with client.session_transaction() as session:
        session['user_info'] = {'id': 'remote-9', 'username': 'Someone'}

    with no_setup():
        with patch('app.auth.forms.get_setting', return_value=False):
            response = client.post('/auth/mastodon_authorize',
                                   data={'email': 'person@example.com',
                                         'submit': 'Register'})

    assert response.status_code == 200
    assert b'already exists' in response.data
    assert User.query.filter_by(email='person@example.com').count() == 1


def test_the_mastodon_form_registers_an_account_with_a_free_email(app, env):
    """The feature the check above must not break."""
    client, person = env
    with client.session_transaction() as session:
        session['user_info'] = {'id': 'remote-9', 'username': 'Someone'}

    with no_setup():
        response = client.post('/auth/mastodon_authorize',
                               data={'email': 'brand-new@example.com',
                                     'submit': 'Register'})

    assert response.status_code == 302
    made = User.query.filter_by(email='brand-new@example.com').first()
    assert made is not None
    assert made.mastodon_oauth_id == 'remote-9'


def test_a_banned_visitor_gets_no_account_from_the_mastodon_form(app, env):
    """D1142's second site."""
    client, person = env
    with client.session_transaction() as session:
        session['user_info'] = {'id': 'remote-9', 'username': 'Someone'}
    before = User.query.count()

    with no_setup():
        with patch('app.auth.oauth_util.user_ip_banned', return_value=True):
            with patch('app.auth.oauth_util.flash'):
                response = client.post('/auth/mastodon_authorize',
                                       data={'email': 'brand-new@example.com',
                                             'submit': 'Register'})

    assert response.headers['Location'] == '/auth/login'
    assert User.query.count() == before


def test_a_refused_email_can_be_corrected(app, env):
    """The profile stays in the session across the refusal, or the visitor's
    second attempt falls through to a provider handshake that has already
    been spent."""
    client, person = env
    with client.session_transaction() as session:
        session['user_info'] = {'id': 'remote-9', 'username': 'Someone'}

    with no_setup():
        client.post('/auth/mastodon_authorize',
                    data={'email': 'person@example.com', 'submit': 'Register'})
        response = client.post('/auth/mastodon_authorize',
                               data={'email': 'brand-new@example.com',
                                     'submit': 'Register'})

    assert response.status_code == 302
    assert User.query.filter_by(email='brand-new@example.com').first() is not None


def test_a_form_that_does_not_validate_is_shown_again(app, env):
    """A POST that fails the form's own validators used to fall through to
    `handle_oauth_authorize`, which asks the provider for a token it has
    already spent -- so the visitor was told the OAuth server was at fault."""
    client, person = env
    with client.session_transaction() as session:
        session['user_info'] = {'id': 'remote-9', 'username': 'Someone'}

    response = client.post('/auth/mastodon_authorize',
                           data={'email': '', 'submit': 'Register'})

    assert response.status_code == 200
    assert b'problem with the OAuth server' not in response.data
