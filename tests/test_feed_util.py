"""app/feed/util.py -- the helpers app/feed/routes.py calls.

MEASUREMENT BASIS. Before this file existed the module carried 44 missing
statements and 31 missing arcs on the full-suite --cov=app run at 97a56e713.

THE SLEEP. search_for_feed retries a failed webfinger after
`sleep(randint(3, 10))`, on the request thread. Every test that reaches that
path patches app.feed.util.sleep, or the suite pays ten seconds a row.
"""
import httpx
import pytest
from unittest.mock import MagicMock, patch

from app import db
from app.feed.util import (actor_to_feed, feed_communities_for_edit, feeds_for_form,
                           initialise_new_communities, search_for_feed)
from app.models import BannedInstances, Community, Feed, FeedItem
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    owner = make_user(instance, 'feedowner', local=True)
    return instance, owner


def _feed(user, name, **kwargs):
    kwargs.setdefault('public', True)
    feed = Feed(user_id=user.id, title=kwargs.pop('title', name), name=name,
                machine_name=name, instance_id=1,
                ap_profile_id=f'https://test.piefed.local/f/{name}',
                ap_public_url=f'https://test.piefed.local/f/{name}', **kwargs)
    db.session.add(feed)
    db.session.commit()
    return feed


@pytest.mark.parametrize('address', ['~name@host@extra', '~nohost'])
def test_searching_for_a_feed_rejects_an_address_that_is_not_one(app, db_session, address):
    """Was a PIN; INVERTED once the split was checked.

    ORIGINAL PINNED CLAIM, now false: ":39 unpacks address[1:].split('@') into
    two names, so an address carrying two '@' raises ValueError" -- reachable
    from feed_add_remote, whose first arm accepts any '~...@...' string.

    The second row is the other end of the same check: a '~' address with no
    host at all. Neither is a typo the server should guess at, and both now
    take the not-found path the callers already have.
    """
    _seed()
    with app.test_request_context('/'):
        assert search_for_feed(address) is None


def test_searching_for_a_local_feed_returns_it_without_fetching(app, db_session):
    """The control for the test above, and :44-46's local shortcut: a
    well-formed address whose server is this one resolves from the database
    with no request at all."""
    instance, owner = _seed()
    feed = _feed(owner, 'localfeed')
    server = app.config['SERVER_NAME']

    with app.test_request_context('/'):
        with patch('app.feed.util.get_request') as get:
            found = search_for_feed(f'~localfeed@{server}')

    assert found.id == feed.id
    assert get.call_count == 0
