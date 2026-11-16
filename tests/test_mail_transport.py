"""The error mailer's transport (app/mail_transport.py).

smtplib is patched: what is asserted is how the connection would be opened and secured, and what would be sent.
"""
import logging
import ssl
from unittest.mock import patch

import pytest

from app.mail_transport import ErrorMailHandler, tls_context


def a_record(msg='it broke'):
    return logging.LogRecord('app', logging.ERROR, __file__, 1, msg, None, None)


def a_handler(**overrides):
    kwargs = dict(mailhost=('mail.probeland.test', 587), fromaddr='daemon@probeland.test',
                  toaddrs=['ops@probeland.test'], subject='PieFed error',
                  credentials=('daemon@probeland.test', 'secret'), use_tls=True)
    kwargs.update(overrides)
    return ErrorMailHandler(**kwargs)


def assert_verifying(context):
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_the_shared_context_checks_certificate_and_host_name():
    assert_verifying(tls_context())


class TestSecuringTheConnection:
    def test_starttls_checks_the_server_certificate(self):
        with patch('smtplib.SMTP') as plain, patch('smtplib.SMTP_SSL') as implicit:
            a_handler().emit(a_record())
        plain.assert_called_once_with('mail.probeland.test', 587, timeout=5.0)
        assert implicit.call_count == 0
        assert_verifying(plain.return_value.starttls.call_args.kwargs['context'])

    def test_it_upgrades_even_when_it_does_not_log_in(self):
        """logging's own handler upgraded only when it also had credentials."""
        with patch('smtplib.SMTP') as plain:
            a_handler(credentials=None).emit(a_record())
        server = plain.return_value
        assert_verifying(server.starttls.call_args.kwargs['context'])
        assert server.login.call_count == 0
        server.send_message.assert_called_once()

    def test_without_starttls_it_uses_implicit_tls_like_member_mail(self):
        """logging's own handler sent in clear here, credentials included."""
        with patch('smtplib.SMTP') as plain, patch('smtplib.SMTP_SSL') as implicit:
            a_handler(use_tls=False, mailhost=('mail.probeland.test', 465)).emit(a_record())
        assert plain.call_count == 0
        assert implicit.call_args.args == ('mail.probeland.test', 465)
        assert implicit.call_args.kwargs['timeout'] == 5.0
        assert_verifying(implicit.call_args.kwargs['context'])
        implicit.return_value.login.assert_called_once_with('daemon@probeland.test', 'secret')

    @pytest.mark.parametrize('use_tls, port', [(True, 25), (False, 465)])
    def test_a_host_with_no_port_gets_the_transport_s_default(self, use_tls, port):
        with patch('smtplib.SMTP') as plain, patch('smtplib.SMTP_SSL') as implicit:
            a_handler(use_tls=use_tls, mailhost='mail.probeland.test').emit(a_record())
        opened = plain if use_tls else implicit
        assert opened.call_args.args == ('mail.probeland.test', port)


class TestWhatIsSent:
    def test_the_report(self):
        with patch('smtplib.SMTP') as plain:
            a_handler().emit(a_record('the database went away'))
        server = plain.return_value
        server.login.assert_called_once_with('daemon@probeland.test', 'secret')
        msg = server.send_message.call_args.args[0]
        assert msg['From'] == 'daemon@probeland.test'
        assert msg['To'] == 'ops@probeland.test'
        assert msg['Subject'] == 'PieFed error'
        assert msg['Date']
        assert msg['Message-ID'].endswith('@probeland.test>')
        assert 'the database went away' in msg.get_content()
        server.quit.assert_called_once()

    def test_a_failure_is_handed_to_logging_and_the_connection_closed(self):
        with patch('smtplib.SMTP') as plain, patch.object(ErrorMailHandler, 'handleError') as handle:
            plain.return_value.send_message.side_effect = OSError('refused')
            record = a_record()
            a_handler().emit(record)
        handle.assert_called_once_with(record)
        plain.return_value.quit.assert_called_once()

    def test_a_refused_connection_is_handed_to_logging(self):
        with patch('smtplib.SMTP', side_effect=OSError('unreachable')), \
                patch.object(ErrorMailHandler, 'handleError') as handle:
            record = a_record()
            a_handler().emit(record)
        handle.assert_called_once_with(record)


class TestHowOftenItMails:
    """The account that mails error reports also sends password resets; a storm of errors must not spend it."""

    def test_one_report_per_interval_and_the_next_counts_the_rest(self):
        handler = a_handler(interval=300)
        with patch('smtplib.SMTP') as plain, patch('app.mail_transport.time.monotonic') as clock:
            clock.return_value = 1000.0
            handler.emit(a_record('first'))
            clock.return_value = 1100.0
            handler.emit(a_record('second'))
            handler.emit(a_record('third'))
            clock.return_value = 1300.0
            handler.emit(a_record('fourth'))
        sent = [call.args[0].get_content() for call in plain.return_value.send_message.call_args_list]
        assert len(sent) == 2
        assert 'first' in sent[0] and 'not mailed' not in sent[0]
        assert 'fourth' in sent[1]
        assert '2 more error(s) since the last report were not mailed' in sent[1]

    def test_the_count_starts_again_after_a_report(self):
        handler = a_handler(interval=300)
        with patch('smtplib.SMTP') as plain, patch('app.mail_transport.time.monotonic') as clock:
            for at, msg in ((0.0, 'a'), (10.0, 'b'), (400.0, 'c'), (800.0, 'd')):
                clock.return_value = at
                handler.emit(a_record(msg))
        sent = [call.args[0].get_content() for call in plain.return_value.send_message.call_args_list]
        assert len(sent) == 3
        assert '1 more error(s)' in sent[1]
        assert 'not mailed' not in sent[2]

    def test_a_report_that_fails_to_send_still_spends_the_interval(self):
        """A mail server that refuses is not retried on every following error."""
        handler = a_handler(interval=300)
        with patch('smtplib.SMTP', side_effect=OSError('unreachable')) as plain, \
                patch.object(ErrorMailHandler, 'handleError'), \
                patch('app.mail_transport.time.monotonic', return_value=5.0):
            handler.emit(a_record())
            handler.emit(a_record())
        assert plain.call_count == 1
