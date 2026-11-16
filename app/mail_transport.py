"""How the instance reaches its outgoing mail server, for member mail (app/email.py) and for error reports.

Nothing here imports the app, so the app factory can build the error mailer before anything else loads.

MAIL_USE_TLS picks the transport for both: set, a plain connection upgraded with STARTTLS; unset, implicit TLS from the
first byte (SMTP_SSL). Either way the server's certificate and host name are checked against the system's trust
store. smtplib's own default for both is an unverified context, under which anyone on the path could pose as the mail
server and collect MAIL_PASSWORD.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import localtime
from logging.handlers import SMTPHandler


def tls_context() -> ssl.SSLContext:
    """A context that checks the server's certificate chain and host name."""
    return ssl.create_default_context()


class ErrorMailHandler(SMTPHandler):
    """Mails ERROR records to the operator, over the same transport as member mail.

    logging's SMTPHandler upgrades with an unverified context, upgrades only when it also logs in, and has no implicit
    TLS mode, so with MAIL_USE_TLS unset it sent the password in clear while member mail used SMTP_SSL.
    """

    def __init__(self, mailhost, fromaddr, toaddrs, subject, credentials=None, use_tls=True, timeout=5.0):
        super().__init__(mailhost, fromaddr, toaddrs, subject, credentials=credentials, secure=None, timeout=timeout)
        self.use_tls = use_tls

    def connection(self):
        port = self.mailport or (smtplib.SMTP_PORT if self.use_tls else smtplib.SMTP_SSL_PORT)
        if self.use_tls:
            server = smtplib.SMTP(self.mailhost, port, timeout=self.timeout)
            server.starttls(context=tls_context())
            return server
        return smtplib.SMTP_SSL(self.mailhost, port, timeout=self.timeout, context=tls_context())

    def message(self, record: logging.LogRecord) -> EmailMessage:
        msg = EmailMessage()
        msg['From'] = self.fromaddr
        msg['To'] = ','.join(self.toaddrs)
        msg['Subject'] = self.getSubject(record)
        msg['Date'] = localtime()
        msg.set_content(self.format(record))
        return msg

    def emit(self, record):
        try:
            server = self.connection()
            try:
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(self.message(record))
            finally:
                server.quit()
        except Exception:
            self.handleError(record)
