"""Interop D24: the admin discovery page holds the channel/podcast pre-load and names every data source. Castopod
podcasts come from index.castopod.org, which needs no credentials, so the page asks for none."""
import pytest

from app.utils import get_setting, set_setting
from tests.discovery_fixtures import admin, fresh_cache  # noqa: F401
from tests.factories import make_instance, make_user
from tests.test_admin_federation import csrf, login

pytestmark = [pytest.mark.usefixtures('site', 'fresh_cache')]

PAGE = '/admin/federation/discovery'


def test_the_page_has_no_credentials_form_and_leaves_stored_settings_alone(admin):
    client, token = admin
    set_setting('podcastindex_api_key', 'STOREDkey')
    set_setting('podcastindex_api_secret', 'STOREDsecret')

    page = client.get(PAGE).get_data(as_text=True)
    client.post(PAGE, data={'podcastindex_api_key': 'NEWkey', 'podcastindex_api_secret': 'NEWsecret',
                            'podcastindex_save': 'go', 'podcastindex_remove': 'go', 'csrf_token': token})

    assert 'podcastindex_api_key' not in page and 'podcastindex_api_secret' not in page
    assert 'Podcast Index credentials' not in page and 'podcastindex_status' not in page
    assert 'STOREDkey' not in page and 'STOREDsecret' not in page
    assert (get_setting('podcastindex_api_key'), get_setting('podcastindex_api_secret')) == ('STOREDkey',
                                                                                             'STOREDsecret')


def test_the_page_names_every_data_source(admin):
    client, _token = admin

    page = client.get(PAGE).get_data(as_text=True)

    for source in ('https://sepiasearch.org/', 'https://index.castopod.org/', 'https://joinmastodon.org/',
                   'https://fedidb.org/'):
        assert source in page
    assert 'index.castopod.org</a> (Castopod)' in page
    assert 'podcastindex.org' not in page and 'Podcast Index' not in page


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


def test_the_admin_tab_bar_has_a_discovery_tab_after_federation(admin):
    client, _ = admin
    page = client.get('/admin/instances').get_data(as_text=True)

    federation = page.index('id="federation-tab"')
    discovery = page.index('id="discovery-tab"')
    assert federation < discovery < page.index('id="instances-tab"')
    tab = page[page.rindex('<a ', 0, discovery):page.index('</a>', discovery)]
    assert f'href="{PAGE}"' in tab and 'active' not in tab


def test_the_discovery_tab_is_the_active_one_on_this_page(admin):
    client, _ = admin
    page = client.get(PAGE).get_data(as_text=True)

    discovery = page.index('id="discovery-tab"')
    assert 'nav-link active' in page[page.rindex('<a ', 0, discovery):discovery]
    federation = page.index('id="federation-tab"')
    assert 'active' not in page[page.rindex('<a ', 0, federation):federation]


def test_an_admin_gets_the_page(admin):
    client, _token = admin

    assert client.get(PAGE).status_code == 200


def test_someone_without_the_permission_is_refused(app, db_session):
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    ordinary = make_user(instance, 'ordinary', local=True)
    client = app.test_client()
    login(client, ordinary)

    refused = client.get(PAGE)
    assert refused.status_code == 302 and refused.headers['Location'].endswith('/auth/permission_denied')
    posted = client.post(PAGE, data={'preload_count': 5, 'preload_platforms': ['peertube'],
                                     'preload_subscribe': 'go', 'csrf_token': csrf(app, client)})
    assert posted.status_code == 302 and posted.headers['Location'].endswith('/auth/permission_denied')
