"""resolve_object resolves a local handle or URL on the domain its host names, and never fetches it (residual R1).

It used to strip `@SERVER_NAME` from a handle and look the bare name up on every domain, so with a `bob` on both
the first domain and the onion, `@bob@SERVER_NAME` could answer with the onion's bob. A miss is a miss: nothing is fetched.
"""
import pytest

from app.api.alpha.utils.misc import get_resolve_object
from tests.factories import make_user
from tests.two_domains import FIRST, ONION, no_http, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def two_bobs(app, two_domains):
    """A `bob` on each domain. The onion's row goes straight in: creation refuses a name another domain holds."""
    first, onion = two_domains.domains
    caller = make_user(two_domains.instance, 'caller', local=True)
    return caller, two_domains.local_user('bob', first), two_domains.local_user('bob', onion)


def _resolved(caller, query):
    return get_resolve_object(None, {'q': query}, user_id=caller.id)['person']['person']['id']


@pytest.mark.parametrize('query, whose', [
    (f'@bob@{FIRST}', 'first'),
    (f'@bob@{ONION}', 'onion'),
    (f'@BOB@{ONION}', 'onion'),
    (f'https://{ONION}/u/bob/', 'onion'),     # not its stored id, so the URL branch resolves it
    (f'https://{FIRST}/u/bob/', 'first'),
    (f'https://{FIRST}/u/bob@{ONION}', 'onion'),
    (f'https://{ONION}/u/bob@{FIRST}', 'first'),
])
def test_each_handle_resolves_to_its_own_domains_account(app, two_bobs, no_http, query, whose):
    caller, first_bob, onion_bob = two_bobs
    assert _resolved(caller, query) == {'first': first_bob, 'onion': onion_bob}[whose].id
    assert no_http == []


@pytest.mark.parametrize('query', [f'@nobody@{ONION}', f'@nobody@{FIRST}', f'https://{ONION}/u/nobody/'])
def test_a_miss_is_a_miss(app, two_bobs, no_http, query):
    caller, first_bob, onion_bob = two_bobs
    with pytest.raises(Exception, match='No object found'):
        get_resolve_object(None, {'q': query}, user_id=caller.id)
    assert [url for url in no_http if FIRST in url or ONION in url] == []


def test_a_foreign_host_that_ends_like_ours_is_not_ours(app, two_bobs, no_http):
    """`@bob@nottest.piefed.local` ends with SERVER_NAME; the old suffix test took it for the first domain's bob. It is a
    remote handle, so it is looked for over there (refused here by the recording fixture)."""
    caller, first_bob, onion_bob = two_bobs
    with pytest.raises(Exception, match=f'fetched https://not{FIRST}/'):
        get_resolve_object(None, {'q': f'@bob@not{FIRST}'}, user_id=caller.id)
