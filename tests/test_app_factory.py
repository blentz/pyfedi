"""app/__init__.py -- get_locale and the factory's configuration branches.

MEASUREMENT BASIS. The module stood at 81.938% on the full-suite --cov=app run
at d5b160646, carrying 26 missing lines: get_locale's logged-in, session and
exception arms, and every branch of create_app that depends on a config value
the test environment has never set.

No production change. The uncovered branches are configuration, not defects --
which is worth saying plainly, because it is the first round since 66 to repair
nothing.

THE SHARED LOGGER. Flask.logger is logging.getLogger(app.name), and every app
built from this package is called 'app', so an app a test builds shares one
logger object with the session's app. Handlers accumulate on it globally:

    PROBE e1 handlers: ['RotatingFileHandler', 'SMTPHandler', 'RotatingFileHandler']

The factory attaches an SMTPHandler at ERROR level whenever MAIL_SERVER and
ERRORS_TO are set, and MAIL_SUPPRESS_SEND does not reach it -- it is a smtplib
client, not Flask-Mail. Leaving one attached would put every later test in the
session one app.logger.error() away from a real connection attempt. The
`restores_the_shared_logger` fixture below is what stops that, and it runs even
when a row fails. Fact 338.
"""
import logging
import os
from unittest.mock import patch

import pytest
from flask import session as flask_session

from app import create_app, get_locale
from app.models import Site
from tests.conftest import TestConfig
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def restores_the_shared_logger():
    """Put the 'app' logger back exactly as it was found.

    Yields nothing: every row that builds an app requests it for the teardown.
    A `finally` in each row would do the same job and be forgotten by the next
    one added.
    """
    logger = logging.getLogger('app')
    before = list(logger.handlers)
    yield
    for handler in list(logger.handlers):
        if handler not in before:
            logger.removeHandler(handler)
            handler.close()


def _config(**overrides):
    """A TestConfig subclass carrying the values a branch needs.

    Built per row rather than shared, because create_app mutates the config it
    is handed -- SERVER_URL and the API_* keys are written onto it.
    """
    return type('OneOffConfig', (TestConfig,), overrides)


# --------------------------------------------------------------------------
# get_locale
# --------------------------------------------------------------------------


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    return instance, alice


def test_a_signed_in_readers_own_language_wins(app, db_session):
    """`:30-31`, the first arm. It is checked before the session and before the
    request's header, so a reader who has chosen a language keeps it wherever
    they log in from.
    """
    from flask_login import login_user
    from app import db

    instance, alice = _seed()
    alice.interface_language = 'de'
    db.session.commit()

    with app.test_request_context('/', headers={'Accept-Language': 'fr'}):
        login_user(alice)

        assert get_locale() == 'de'


def test_a_signed_in_reader_with_no_language_falls_through(app, db_session):
    """The `and current_user.interface_language` conjunct: being logged in is
    not enough, and without this row a mutant dropping it would survive.
    """
    from flask_login import login_user
    from app import db

    instance, alice = _seed()
    alice.interface_language = None
    db.session.commit()

    with app.test_request_context('/', headers={'Accept-Language': 'fr'}):
        login_user(alice)

        assert get_locale() == 'fr'


def test_the_session_language_is_used_when_nobody_is_signed_in(app, db_session):
    """`:32-33`, the second arm -- how an anonymous visitor's chosen language
    survives between requests.
    """
    _seed()
    with app.test_request_context('/', headers={'Accept-Language': 'fr'}):
        flask_session['ui_language'] = 'es'

        assert get_locale() == 'es'


def test_the_browsers_header_is_used_when_nothing_else_is_set(app, db_session):
    _seed()
    with app.test_request_context('/', headers={'Accept-Language': 'fr,en;q=0.8'}):

        assert get_locale() == 'fr'


@pytest.mark.parametrize('headers', [
    {},
    {'Accept-Language': 'xx-ZZ'},
])
def test_a_request_that_names_no_known_language_gets_english(app, db_session, headers):
    """D864's repair. `best_match` returns None rather than raising for a
    request with no Accept-Language header, or one matching nothing in
    LANGUAGES, and the `except:` below only catches exceptions -- so that None
    used to be returned as though it were a locale:

        PROBE d1 no Accept-Language -> None
        PROBE d1 unmatchable -> None

    dateparser.parse(languages=[None]) raises, and a bare except two modules
    away reported it to the user as "Invalid." for every reminder.
    """
    _seed()
    with app.test_request_context('/', headers=headers):

        assert get_locale() == 'en'


def test_english_is_the_answer_when_there_is_no_request_at_all(app, db_session):
    """`:44-45`, the outer handler. Outside a request context `current_user`
    raises, and the caller still needs a locale -- `app/cli.py` reaches this on
    every scheduled task.
    """
    assert get_locale() == 'en'


def test_a_language_the_site_does_not_offer_is_not_chosen(app, db_session):
    """`best_match` is against `LANGUAGES`, not against anything the browser
    sends: a header naming a real language the site does not carry falls
    through to English rather than being honoured.
    """
    _seed()
    assert 'ru' not in app.config['LANGUAGES']
    with app.test_request_context('/', headers={'Accept-Language': 'ru'}):

        assert get_locale() == 'en'


# --------------------------------------------------------------------------
# create_app: the configuration branches
# --------------------------------------------------------------------------


@pytest.mark.parametrize('protocol, expected', [
    ('mixed', 'https://test.piefed.local'),
    ('https', 'https://test.piefed.local'),
    ('http', 'http://test.piefed.local'),
])
def test_the_server_url_is_built_from_the_protocol(restores_the_shared_logger,
                                                   protocol, expected):
    """`:139-142`. 'mixed' is the odd one: it serves the web UI over http while
    federating over https, so SERVER_URL -- which is what goes into outgoing
    ActivityPub ids -- is forced to https and does NOT follow HTTP_PROTOCOL.
    The http row is what makes that distinction visible.
    """
    built = create_app(_config(HTTP_PROTOCOL=protocol))

    assert built.config['SERVER_URL'] == expected


def test_sentry_is_initialised_only_when_a_dsn_is_configured(restores_the_shared_logger):
    """`:144-149`. sentry_sdk.init is patched: the real one installs global
    excepthooks and starts a transport thread, neither of which a test should
    leave behind.
    """
    with patch('sentry_sdk.init') as sentry:
        create_app(_config(SENTRY_DSN='https://public@sentry.example/1'))

        assert sentry.call_count == 1
        assert sentry.call_args.kwargs['dsn'] == 'https://public@sentry.example/1'
        assert sentry.call_args.kwargs['enable_tracing'] is False

    with patch('sentry_sdk.init') as sentry:
        create_app(_config(SENTRY_DSN=''))

        assert sentry.call_count == 0


def test_a_missing_secret_key_refuses_to_build_an_app(restores_the_shared_logger):
    """`:156-157`. The factory raises rather than running with an empty key,
    which would make every session cookie forgeable.
    """
    with pytest.raises(Exception, match='SECRET_KEY'):
        create_app(_config(SECRET_KEY=''))


@pytest.mark.parametrize('serve_docs, expected_prefix', [
    (True, '/api/alpha'),
    (False, None),
])
def test_the_api_docs_are_served_only_when_asked_for(restores_the_shared_logger,
                                                     serve_docs, expected_prefix):
    built = create_app(_config(SERVE_API_DOCS=serve_docs))

    assert built.config.get('OPENAPI_URL_PREFIX') == expected_prefix
    if serve_docs:
        assert built.config['OPENAPI_SWAGGER_UI_PATH'] == '/swagger'
        assert built.config['API_SPEC_OPTIONS']['security'] == [{'bearerAuth': []}]


@pytest.mark.parametrize('provider, overrides, expected_name', [
    ('google', {'GOOGLE_OAUTH_CLIENT_ID': 'gid', 'GOOGLE_OAUTH_SECRET': 'gsecret'},
     'google'),
    ('mastodon', {'MASTODON_OAUTH_CLIENT_ID': 'mid', 'MASTODON_OAUTH_SECRET': 'msecret',
                  'MASTODON_OAUTH_DOMAIN': 'mastodon.example'}, 'mastodon'),
    ('discord', {'DISCORD_OAUTH_CLIENT_ID': 'did', 'DISCORD_OAUTH_SECRET': 'dsecret'},
     'discord'),
])
def test_each_oauth_provider_is_registered_only_when_configured(
        restores_the_shared_logger, provider, overrides, expected_name):
    """`:233-265`, three near-identical blocks. oauth.register is patched
    because the registry is a MODULE-LEVEL Authlib object shared by every app
    in the process -- registering a provider for real would leak into the
    session's app and into every later row.
    """
    with patch('app.oauth.register') as register:
        create_app(_config(**overrides))

    names = [call.kwargs['name'] for call in register.call_args_list]
    assert expected_name in names


def test_no_oauth_provider_is_registered_by_default(restores_the_shared_logger):
    """The other arm of all three guards at once: a config with no client ids
    registers nothing.
    """
    with patch('app.oauth.register') as register:
        create_app(_config())

    assert register.call_args_list == []


def test_the_mastodon_urls_are_built_from_its_domain(restores_the_shared_logger):
    """Mastodon's block differs from the other two: its three URLs are
    f-strings over MASTODON_OAUTH_DOMAIN rather than constants, so a wrong
    domain sends users to another server's consent screen.
    """
    with patch('app.oauth.register') as register:
        create_app(_config(MASTODON_OAUTH_CLIENT_ID='mid', MASTODON_OAUTH_SECRET='s',
                           MASTODON_OAUTH_DOMAIN='mastodon.example'))

    kwargs = next(call.kwargs for call in register.call_args_list
                  if call.kwargs['name'] == 'mastodon')
    assert kwargs['authorize_url'] == 'https://mastodon.example/oauth/authorize'
    assert kwargs['access_token_url'] == 'https://mastodon.example/oauth/token'
    assert kwargs['api_base_url'] == 'https://mastodon.example/api/v1/'


# --------------------------------------------------------------------------
# create_app: the error mailer
# --------------------------------------------------------------------------


def _mail_handlers(built):
    from logging.handlers import SMTPHandler

    return [h for h in built.logger.handlers if isinstance(h, SMTPHandler)]


def test_no_error_mailer_without_both_a_server_and_a_recipient(restores_the_shared_logger):
    """`:332`'s two conjuncts, one row per way of failing it. Either alone
    leaves the handler off, which is what stops a half-configured instance
    building an SMTPHandler that fails on the first error it tries to report.
    """
    assert _mail_handlers(create_app(_config(MAIL_SERVER='', ERRORS_TO='e@example.com'))) == []
    assert _mail_handlers(create_app(_config(MAIL_SERVER='smtp.example', ERRORS_TO=''))) == []


@pytest.mark.parametrize('username, password, expects_credentials', [
    ('u', 'p', True),
    ('u', '', True),
    ('', 'p', True),
    ('', '', False),
])
def test_the_mailer_carries_credentials_only_when_one_is_set(
        restores_the_shared_logger, username, password, expects_credentials):
    """`:334`'s `or`, all four combinations. The two half-set rows are the ones
    that matter: `or` means a username with no password still produces a
    credentials tuple, which is what an operator part-way through configuring
    SMTP would get.
    """
    built = create_app(_config(MAIL_SERVER='smtp.example', ERRORS_TO='e@example.com',
                               MAIL_USERNAME=username, MAIL_PASSWORD=password))

    handler = _mail_handlers(built)[0]
    if expects_credentials:
        assert handler.username == username
        assert handler.password == password
    else:
        assert handler.username is None


@pytest.mark.parametrize('use_tls, expected', [
    (True, ()),
    (False, None),
])
def test_the_mailer_uses_tls_only_when_asked_to(restores_the_shared_logger, use_tls,
                                                expected):
    """`:337-339`. `secure=()` is smtplib's "STARTTLS with no key material",
    and `None` is "do not". The empty tuple is easy to read as "nothing set",
    which is why this row asserts the difference rather than truthiness.
    """
    built = create_app(_config(MAIL_SERVER='smtp.example', ERRORS_TO='e@example.com',
                               MAIL_USE_TLS=use_tls))

    assert _mail_handlers(built)[0].secure == expected


def test_the_mailer_reports_only_errors(restores_the_shared_logger):
    """`:345`. At any lower level a busy instance would mail its operator on
    every warning.
    """
    built = create_app(_config(MAIL_SERVER='smtp.example', ERRORS_TO='e@example.com',
                               MAIL_PORT=587, MAIL_FROM='from@example.com'))

    handler = _mail_handlers(built)[0]
    assert handler.level == logging.ERROR
    assert handler.mailhost == 'smtp.example'
    assert handler.mailport == 587
    # SMTPHandler wraps a string toaddrs in a list of one. R4: that means a
    # comma-separated ERRORS_TO becomes a SINGLE malformed recipient rather
    # than several addresses, and nothing complains until mail is first sent.
    assert handler.toaddrs == ['e@example.com']


def test_several_error_recipients_become_one_malformed_address(
        restores_the_shared_logger):
    """R4, pinned as the behaviour it is rather than repaired.

    ERRORS_TO is handed to SMTPHandler unsplit. SMTPHandler wraps a str in a
    one-element list, so 'a@x.example,b@x.example' is one recipient containing
    a comma -- which no SMTP server will accept -- and the failure appears only
    when an error is first reported, which is exactly when the operator is not
    watching.
    """
    built = create_app(_config(MAIL_SERVER='smtp.example',
                               ERRORS_TO='a@x.example,b@x.example'))

    assert _mail_handlers(built)[0].toaddrs == ['a@x.example,b@x.example']


# --------------------------------------------------------------------------
# The fixture that keeps the rows above from poisoning the session
# --------------------------------------------------------------------------


def test_the_shared_logger_is_left_as_it_was_found(restores_the_shared_logger):
    """The fixture's own proof, and the reason it exists.

    Flask.logger is logging.getLogger(app.name) and every app here is named
    'app', so the handler list is process-wide. An SMTPHandler left on it would
    try to reach smtp.example the next time anything in the suite logged an
    error -- MAIL_SUPPRESS_SEND does not apply, because the handler is a
    smtplib client rather than Flask-Mail.
    """
    from logging.handlers import SMTPHandler

    logger = logging.getLogger('app')
    before = list(logger.handlers)

    create_app(_config(MAIL_SERVER='smtp.example', ERRORS_TO='e@example.com'))

    assert any(isinstance(h, SMTPHandler) for h in logger.handlers)
    assert logger is logging.getLogger('app')
    # The fixture removes it at teardown; the next row asserts the outcome.
    assert len(logger.handlers) > len(before)


def test_no_mail_handler_survives_the_previous_row(restores_the_shared_logger):
    """Ordered after the row above deliberately: it is the assertion that the
    teardown actually ran.
    """
    from logging.handlers import SMTPHandler

    assert [h for h in logging.getLogger('app').handlers
            if isinstance(h, SMTPHandler)] == []


def test_a_site_with_no_language_list_still_answers_a_locale(app, db_session):
    """`:44-45`, the INNER handler -- distinct from the outer one two rows
    above, and reachable only when the lookup itself fails rather than when
    there is no request.

    `current_app.config['LANGUAGES']` is a plain subscript, so an instance
    whose config omits the key raises KeyError inside the `else` arm. Every
    caller of get_locale is on a request path, so answering 'en' rather than
    propagating is what keeps a misconfigured instance serving pages.
    """
    _seed()
    with app.test_request_context('/', headers={'Accept-Language': 'fr'}):
        languages = app.config.pop('LANGUAGES')
        try:
            assert get_locale() == 'en'
        finally:
            app.config['LANGUAGES'] = languages

    # The key is back: a leak here would silently change every later row's
    # answer, because the `app` fixture is session-scoped.
    assert 'LANGUAGES' in app.config


def test_the_log_directory_is_created_when_it_is_missing(restores_the_shared_logger):
    """`:349-350`. The directory already exists in the container, so the branch
    is unreachable without saying otherwise. os.mkdir is patched rather than
    allowed to run: the path is relative to the process's working directory
    (R2), and a test that really created it would leave a directory behind
    wherever pytest happened to be started from.
    """
    real_exists = os.path.exists

    def missing_logs(path):
        return False if path == 'logs' else real_exists(path)

    with patch('app.os.path.exists', side_effect=missing_logs):
        with patch('app.os.mkdir') as mkdir:
            create_app(_config())

    assert mkdir.call_args_list and mkdir.call_args_list[0].args[0] == 'logs'


def test_the_log_directory_is_left_alone_when_it_exists(restores_the_shared_logger):
    """The other arm, which is what every other row in this file exercises
    implicitly -- asserted once, so a mutant inverting the guard has somewhere
    to die.
    """
    with patch('app.os.mkdir') as mkdir:
        create_app(_config())

    assert mkdir.call_args_list == []
