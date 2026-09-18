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


def test_searching_for_a_feed_with_two_at_signs_raises(app, db_session):
    """PIN (P2): :39 unpacks address[1:].split('@') into two names, so an
    address carrying two '@' raises ValueError.

    Reachable from feed_add_remote, whose first arm is exactly
    `address.startswith('~') and '@' in address` -- so '~a@b@c' typed into the
    add-remote box is a 500.
    """
    _seed()
    with app.test_request_context('/'):
        with pytest.raises(ValueError, match='too many values to unpack'):
            search_for_feed('~name@host@extra')
