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
import time
from email.message import EmailMessage
from email.utils import localtime, make_msgid, parseaddr
from logging.handlers import SMTPHandler


def message_id(sender: str, fallback_domain: str) -> str:
    """A fresh Message-ID on the sender's own domain, `fallback_domain` when the sender has none."""
    address = parseaddr(sender or '')[1]
    domain = address.rpartition('@')[2] if '@' in address else fallback_domain
    return make_msgid(domain=domain)


def tls_context() -> ssl.SSLContext:
    """A context that checks the server's certificate chain and host name."""
    return ssl.create_default_context()


class ErrorMailHandler(SMTPHandler):
    """Mails ERROR records to the operator, over the same transport as member mail.

    logging's SMTPHandler upgrades with an unverified context, upgrades only when it also logs in, and has no implicit
    TLS mode, so with MAIL_USE_TLS unset it sent the password in clear while member mail used SMTP_SSL.

    At most one report per `interval` seconds per process: the account that sends them also sends password resets
    and verifications, and an error storm mailing every record could spend its sending allowance. The records not
    mailed are still in the journal; the next report says how many there were.
    """

    def __init__(self, mailhost, fromaddr, toaddrs, subject, credentials=None, use_tls=True, timeout=5.0,
                 interval=300.0):
        super().__init__(mailhost, fromaddr, toaddrs, subject, credentials=credentials, secure=None, timeout=timeout)
        self.use_tls = use_tls
        self.interval = interval
        self.last_sent = None   # time.monotonic() of the last report mailed
        self.held_back = 0      # records not mailed since then

    def due(self) -> bool:
        """Whether this record may be mailed now. logging.Handler.handle holds self.lock around emit."""
        now = time.monotonic()
        if self.last_sent is not None and now - self.last_sent < self.interval:
            self.held_back += 1
            return False
        self.last_sent = now
        return True

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
        msg['Message-ID'] = message_id(self.fromaddr, self.mailhost)
        body = self.format(record)
        if self.held_back:
            body += (f'\n\n{self.held_back} more error(s) since the last report were not mailed (at most one report '
                     f'per {self.interval:g}s); see the journal.')
        msg.set_content(body)
        return msg

    def emit(self, record):
        if not self.due():
            return
        try:
            msg = self.message(record)
            self.held_back = 0
            server = self.connection()
            try:
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(msg)
            finally:
                server.quit()
        except Exception:
            self.handleError(record)
