"""Sub-project 84 slice P probes for app/api/alpha/utils/site.py.

MEASURES behaviour; asserts nothing. Deleted once the findings are recorded.
"""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.api.alpha.utils.site import (get_federated_instances, get_site,
                                      get_site_instance_chooser,
                                      get_site_instance_chooser_search,
                                      get_site_metadata, get_site_version,
                                      post_site_block)
from app.models import InstanceChooser, Language, Site
from tests.factories import make_instance

MISSING = 999999


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def a_chooser(domain='elsewhere.test', **data):
    payload = {
        'elevator_pitch': 'a nice place',
        'registration_mode': 'Open',
        'language': {'name': 'English', 'code': 'en'},
    }
    payload.update(data)
    row = InstanceChooser(domain=domain, data=payload)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(reader=api_baseline.user3, baseline=api_baseline)


def show(label, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        print(f'PROBE {label}: {type(exc).__name__}: {str(exc)[:160]}')
    else:
        print(f'PROBE {label}: returned {str(result)[:160]}')


def test_probe_simple(env):
    show('ha the site, anonymously', get_site, None)
    show('hb the site, with an account', get_site, token(env.reader))
    show('hc the version', get_site_version, None)
    show('hd the federated instances', get_federated_instances, {})
    show('he the instance chooser', get_site_instance_chooser, None)


def test_probe_chooser_search(env):
    a_chooser()
    show('ia no filters', get_site_instance_chooser_search, {})
    show('ib a text filter', get_site_instance_chooser_search, {'q': 'nice'})
    show('ic nsfw yes', get_site_instance_chooser_search, {'nsfw': 'yes'})
    show('id nsfw no', get_site_instance_chooser_search, {'nsfw': 'no'})
    show('ie a language', get_site_instance_chooser_search, {'language': 'en'})
    show('if a language nobody has', get_site_instance_chooser_search,
         {'language': 'zz'})
    show('ig newbie yes', get_site_instance_chooser_search, {'newbie': 'yes'})
    show('ih newbie no', get_site_instance_chooser_search, {'newbie': 'no'})


def test_probe_chooser_data(env):
    row = a_chooser()
    get_site_instance_chooser_search({})
    db.session.commit()
    stored = db.session.get(InstanceChooser, row.id)
    print('PROBE ja stored data keys:', sorted(stored.data.keys()))
    print('PROBE jb stored language:', stored.data.get('language'))


def test_probe_chooser_missing_keys(env):
    row = InstanceChooser(domain='sparse.test', data={'elevator_pitch': 'x'})
    db.session.add(row)
    db.session.commit()
    show('ka a row with no registration_mode',
         get_site_instance_chooser_search, {})


def test_probe_chooser_no_language(env):
    row = InstanceChooser(domain='sparse.test',
                          data={'registration_mode': 'Open'})
    db.session.add(row)
    db.session.commit()
    show('kb a row with no language', get_site_instance_chooser_search, {})


def test_probe_metadata(env):
    show('la no url at all', get_site_metadata, None, {})
    with patch('app.api.alpha.utils.site.opengraph_parse', return_value=None):
        show('lb a url that answers nothing', get_site_metadata, None,
             {'url': 'https://example.test/x'})
    with patch('app.api.alpha.utils.site.opengraph_parse',
               return_value={'og:title': 't'}):
        show('lc a url with only a title', get_site_metadata, None,
             {'url': 'https://example.test/x'})


def test_probe_block(env):
    remote = env.baseline.instance_remote
    show('ma block an instance nobody holds', post_site_block,
         token(env.reader), {'instance_id': MISSING, 'block': True})
    show('mb block a real instance', post_site_block, token(env.reader),
         {'instance_id': remote.id, 'block': True})
    show('mc unblock it again', post_site_block, token(env.reader),
         {'instance_id': remote.id, 'block': False})
