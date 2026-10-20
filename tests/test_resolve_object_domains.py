"""resolve_object resolves a local handle or URL on the domain its host names, and never fetches it (residual R1).

It used to strip `@SERVER_NAME` from a handle and look the bare name up on every domain, so with a `bob` on both
hell.cloud and the onion, `@bob@hell.cloud` could answer with the onion's bob. A miss is a miss: nothing is fetched.
"""
import pytest

import app.activitypub.util as ap_util
import app.community.util as community_util
import app.feed.util as feed_util
import app.user.utils as user_utils
from app.api.alpha.utils.misc import get_resolve_object
from tests.factories import make_user
from tests.two_domains import HELL, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def requests(monkeypatch):
    made = []

    def recording_get_request(url, *args, **kwargs):
        made.append(url)
        raise AssertionError(f'fetched {url}')

    for module in (user_utils, community_util, feed_util, ap_util):
        monkeypatch.setattr(module, 'get_request', recording_get_request, raising=False)
    return made


@pytest.fixture
def two_bobs(app, two_domains):
    """A `bob` on each domain. The onion's row goes straight in: creation refuses a name another domain holds."""
    hell, onion = two_domains.domains
    caller = make_user(two_domains.instance, 'caller', local=True)
    return caller, two_domains.local_user('bob', hell), two_domains.local_user('bob', onion)


def _resolved(caller, query):
    return get_resolve_object(None, {'q': query}, user_id=caller.id)['person']['person']['id']


@pytest.mark.parametrize('query, whose', [
    (f'@bob@{HELL}', 'hell'),
    (f'@bob@{ONION}', 'onion'),
    (f'@BOB@{ONION}', 'onion'),
    (f'https://{ONION}/u/bob/', 'onion'),     # not its stored id, so the URL branch resolves it
    (f'https://{HELL}/u/bob/', 'hell'),
    (f'https://{HELL}/u/bob@{ONION}', 'onion'),
    (f'https://{ONION}/u/bob@{HELL}', 'hell'),
])
def test_each_handle_resolves_to_its_own_domains_account(app, two_bobs, requests, query, whose):
    caller, hell_bob, onion_bob = two_bobs
    assert _resolved(caller, query) == {'hell': hell_bob, 'onion': onion_bob}[whose].id
    assert requests == []


@pytest.mark.parametrize('query', [f'@nobody@{ONION}', f'@nobody@{HELL}', f'https://{ONION}/u/nobody/'])
def test_a_miss_is_a_miss(app, two_bobs, requests, query):
    caller, hell_bob, onion_bob = two_bobs
    with pytest.raises(Exception, match='No object found'):
        get_resolve_object(None, {'q': query}, user_id=caller.id)
    assert [url for url in requests if HELL in url or ONION in url] == []


def test_a_foreign_host_that_ends_like_ours_is_not_ours(app, two_bobs, requests):
    """`@bob@nottest.piefed.local` ends with SERVER_NAME; the old suffix test took it for hell.cloud's bob. It is a
    remote handle, so it is looked for over there (refused here by the recording fixture)."""
    caller, hell_bob, onion_bob = two_bobs
    with pytest.raises(Exception, match=f'fetched https://not{HELL}/'):
        get_resolve_object(None, {'q': f'@bob@not{HELL}'}, user_id=caller.id)
