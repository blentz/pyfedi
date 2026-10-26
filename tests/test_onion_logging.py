"""No log line or error email connects anyone to onion activity, and the app writes no log file (project 5, task 4;
spec goals 4, 9 and 10, and section 5, "Error emails for an onion request").

Clearnet output stays exactly as it was: the three fail2ban lines are byte-identical, and a clearnet error mail still
carries its message and its source lines.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler, SMTPHandler
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from redis.exceptions import RedisError

from app import create_app
from app.auth.util import sync_user_with_ldap
from app.log_scrub import ONION_EXCEPTION_MESSAGE, OnionScrubFilter
from app.onion_store import read_other_store
from app.utils import log_login_failure, log_probe_refused, log_register_attempt
from tests.conftest import TestConfig
from tests.test_login_failure_log import CLEARNET, clearnet_login, failures, only_failure
from tests.test_onion_guard_limits import (LOGIN, PASSWORD, a_session, alice, directory, ldap_off,  # noqa: F401
                                           login_form, onion, onion_context, onion_get, onion_post, proof_given,
                                           web_login)

SECRET = 'alice-secret-name'
ERROR_PATH = '/some/path?token=hunter2'


@pytest.fixture
def restores_the_shared_logger():
    logger = logging.getLogger('app')
    before = list(logger.handlers)
    yield
    for handler in list(logger.handlers):
        if handler not in before:
            logger.removeHandler(handler)
            handler.close()


@pytest.fixture
def mail_handler(app):
    """An SMTPHandler wired as create_app wires it, with emit replaced by a capture of the formatted text."""
    sent = []
    handler = SMTPHandler(mailhost=('localhost', 25), fromaddr='a@example.com', toaddrs=['b@example.com'],
                          subject='PieFed error')
    handler.setLevel(logging.ERROR)
    handler.addFilter(OnionScrubFilter())
    handler.emit = lambda record: sent.append(handler.format(record))
    app.logger.addHandler(handler)
    yield sent
    app.logger.removeHandler(handler)


def raise_in(app, context, message=f'no such user {SECRET}'):
    with context:
        try:
            raise ValueError(message)
        except ValueError:
            app.log_exception(sys.exc_info())


def test_onion_login_failure_writes_no_log_line(app, alice, ldap_off, caplog):
    client = app.test_client()
    a_session(client)
    web_login(client, 'alice', 'a wrong password')
    assert failures(caplog) == []
    assert [r for r in caplog.records if r.name == 'app'] == []     # nor a scrubbed stand-in that still says "onion"


def test_onion_register_and_probe_write_no_log_line(app, onion, caplog):
    client = app.test_client()
    a_session(client)
    onion_post(client, '/auth/register', data={})
    onion_get(client, '/.env')
    assert [r for r in caplog.records if r.name == 'app'] == []


def test_clearnet_fail2ban_lines_are_byte_identical(app, caplog):
    with app.test_request_context('/', environ_base=CLEARNET):
        log_login_failure()
        log_register_attempt()
        log_probe_refused()
    assert [r.getMessage() for r in caplog.records] == [
        'auth: login failed ip=203.0.113.9',
        'auth: register attempt ip=203.0.113.9',
        'scan: probe refused ip=203.0.113.9',
    ]


def test_onion_error_mail_has_no_path_host_query_user_or_message(app, onion, mail_handler):
    raise_in(app, onion_context(app, ERROR_PATH))
    assert len(mail_handler) == 1
    mail = mail_handler[0]
    for leaked in (SECRET, 'hunter2', 'some/path', 'abc.onion', 'no such user', 'raise ValueError'):
        assert leaked not in mail
    assert 'ValueError' in mail
    assert ONION_EXCEPTION_MESSAGE in mail
    assert 'test_onion_logging.py' in mail and 'in raise_in' in mail    # app frames stay: file, line, function
    assert 'Exception on /some' not in mail


def test_onion_error_goes_to_every_handler_scrubbed(app, onion, caplog):
    raise_in(app, onion_context(app, ERROR_PATH))
    text = caplog.text
    assert SECRET not in text and 'hunter2' not in text and 'abc.onion' not in text
    assert ONION_EXCEPTION_MESSAGE in text


def test_clearnet_error_mail_unchanged(app, mail_handler):
    raise_in(app, app.test_request_context(ERROR_PATH, environ_base=CLEARNET))
    mail = mail_handler[0]
    assert 'Exception on /some/path [GET]' in mail
    assert f'ValueError: no such user {SECRET}' in mail
    assert 'raise ValueError(message)' in mail


def test_an_onion_error_without_an_exception_is_generic(app, onion, mail_handler):
    with onion_context(app):
        app.logger.error('failed for %s', SECRET)
    assert mail_handler == ['error on an onion request']


def test_the_filter_is_idempotent_and_passes_clearnet_and_non_request_records(app, onion):
    scrub = OnionScrubFilter()
    record = logging.LogRecord('app', logging.ERROR, __file__, 1, 'plain %s', ('x',), None)
    assert scrub.filter(record) is True and (record.msg, record.args) == ('plain %s', ('x',))
    with app.test_request_context('/', environ_base=CLEARNET):
        assert scrub.filter(record) is True and record.msg == 'plain %s'
    with onion_context(app):
        assert scrub.filter(record) is True and record.msg == 'error on an onion request'
        assert scrub.filter(record) is True and record.msg == 'error on an onion request'


def test_onion_safe_records_pass_unchanged(app, onion):
    record = logging.LogRecord('app', logging.WARNING, __file__, 1, 'onion store unavailable', (), None)
    record.onion_safe = True
    with onion_context(app):
        assert OnionScrubFilter().filter(record) is True
    assert record.msg == 'onion store unavailable'


def test_an_unavailable_onion_store_line_names_no_user_ip_or_path(app, caplog):

    def broken(_client):
        raise RedisError('Error connecting to /run/redis/onion.sock for alice at 203.0.113.9')

    with app.test_request_context('/u/alice', environ_base=CLEARNET), \
            patch('app.onion_store.onion_configured', return_value=True):
        read_other_store(broken, 0)
    lines = [r.getMessage() for r in caplog.records if 'onion store' in r.getMessage()]
    assert lines and all('alice' not in line and '203.0.113.9' not in line and '/' not in line for line in lines)


def test_ldap_sync_failure_on_the_onion_names_no_user(app, onion, caplog):
    user = SimpleNamespace(user_name=SECRET, email='x@example.com')
    with patch('app.auth.util.sync_user_to_ldap', side_effect=RuntimeError(f'bind failed for {SECRET}')), \
            onion_context(app):
        sync_user_with_ldap(user, 'pw')
    assert SECRET not in caplog.text
    with patch('app.auth.util.sync_user_to_ldap', side_effect=RuntimeError('bind failed')), \
            app.test_request_context('/', environ_base=CLEARNET):
        sync_user_with_ldap(user, 'pw')
    assert f'LDAP sync failed for user {SECRET}: bind failed' in caplog.text


def test_no_file_log_handler(restores_the_shared_logger, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    create_app(type('OneOff', (TestConfig,), {}))
    assert not [h for h in logging.getLogger('app').handlers if isinstance(h, RotatingFileHandler)]
    assert not os.path.exists(tmp_path / 'logs')


def test_the_app_factory_attaches_the_scrub_to_the_mail_handler(restores_the_shared_logger):
    config = type('OneOff', (TestConfig,), {'MAIL_SERVER': 'localhost', 'MAIL_PORT': 25, 'ERRORS_TO': 'b@example.com',
                                            'MAIL_FROM': 'a@example.com'})
    create_app(config)
    mail = [h for h in logging.getLogger('app').handlers if isinstance(h, SMTPHandler)]
    assert mail and all(any(isinstance(f, OnionScrubFilter) for f in h.filters) for h in mail)
