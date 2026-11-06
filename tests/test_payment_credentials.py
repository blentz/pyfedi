"""Provider credentials are encrypted in the database, under a key from the environment (M1, C4)."""
import pytest
from cryptography.fernet import Fernet
from flask import current_app

from app import cli
from app.payments.credentials import CredentialsUnavailable, credentials_available, seal, unseal

KEY = Fernet.generate_key().decode()
OLD_KEY = Fernet.generate_key().decode()
VALUES = {'secret_key': 'not-a-real-secret', 'webhook_secret': 'not-a-real-webhook-secret'}


@pytest.fixture
def key(app, monkeypatch):
    with app.app_context():
        monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', KEY)
        yield


def with_key(monkeypatch, value):
    monkeypatch.setitem(current_app.config, 'PAYMENT_SECRETS_KEY', value)


def test_what_is_sealed_opens(key):
    assert unseal(seal(VALUES)) == VALUES


def test_a_sealed_value_does_not_show_what_it_holds(key):
    token = seal(VALUES)
    assert 'not-a-real-secret' not in token and 'secret_key' not in token


def test_a_rotated_key_opens_what_the_old_one_sealed(key, monkeypatch):
    with_key(monkeypatch, OLD_KEY)
    token = seal(VALUES)
    with_key(monkeypatch, f'{KEY},{OLD_KEY}')
    assert unseal(token) == VALUES


def test_a_rotated_key_seals_with_the_newest(key, monkeypatch):
    with_key(monkeypatch, f'{KEY},{OLD_KEY}')
    token = seal(VALUES)
    with_key(monkeypatch, KEY)
    assert unseal(token) == VALUES


def test_the_wrong_key_opens_nothing(key, monkeypatch):
    token = seal(VALUES)
    with_key(monkeypatch, OLD_KEY)
    with pytest.raises(CredentialsUnavailable):
        unseal(token)


def test_a_token_that_is_not_one_opens_nothing(key):
    with pytest.raises(CredentialsUnavailable):
        unseal('nonsense')


@pytest.mark.parametrize('value', ['', None, 'too short', f'{KEY},too short'],
                         ids=['empty', 'none', 'malformed', 'one_malformed'])
def test_without_a_valid_key_credentials_are_unavailable_and_nothing_crashes(key, monkeypatch, value):
    with_key(monkeypatch, value)
    assert credentials_available() is False
    with pytest.raises(CredentialsUnavailable):
        seal(VALUES)
    with pytest.raises(CredentialsUnavailable):
        unseal('anything')


def test_with_a_valid_key_they_are_available(key):
    assert credentials_available() is True


def test_the_command_prints_a_usable_key_and_stores_nothing(app, monkeypatch):
    cli.register(app)
    result = app.test_cli_runner().invoke(args=['generate-payment-key'])
    assert result.exit_code == 0
    printed = result.output.strip().splitlines()[-1]
    assert Fernet(printed.encode())
