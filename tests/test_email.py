"""Sending mail: what is composed, and down which of the two paths.

Sub-project 93 -- `app/email.py`. Four things the instance sends by email (a
password reset, an address verification, a registration approval and a topic
suggestion), one Celery task that sends them, and a small SMTP class under
it. An instance sends through either an SMTP server or Amazon SES, decided by
which of two config keys is set.

Nothing here talks to a real server: `smtplib.SMTP` and `boto3.client` are
patched, and what is asserted is the message that WOULD have gone out.

One thing removed rather than covered: `SMTPEmailService.set_cc_bcc` read
`self.msg.CC`, which is not an attribute of `email.message.Message` -- the
header is `self.msg['CC']` -- so any call to it was `AttributeError`. It also
ignored both of its arguments and appended lists, rather than addresses, to
`self.recipients`. Nothing called it (D1278).

Also measured and left alone: a subject or a recipient carrying a newline is
`HeaderParseError: header value appears to contain an embedded header` when
the message is serialised, so Python's own library refuses the injection
before it reaches the wire. The tests below pin that refusal.
"""
from email.errors import HeaderParseError
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from flask import current_app, g

from app import db
from app.email import (SMTPEmailService, send_async_email, send_email,
                       send_password_reset_email,
                       send_registration_approved_email,
                       send_topic_suggestion, send_verification_email)
from app.models import Site
from tests.factories import make_community


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.name = 'Probeland'
    g.site.contact_email = 'admin@probeland.test'
    db.session.commit()
    return SimpleNamespace(app=app, user=api_baseline.user2,
                           baseline=api_baseline)


@pytest.fixture
def smtp(monkeypatch):
    """An instance configured to send through an SMTP server."""
    for key, value in (('MAIL_SERVER', 'mail.probeland.test'),
                       ('MAIL_PORT', 587), ('MAIL_USERNAME', 'postmaster'),
                       ('MAIL_PASSWORD', 'secret'), ('MAIL_USE_TLS', True),
                       ('MAIL_FROM', 'noreply@probeland.test'),
                       ('AWS_REGION', None),
                       ('BOUNCE_ADDRESS', 'bounces@probeland.test')):
        monkeypatch.setitem(current_app.config, key, value)


@pytest.fixture
def ses(monkeypatch):
    """One configured to send through Amazon SES instead."""
    for key, value in (('MAIL_SERVER', None), ('AWS_REGION', 'eu-west-1'),
                       ('MAIL_FROM', 'noreply@probeland.test'),
                       ('BOUNCE_ADDRESS', 'bounces@probeland.test')):
        monkeypatch.setitem(current_app.config, key, value)


def a_service(use_tls=True):
    with patch('smtplib.SMTP') as plain, patch('smtplib.SMTP_SSL') as secure:
        service = SMTPEmailService('postmaster', 'secret',
                                   ('mail.probeland.test', 587),
                                   use_tls=use_tls)
    service.constructors = (plain, secure)
    return service


# --------------------------------------------------------------------------
# which path a message takes
# --------------------------------------------------------------------------

class TestWhereAMessageIsHandedOff:
    def test_in_debug_it_is_sent_here_and_now(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)
        with patch('app.email.send_async_email') as task:
            send_email('a subject', 'a@b.test', ['c@d.test'], 'text', '<p>x</p>')
        task.assert_called_once_with('a subject', 'a@b.test', ['c@d.test'],
                                     'text', '<p>x</p>', None)

    def test_otherwise_it_is_queued(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        with patch('app.email.send_async_email') as task:
            send_email('a subject', 'a@b.test', ['c@d.test'], 'text',
                       '<p>x</p>', reply_to='e@f.test')
        task.delay.assert_called_once_with('a subject', 'a@b.test',
                                           ['c@d.test'], 'text', '<p>x</p>',
                                           'e@f.test')


class TestSendingThroughAnSmtpServer:
    def sent(self, **kwargs):
        with patch('app.email.SMTPEmailService') as service:
            send_async_email(kwargs.pop('subject', 'a subject'),
                             kwargs.pop('sender', 'Probeland <a@b.test>'),
                             kwargs.pop('recipients', ['c@d.test']),
                             'text', '<p>x</p>',
                             kwargs.pop('reply_to', None))
        return service

    def test_the_server_it_is_told_to_use(self, env, smtp):
        service = self.sent()
        assert service.call_args.args[0] == 'postmaster'
        assert service.call_args.args[2] == ('mail.probeland.test', 587)
        assert service.call_args.kwargs == {'use_tls': True}

    def test_the_message_is_composed_and_sent(self, env, smtp):
        service = self.sent()
        mailer = service.return_value
        mailer.set_message.assert_called_once_with('text', 'a subject',
                                                   'Probeland <a@b.test>',
                                                   '<p>x</p>')
        mailer.set_recipients.assert_called_once_with(['c@d.test'])
        mailer.connect.assert_called_once()
        mailer.send_all.assert_called_once_with(close_connection=True)

    def test_a_reply_to_address_when_one_is_given(self, env, smtp):
        service = self.sent(reply_to='admin@probeland.test')
        service.return_value.set_reply_to.assert_called_once_with(
            'admin@probeland.test')

    def test_and_none_when_it_is_not(self, env, smtp):
        service = self.sent()
        assert service.return_value.set_reply_to.call_count == 0

    def test_one_recipient_given_as_a_bare_string(self, env, smtp):
        """`send_topic_suggestion` passes `g.site.contact_email`, which is
        one address, not a list of them."""
        service = self.sent(recipients='admin@probeland.test')
        service.return_value.set_recipients.assert_called_once_with(
            ['admin@probeland.test'])


class TestSendingThroughAmazon:
    def sent(self, **kwargs):
        client = MagicMock()
        with patch('app.email.boto3.client', return_value=client) as factory:
            send_async_email(kwargs.pop('subject', 'a subject'),
                             kwargs.pop('sender', 'Probeland <a@b.test>'),
                             kwargs.pop('recipients', ['c@d.test']),
                             'text', '<p>x</p>',
                             kwargs.pop('reply_to', None))
        return factory, client

    def test_the_region_it_is_told_to_use(self, env, ses):
        factory, _ = self.sent()
        factory.assert_called_once_with('ses', region_name='eu-west-1')

    def test_what_is_sent(self, env, ses):
        _, client = self.sent()
        call = client.send_email.call_args.kwargs
        assert call['Destination'] == {'ToAddresses': ['c@d.test']}
        assert call['Message']['Subject']['Data'] == 'a subject'
        assert call['Message']['Body']['Text']['Data'] == 'text'
        assert call['Message']['Body']['Html']['Data'] == '<p>x</p>'
        assert call['Source'] == 'Probeland <a@b.test>'
        assert call['ReturnPath'] == 'bounces@probeland.test'
        assert 'ReplyToAddresses' not in call

    def test_a_reply_to_address_when_one_is_given(self, env, ses):
        _, client = self.sent(reply_to='admin@probeland.test')
        assert client.send_email.call_args.kwargs['ReplyToAddresses'] == \
            ['admin@probeland.test']

    def test_amazon_refusing_is_logged_and_reported(self, env, ses):
        error = ClientError({'Error': {'Code': 'MessageRejected',
                                       'Message': 'Email address is not verified'}},
                            'SendEmail')
        client = MagicMock()
        client.send_email.side_effect = error
        with patch('app.email.boto3.client', return_value=client):
            result = send_async_email('a subject', 'Probeland <a@b.test>',
                                      ['c@d.test'], 'text', '<p>x</p>', None)
        assert result == 'Email address is not verified'

    def test_a_local_development_sender_is_replaced(self, env, ses):
        """An ngrok tunnel's hostname is not a domain SES will send from."""
        _, client = self.sent(sender='PieFed <noreply@abc.ngrok.app>')
        assert client.send_email.call_args.kwargs['Source'] == \
            'PieFed <noreply@piefed.social>'
        assert client.send_email.call_args.kwargs['ReturnPath'] == \
            'bounces@piefed.social'


class TestAnInstanceThatHasConfiguredNeither:
    def test_nothing_is_sent_and_nothing_raises(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MAIL_SERVER', None)
        monkeypatch.setitem(current_app.config, 'AWS_REGION', None)
        monkeypatch.setitem(current_app.config, 'BOUNCE_ADDRESS', None)
        with patch('app.email.SMTPEmailService') as service, \
                patch('app.email.boto3.client') as amazon:
            assert send_async_email('a subject', 'a@b.test', ['c@d.test'],
                                    'text', '<p>x</p>', None) is None
        assert service.call_count == 0
        assert amazon.call_count == 0


# --------------------------------------------------------------------------
# the four things the instance sends
# --------------------------------------------------------------------------

class TestWhatIsSentToAnAccount:
    def test_a_password_reset(self, env):
        with patch('app.email.send_email') as send:
            send_password_reset_email(env.user)
        assert send.call_args.kwargs['recipients'] == [env.user.email]
        assert 'Reset Your Password' in send.call_args.args[0]
        assert send.call_args.kwargs['sender'].startswith('Probeland <')

    def test_a_password_reset_in_development_prints_the_link(self, env,
                                                             monkeypatch,
                                                             capsys):
        monkeypatch.setattr(current_app, 'debug', True)
        with env.app.test_request_context('/'):
            g.site = db.session.get(Site, 1)
            with patch('app.email.send_email'):
                send_password_reset_email(env.user)
        assert 'reset_password' in capsys.readouterr().out

    def test_an_address_verification(self, env):
        env.user.verification_token = 'a-token'
        db.session.commit()
        with patch('app.email.send_email') as send:
            send_verification_email(env.user)
        assert send.call_args.kwargs['recipients'] == [env.user.email]
        assert 'verify your email address' in send.call_args.args[0]

    def test_the_body_of_a_verification_names_the_account(self, env):
        env.user.verification_token = 'a-token'
        db.session.commit()
        with patch('app.email.send_email') as send:
            send_verification_email(env.user)
        assert env.user.user_name in send.call_args.kwargs['text_body']


class TestWelcomingAnApprovedApplicant:
    def test_the_instance_s_own_words_are_used_when_it_has_set_any(self, env):
        from app.utils import set_setting
        set_setting('registration_approved_email',
                    'Welcome aboard, **enjoy yourself**.')
        with patch('app.email.send_email') as send:
            send_registration_approved_email(env.user)
        assert 'enjoy yourself' in send.call_args.kwargs['html_body']

    def test_a_default_is_used_when_it_has_not(self, env):
        with patch('app.email.send_email') as send:
            send_registration_approved_email(env.user)
        assert 'has been approved' in send.call_args.kwargs['html_body']

    def test_the_plain_text_version_is_derived_from_the_html(self, env):
        with patch('app.email.send_email') as send:
            send_registration_approved_email(env.user)
        assert '<' not in send.call_args.kwargs['text_body']

    def test_replies_go_to_the_administrators(self, env):
        with patch('app.email.send_email') as send:
            send_registration_approved_email(env.user)
        assert send.call_args.kwargs['reply_to'] == 'admin@probeland.test'

    def test_an_instance_with_no_from_address_sends_as_its_contact(
            self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'MAIL_FROM', '')
        with patch('app.email.send_email') as send:
            send_registration_approved_email(env.user)
        assert send.call_args.kwargs['sender'] == \
            'Probeland <admin@probeland.test>'


class TestSuggestingATopic:
    def test_it_goes_to_the_administrators(self, env):
        community = make_community('probeland')
        db.session.commit()
        with patch('app.email.send_email') as send:
            send_topic_suggestion([community], env.user,
                                  'admin@probeland.test',
                                  'New topic suggestion', 'Gardening')
        assert send.call_args.kwargs['recipients'] == 'admin@probeland.test'
        assert 'Gardening' in send.call_args.kwargs['text_body']
        assert env.user.user_name in send.call_args.kwargs['text_body']


# --------------------------------------------------------------------------
# the SMTP class
# --------------------------------------------------------------------------

class TestConnectingToTheServer:
    def test_tls_opens_a_plain_connection_and_upgrades_it(self, env):
        service = a_service(use_tls=True)
        plain, secure = service.constructors
        plain.assert_called_once_with('mail.probeland.test', 587)
        assert secure.call_count == 0
        service.connect()
        service.smtpserver.starttls.assert_called_once()
        assert service.connected is True

    def test_without_tls_it_opens_an_ssl_connection(self, env):
        service = a_service(use_tls=False)
        plain, secure = service.constructors
        secure.assert_called_once_with('mail.probeland.test', 587)
        assert plain.call_count == 0
        service.connect()
        assert service.smtpserver.starttls.call_count == 0

    def test_it_logs_in_when_it_has_credentials(self, env):
        service = a_service()
        service.connect()
        service.smtpserver.login.assert_called_once_with('postmaster',
                                                         'secret')

    def test_and_does_not_when_it_has_none(self, env):
        with patch('smtplib.SMTP'):
            service = SMTPEmailService('', '', ('mail.probeland.test', 587),
                                       use_tls=True)
        service.connect()
        assert service.smtpserver.login.call_count == 0

    def test_disconnecting(self, env):
        service = a_service()
        service.connect()
        service.disconnect()
        service.smtpserver.close.assert_called_once()
        assert service.connected is False

    def test_what_it_says_about_itself(self, env):
        service = a_service()
        described = str(service)
        assert 'mail.probeland.test' in described
        assert 'Connected: False' in described


class TestComposingTheMessage:
    def test_a_message_with_both_a_text_and_an_html_version(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test', '<p>x</p>')
        assert service.msg.is_multipart()
        assert service.msg['Subject'] == 'a subject'
        assert service.msg['From'] == 'a@b.test'
        assert service.msg['Message-ID'].endswith('@mail.probeland.test>')
        assert service.msg['Date']

    def test_a_message_with_only_text(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        assert not service.msg.is_multipart()
        assert service.msg.get_payload() == 'the text'

    def test_a_message_with_no_sender_of_its_own_is_from_the_login(self, env):
        service = a_service()
        service.set_message('the text', 'a subject')
        assert service.msg['From'] == 'postmaster'

    def test_the_subject_can_be_replaced(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_subject('a different subject')
        assert service.msg['Subject'] == 'a different subject'

    def test_the_sender_can_be_replaced(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_from('c@d.test')
        assert service.msg['From'] == 'c@d.test'

    def test_a_reply_to_address_can_be_added(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_reply_to('e@f.test')
        assert service.msg['Reply-To'] == 'e@f.test'

    def test_the_body_can_be_emptied(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.clear_message()
        assert service.msg.get_payload() == ''

    def test_the_text_of_a_plain_message_can_be_replaced(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_plaintext('different text')
        assert service.msg.get_payload() == 'different text'

    def test_the_text_of_a_multipart_message_can_be_replaced(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test', '<p>x</p>')
        service.set_plaintext('different text')
        assert 'different text' in service.msg.get_payload()[0].as_string()

    def test_the_html_of_a_multipart_message_can_be_replaced(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test', '<p>x</p>')
        service.set_html('<p>different</p>')
        assert '<p>different</p>' in service.msg.get_payload()[1].as_string()

    def test_html_cannot_be_set_on_a_message_that_has_none(self, env,
                                                           capsys):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        with pytest.raises(TypeError):
            service.set_html('<p>x</p>')
        assert 'Payload is not a list' in capsys.readouterr().out


class TestWhoTheMessageGoesTo:
    def test_a_list_of_recipients(self, env):
        service = a_service()
        service.set_recipients(['a@b.test', 'c@d.test'])
        assert service.recipients == ['a@b.test', 'c@d.test']

    def test_a_tuple_is_accepted_too(self, env):
        service = a_service()
        service.set_recipients(('a@b.test',))
        assert service.recipients == ('a@b.test',)

    def test_one_bare_address_is_refused(self, env):
        """A string is iterable, so accepting one would send a message per
        character."""
        service = a_service()
        with pytest.raises(TypeError, match='must be a list or tuple'):
            service.set_recipients('a@b.test')

    def test_one_more_can_be_added(self, env):
        service = a_service()
        service.set_recipients(['a@b.test'])
        service.add_recipient('c@d.test')
        assert service.recipients == ['a@b.test', 'c@d.test']


class TestSendingIt:
    def test_each_recipient_is_sent_their_own_copy(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_recipients(['a@b.test', 'c@d.test'])
        service.connect()
        sent = []
        service.smtpserver.send_message = lambda msg: sent.append(msg['To'])
        service.send_all(close_connection=False)
        assert sent == ['a@b.test', 'c@d.test']

    def test_nobody_is_named_in_anybody_else_s_copy(self, env):
        """Each send replaces the To header rather than adding to it, so one
        recipient never learns of another."""
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_recipients(['a@b.test', 'c@d.test'])
        service.connect()
        sent = []
        service.smtpserver.send_message = lambda msg: sent.append(
            msg.as_string())
        service.send_all(close_connection=False)
        assert 'c@d.test' not in sent[0]

    def test_the_connection_is_closed_afterwards_if_asked(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_recipients(['a@b.test'])
        service.connect()
        service.send_all(close_connection=True)
        assert service.connected is False

    def test_and_left_open_if_not(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_recipients(['a@b.test'])
        service.connect()
        service.send_all(close_connection=False)
        assert service.connected is True

    def test_sending_before_connecting_is_refused(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_recipients(['a@b.test'])
        with pytest.raises(ConnectionError, match='Not connected'):
            service.send_all()


class TestHeadersThatCarryANewline:
    """Python's own library refuses these when the message is serialised, so
    an injected header never reaches the wire."""

    def test_a_subject(self, env):
        service = a_service()
        service.set_message('the text', 'Hello\nBcc: victim@example.test',
                            'a@b.test')
        service.set_recipients(['a@b.test'])
        service.connect()
        service.smtpserver.send_message = lambda msg: msg.as_string()
        with pytest.raises(HeaderParseError):
            service.send_all(close_connection=False)

    def test_a_recipient(self, env):
        service = a_service()
        service.set_message('the text', 'a subject', 'a@b.test')
        service.set_recipients(['a@b.test\nBcc: victim@example.test'])
        service.connect()
        service.smtpserver.send_message = lambda msg: msg.as_string()
        with pytest.raises(HeaderParseError):
            service.send_all(close_connection=False)
