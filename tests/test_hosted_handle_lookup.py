"""A handle on a domain this site hosts is looked up in the database and never fetched (final fix F2).

`name@<onion>` used to reach WebFinger on the onion itself: the server sent itself a request for a row it holds,
and on a host without a Tor resolver the onion's name went out to the upstream DNS resolver. The same held for
`name@<SERVER_NAME>` users. Found or not, a hosted handle makes no request.
"""
import pytest

import app.community.util as community_util
import app.feed.util as feed_util
import app.user.utils as user_utils
from app.api.alpha.utils.misc import get_resolve_object
from app.community.util import search_for_community
from app.feed.util import search_for_feed
from app.user.utils import search_for_user
from tests.test_community_lifecycle import login
from tests.test_local_only_invisible import world  # noqa: F401 -- world is a fixture

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def requests(monkeypatch):
    """Every get_request the lookups make. Each one fails, as a fetch of these hosts would."""
    made = []

    def recording_get_request(url, *args, **kwargs):
        made.append(url)
        raise AssertionError(f'fetched {url}')

    for module in (user_utils, community_util, feed_util):
        monkeypatch.setattr(module, 'get_request', recording_get_request)
    return made


def _server(app):
    return app.config['SERVER_NAME']


@pytest.mark.parametrize('handle, expected', [
    ('onionite@abc.onion', 'onionite'),
    ('@onionite@abc.onion', 'onionite'),
    ('OnionITE@ABC.onion', 'onionite'),
    ('nobody@abc.onion', None),
    ('alice@abc.onion', None),        # alice is the first domain's: the onion has no alice
    ('alice@{server}', 'alice'),
    ('onionite@{server}', None),      # nor does the first domain have the onion's account
    ('nobody@{server}', None),
])
def test_a_user_on_a_hosted_domain_is_found_locally_and_never_fetched(app, world, requests, handle, expected):
    found = search_for_user(handle.format(server=_server(app)))
    assert (found.user_name if found else None) == expected
    assert requests == []


@pytest.mark.parametrize('handle, expected', [
    ('!hidden@abc.onion', 'hidden'),
    ('!nothing@abc.onion', None),
    ('!clearnet@abc.onion', None),
    ('!clearnet@{server}', 'clearnet'),
    ('!nothing@{server}', None),
])
def test_a_community_on_a_hosted_domain_is_found_locally_and_never_fetched(app, world, requests, handle, expected):
    found = search_for_community(handle.format(server=_server(app)))
    assert (found.name if found else None) == expected
    assert requests == []


def test_a_feed_on_a_hosted_domain_is_never_fetched(app, world, requests):
    assert search_for_feed('~nothing@abc.onion', signed_in=True) is None
    assert requests == []


@pytest.mark.parametrize('query', ['@nobody@abc.onion', '@onionite@abc.onion', 'nobody@abc.onion'])
def test_resolve_object_never_fetches_a_hosted_handle(app, world, requests, query):
    try:
        get_resolve_object(None, {'q': query}, user_id=world.alice.id)
    except Exception as refused:
        assert 'fetched' not in str(refused)
    assert requests == []


def test_a_member_opening_an_onion_handle_makes_no_request(app, world, requests):
    client = app.test_client()
    login(client, world.alice)
    client.get('/u/nobody@abc.onion', headers={'Accept': 'text/html'})
    assert requests == []
