"""Provider credentials are encrypted in the database (M1), under a key that lives only in the environment.

PAYMENT_SECRETS_KEY is one or more Fernet keys, newest first and comma separated, so a key can be rotated: the newest
seals, and any of them opens. A missing or malformed key means integrated providers are off and the site keeps running
(C4). The keyring itself is app.sealing's, shared with connected accounts.
"""
from app.sealing import SealUnavailable, generate_key, keyring, seal_with, unseal_with

CredentialsUnavailable = SealUnavailable   # the payments name for it, kept for its callers

KEY = 'PAYMENT_SECRETS_KEY'

__all__ = ['CredentialsUnavailable', 'credentials_available', 'generate_key', 'seal', 'unseal']


def credentials_available() -> bool:
    return keyring(KEY) is not None


def seal(values: dict) -> str:
    return seal_with(KEY, values)


def unseal(token: str) -> dict:
    return unseal_with(KEY, token)
