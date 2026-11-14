"""Secrets sealed in the database under Fernet keys that live only in the environment.

A keyring is named by its config key, whose value is one or more Fernet keys, newest first and comma separated, so a
key can be rotated: the newest seals, and any of them opens. A missing or malformed key means the keyring is off.
Nothing here logs a value or puts one in an exception, so a report of a failure cannot carry a secret.
"""
import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from flask import current_app


class SealUnavailable(Exception):
    """There is no valid key, or the key does not open this value."""


def keyring(config_name: str) -> MultiFernet | None:
    configured = current_app.config.get(config_name) or ''
    keys = [key.strip() for key in configured.split(',') if key.strip()]
    if not keys:
        return None
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except ValueError:
        return None


def seal_with(config_name: str, values: dict) -> str:
    ring = keyring(config_name)
    if ring is None:
        raise SealUnavailable()
    return ring.encrypt(json.dumps(values).encode()).decode()


def unseal_with(config_name: str, token: str) -> dict:
    ring = keyring(config_name)
    if ring is None:
        raise SealUnavailable()
    try:
        return json.loads(ring.decrypt(token.encode()))
    except InvalidToken:
        raise SealUnavailable() from None


def generate_key() -> str:
    return Fernet.generate_key().decode()
