"""Provider credentials are encrypted in the database (M1), under a key that lives only in the environment.

PAYMENT_SECRETS_KEY is one or more Fernet keys, newest first and comma separated, so a key can be rotated: the newest
seals, and any of them opens. A missing or malformed key means integrated providers are off and the site keeps running
(C4). Nothing here logs a value or puts one in an exception, so a report of the failure cannot carry a secret.
"""
import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from flask import current_app


class CredentialsUnavailable(Exception):
    """There is no valid key, or the key does not open this value."""


def _keyring() -> MultiFernet | None:
    configured = current_app.config.get('PAYMENT_SECRETS_KEY') or ''
    keys = [key.strip() for key in configured.split(',') if key.strip()]
    if not keys:
        return None
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except ValueError:
        return None


def credentials_available() -> bool:
    return _keyring() is not None


def seal(values: dict) -> str:
    keyring = _keyring()
    if keyring is None:
        raise CredentialsUnavailable()
    return keyring.encrypt(json.dumps(values).encode()).decode()


def unseal(token: str) -> dict:
    keyring = _keyring()
    if keyring is None:
        raise CredentialsUnavailable()
    try:
        return json.loads(keyring.decrypt(token.encode()))
    except InvalidToken:
        raise CredentialsUnavailable() from None


def generate_key() -> str:
    return Fernet.generate_key().decode()
