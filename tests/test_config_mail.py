"""config.py's reading of MAIL_USE_TLS.

It used to be "is the variable set at all", so MAIL_USE_TLS=False meant STARTTLS. A value that reads as false now
means implicit TLS (SMTP_SSL), the same as leaving the variable out.
"""
import importlib

import pytest

import config


@pytest.fixture
def reload_config(monkeypatch):
    def load(value):
        if value is None:
            monkeypatch.delenv('MAIL_USE_TLS', raising=False)
        else:
            monkeypatch.setenv('MAIL_USE_TLS', value)
        return importlib.reload(config).Config.MAIL_USE_TLS
    yield load
    monkeypatch.undo()
    importlib.reload(config)


@pytest.mark.parametrize('value', ['True', 'true', '1', 'yes', 'on', 'TRUE'])
def test_a_true_value_means_starttls(reload_config, value):
    assert reload_config(value) is True


@pytest.mark.parametrize('value', [None, '', 'False', 'false', '0', 'no', 'off', ' False '])
def test_unset_or_a_false_value_means_implicit_tls(reload_config, value):
    assert reload_config(value) is False
