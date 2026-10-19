import pathlib
import runpy

import pytest

CONF = pathlib.Path(__file__).parent.parent / 'gunicorn.conf.py'


@pytest.mark.parametrize('value,expected', [(None, None), ('', None), ('0', None), ('false', None), ('no', None),
                                            ('off', None), ('2', None), (' 1 ', '-'),
                                            ('1', '-'), ('true', '-'), ('TRUE', '-'), ('yes', '-'), ('on', '-')])
def test_the_access_log_follows_the_flag(monkeypatch, value, expected):
    monkeypatch.delenv('ACCESS_LOG', raising=False)
    if value is not None:
        monkeypatch.setenv('ACCESS_LOG', value)
    assert runpy.run_path(str(CONF))['accesslog'] == expected


def test_the_error_log_stays_on(monkeypatch):
    monkeypatch.delenv('ACCESS_LOG', raising=False)
    assert runpy.run_path(str(CONF))['errorlog'] == '-'
