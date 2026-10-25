"""find_actor_or_create looks an actor on any hosted domain up locally and never fetches it (residual R2).

find_actor_by_url took only SERVER_NAME for local, so `https://<onion>/u/x` or `x@<onion>` that was not already
found went to create_actor_from_remote: an actor fetch or WebFinger against the onion itself. A miss is None.
"""
import pytest

import app.activitypub.actor as actor_module
import app.activitypub.util as ap_util
from app import db
from app.activitypub.util import find_actor_or_create
from tests.test_local_only_invisible import world  # noqa: F401 -- world is a fixture

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def requests(monkeypatch):
    made = []

    def recording(url, *args, **kwargs):
        made.append(url)
        raise AssertionError(f'fetched {url}')

    for module in (actor_module, ap_util):
        for name in ('get_request', 'signed_get_request'):
            monkeypatch.setattr(module, name, recording, raising=False)
    return made


@pytest.fixture
def stored(app, world):
    world.alice.ap_profile_id = f"https://{app.config['SERVER_NAME']}/u/alice"   # as finalize_user_setup stores it
    db.session.commit()
    return world


@pytest.mark.parametrize('actor, expected, kwargs', [
    ('http://abc.onion/u/onionite', 'onionite', {}),
    ('http://abc.onion/u/nobody', None, {}),
    ('onionite@abc.onion', 'onionite', {}),
    ('nobody@abc.onion', None, {}),
    ('https://abc.onion/c/hidden', 'hidden', {'community_only': True}),
    ('https://abc.onion/c/nothing', None, {'community_only': True}),
    ('https://{server}/u/alice', 'alice', {}),
    ('https://{server}/u/nobody', None, {}),
    ('alice@{server}', 'alice', {}),
    ('nobody@{server}', None, {}),
])
def test_an_actor_on_a_hosted_domain_is_found_locally_and_never_fetched(app, stored, requests, actor, expected,
                                                                         kwargs):
    found = find_actor_or_create(actor.format(server=app.config['SERVER_NAME']), **kwargs)
    name = getattr(found, 'user_name', None) or getattr(found, 'name', None) if found else None
    assert name == expected
    assert requests == []


def test_the_onions_handle_is_not_first_domains_account(app, stored, requests):
    assert find_actor_or_create('alice@abc.onion') is None
    assert requests == []


@pytest.mark.parametrize('actor, hosted', [('http://[abc.onion/u/x', None), ('https://abc.onion/u/x', 'abc.onion'),
                                           ('@x@abc.onion', 'abc.onion'), ('x@remote.test', None), ('bare', None)])
def test_hosted_domain_of(app, stored, actor, hosted):
    """A URL urlsplit refuses (an unbalanced IPv6 bracket) names no host of ours."""
    found = actor_module.hosted_domain_of(actor)
    assert (found.domain if found else None) == hosted
