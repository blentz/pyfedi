"""Interop D24, decision 4: the Podcast Index key and secret are write-only site settings, and the
discovery page names every data source."""
import pytest

from app.discovery.castopod import SETTING_KEY, SETTING_SECRET
from app.utils import get_setting, set_setting
from tests.discovery_fixtures import admin, fresh_cache  # noqa: F401
from tests.factories import make_instance, make_user
from tests.test_admin_federation import csrf, login

pytestmark = [pytest.mark.usefixtures('site', 'fresh_cache')]

PAGE = '/admin/federation/discovery'


def save(client, token, key='', secret='', button='podcastindex_save'):
    return client.post(PAGE, data={'podcastindex_api_key': key, 'podcastindex_api_secret': secret,
                                   button: 'go', 'csrf_token': token})


def test_saved_credentials_are_stored_and_never_rendered_back(admin):
    client, token = admin

    assert save(client, token, 'KEYabc', 'SECRETxyz').status_code == 302
    page = client.get(PAGE).get_data(as_text=True)

    assert get_setting(SETTING_KEY) == 'KEYabc'
    assert get_setting(SETTING_SECRET) == 'SECRETxyz'
    assert 'KEYabc' not in page and 'SECRETxyz' not in page
    assert 'Status: configured' in page


def test_the_form_never_renders_a_value_even_after_a_failed_post(admin):
    client, token = admin
    set_setting(SETTING_KEY, 'STOREDkey')
    set_setting(SETTING_SECRET, 'STOREDsecret')

    page = client.post(PAGE, data={'podcastindex_api_key': 'K' * 200, 'podcastindex_api_secret': 'TYPEDsecret',
                                   'podcastindex_save': 'go', 'csrf_token': token}).get_data(as_text=True)

    assert 'STOREDkey' not in page and 'STOREDsecret' not in page and 'TYPEDsecret' not in page
    assert get_setting(SETTING_KEY) == 'STOREDkey'


def test_blank_fields_keep_what_is_stored_and_padding_is_trimmed(admin):
    """Review focus 4."""
    client, token = admin
    save(client, token, '  KEYabc  ', ' SECRETxyz ')

    save(client, token, '', '')

    assert get_setting(SETTING_KEY) == 'KEYabc'
    assert get_setting(SETTING_SECRET) == 'SECRETxyz'


def test_remove_clears_both(admin):
    client, token = admin
    save(client, token, 'KEYabc', 'SECRETxyz')

    save(client, token, button='podcastindex_remove')

    assert not get_setting(SETTING_KEY) and not get_setting(SETTING_SECRET)
    assert 'Status: not set' in client.get(PAGE).get_data(as_text=True)


def test_the_page_names_every_data_source(admin):
    client, _token = admin

    page = client.get(PAGE).get_data(as_text=True)

    for source in ('https://sepiasearch.org/', 'https://podcastindex.org/', 'https://joinmastodon.org/',
                   'https://fedidb.org/'):
        assert source in page


def test_the_preload_page_links_here(admin):
    client, _token = admin

    assert PAGE in client.get('/admin/federation/preload').get_data(as_text=True)


def test_the_federation_page_links_here(admin):
    client, _ = admin
    page = client.get('/admin/federation').get_data(as_text=True)

    assert f'<a href="{PAGE}">Discover PeerTube channels and Castopod podcasts</a>' in page


def test_the_admin_nav_links_here(admin):
    client, _ = admin
    page = client.get('/admin/instances').get_data(as_text=True)

    assert f'<a href="{PAGE}">Discovery</a>' in page


def test_an_admin_gets_the_page(admin):
    client, _token = admin

    assert client.get(PAGE).status_code == 200


def test_someone_without_the_permission_is_refused_and_nothing_is_stored(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    ordinary = make_user(instance, 'ordinary', local=True)
    client = app.test_client()
    login(client, ordinary)

    refused = client.get(PAGE)
    assert refused.status_code == 302 and refused.headers['Location'].endswith('/auth/permission_denied')
    set_setting(SETTING_KEY, 'unchanged')
    posted = save(client, csrf(app, client), 'stolen', 'stolen')
    assert posted.status_code == 302 and posted.headers['Location'].endswith('/auth/permission_denied')
    assert get_setting(SETTING_KEY) == 'unchanged'
    assert get_setting(SETTING_SECRET, '') != 'stolen'
