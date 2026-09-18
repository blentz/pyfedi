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


def test_a_banned_instance_is_refused_with_its_reason(app, db_session):
    """:41-43. The reason is appended only when the ban carries one, so the two
    rows below differ in the message rather than in the outcome."""
    _seed()
    db.session.add(BannedInstances(domain='blocked.example', reason='spam'))
    db.session.add(BannedInstances(domain='quiet.example'))
    db.session.commit()

    with app.test_request_context('/'):
        with pytest.raises(Exception, match='blocked.example is blocked. Reason: spam'):
            search_for_feed('~afeed@blocked.example')
        with pytest.raises(Exception, match='quiet.example is blocked.'):
            search_for_feed('~afeed@quiet.example')


def test_a_known_remote_feed_is_returned_without_fetching(app, db_session):
    """:49-51. A feed already in the database short-circuits the webfinger walk
    entirely -- asserted by the request count, since the return value alone
    cannot tell a cached hit from a fetched one."""
    instance, owner = _seed()
    remote_instance = make_instance('remote.example', software='piefed')
    feed = _feed(owner, 'remotefeed', ap_id='remotefeed@remote.example')

    with app.test_request_context('/'):
        with patch('app.feed.util.get_request') as get:
            found = search_for_feed('~remotefeed@remote.example')

    assert found.id == feed.id
    assert get.call_count == 0


def test_an_unknown_feed_is_not_fetched_when_fetching_is_off(app, db_session):
    """:52-53. allow_fetch=False is how callers ask 'do we already know this
    one?' without making a request."""
    _seed()
    with app.test_request_context('/'):
        with patch('app.feed.util.get_request') as get:
            assert search_for_feed('~unknown@remote.example', allow_fetch=False) is None
    assert get.call_count == 0


def _webfinger_response(status_code=200, links=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {'links': links if links is not None else [
        {'rel': 'self', 'type': 'application/activity+json',
         'href': 'https://remote.example/f/remotefeed'}]}
    return response


def _actor_response(status_code=200, actor_type='Feed'):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {'type': actor_type, 'preferredUsername': 'remotefeed'}
    return response


def test_an_unknown_feed_is_fetched_through_webfinger(app, db_session):
    """:57-84, the whole walk: webfinger, the `rel: self` link, the actor
    fetch, the type check, actor_json_to_model, and initialise_new_communities.

    actor_json_to_model is patched: turning a JSON actor into a row is
    app/activitypub's covered ground, and this test is about the walk that
    reaches it. Its arguments are asserted, because the name and server come
    from the parsed address and a swapped pair is exactly what this function
    could get wrong.
    """
    instance, owner = _seed()
    feed = _feed(owner, 'remotefeed')

    with app.test_request_context('/'):
        with patch('app.feed.util.get_request',
                   side_effect=[_webfinger_response(), _actor_response()]) as get, \
                patch('app.feed.util.actor_json_to_model', return_value=feed) as to_model, \
                patch('app.feed.util.initialise_new_communities') as initialise:
            found = search_for_feed('~remotefeed@remote.example')

    assert found.id == feed.id
    assert get.call_args_list[0].args == ('https://remote.example/.well-known/webfinger',)
    assert get.call_args_list[0].kwargs['params'] == {
        'resource': 'acct:~remotefeed@remote.example'}
    assert get.call_args_list[1].args == ('https://remote.example/f/remotefeed',)
    assert to_model.call_args.args[1:] == ('remotefeed', 'remote.example')
    assert initialise.call_count == 1


def test_a_webfinger_failure_is_retried_once_after_a_sleep(app, db_session):
    """:58-67. One HTTPError is retried after `sleep(randint(3, 10))`; a second
    gives up and returns None.

    The sleep is patched -- it runs on the request thread, and the suite would
    otherwise pay up to ten seconds for this test alone (registered as R1).
    Both the retry and the give-up are asserted through the call count.
    """
    _seed()

    with app.test_request_context('/'):
        with patch('app.feed.util.sleep') as slept, \
                patch('app.feed.util.get_request',
                      side_effect=[httpx.HTTPError('boom'), _webfinger_response(),
                                   _actor_response()]) as get, \
                patch('app.feed.util.actor_json_to_model', return_value=None):
            assert search_for_feed('~remotefeed@remote.example') is None
        assert slept.call_count == 1
        assert get.call_count == 3

    with app.test_request_context('/'):
        with patch('app.feed.util.sleep') as slept, \
                patch('app.feed.util.get_request',
                      side_effect=httpx.HTTPError('boom')) as get:
            assert search_for_feed('~remotefeed@remote.example') is None
        assert slept.call_count == 1
        assert get.call_count == 2


@pytest.mark.parametrize('webfinger_status, links, actor_status, actor_type, description', [
    (404, None, 200, 'Feed', 'webfinger says no'),
    (200, [{'href': 'https://remote.example/f/remotefeed'}], 200, 'Feed', 'no rel'),
    (200, None, 404, 'Feed', 'actor fetch fails'),
    (200, None, 200, 'Person', 'not a feed'),
])
def test_the_webfinger_walk_gives_up_at_each_step(app, db_session, webfinger_status,
                                                  links, actor_status, actor_type,
                                                  description):
    """Every way the walk ends in None: a webfinger that is not 200, a links
    entry with no `rel`, an actor fetch that fails, and an actor that is not a
    Feed.

    Four rows because each is a different statement's False arm, and the
    function returns None from all of them -- so only the call counts and the
    description tell them apart.
    """
    _seed()

    with app.test_request_context('/'):
        with patch('app.feed.util.sleep'), \
                patch('app.feed.util.get_request',
                      side_effect=[_webfinger_response(webfinger_status, links),
                                   _actor_response(actor_status, actor_type)]), \
                patch('app.feed.util.actor_json_to_model') as to_model:
            assert search_for_feed('~remotefeed@remote.example') is None

    if description in ('webfinger says no', 'no rel'):
        assert to_model.call_count == 0


def test_a_feed_the_actor_model_refuses_is_not_returned(app, db_session):
    """:81-84's False arm: actor_json_to_model returning None means the walk
    found an actor it could not turn into a row, and the caller gets None
    rather than an exception."""
    _seed()

    with app.test_request_context('/'):
        with patch('app.feed.util.get_request',
                   side_effect=[_webfinger_response(), _actor_response()]), \
                patch('app.feed.util.actor_json_to_model', return_value=None), \
                patch('app.feed.util.initialise_new_communities') as initialise:
            assert search_for_feed('~remotefeed@remote.example') is None

    assert initialise.call_count == 0
