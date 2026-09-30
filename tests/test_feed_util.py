"""app/feed/util.py -- the helpers app/feed/routes.py calls.

MEASUREMENT BASIS. Before this file existed the module carried 44 missing
statements and 31 missing arcs on the full-suite --cov=app run at 97a56e713.

THE SLEEP. search_for_feed retries a failed webfinger after
`sleep(randint(3, 10))` only when a Celery caller passes `retry=True` (D738);
request-path callers never sleep. Tests that reach the retry patch
app.feed.util.sleep, or the suite pays ten seconds a row.
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
        with pytest.raises(Exception) as with_reason:
            search_for_feed('~afeed@blocked.example')
        with pytest.raises(Exception) as without_reason:
            search_for_feed('~afeed@quiet.example')

    # EXACT messages, not a regex search: 'quiet.example is blocked.' is a
    # prefix of the message a mutant that always appends the reason produces
    # (' Reason: None'), and a prefix match cannot tell the two apart. The
    # domains differ between the two rows for the same reason -- a lookup that
    # dropped its domain filter would answer both with the first row's ban.
    assert str(with_reason.value) == 'blocked.example is blocked. Reason: spam'
    assert str(without_reason.value) == 'quiet.example is blocked.'


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


@pytest.mark.parametrize('document', [
    'not a document',           # `webfinger_json['links']` is a TypeError
    42,
    None,
    ['links'],                  # a list: `.get` is an AttributeError
    {},                         # no `links` at all: a KeyError
    {'links': 'https://remote.example/f/remotefeed'},   # iterated its CHARACTERS
    {'links': 42},
    {'links': None},
])
def test_a_webfinger_document_this_shape_cannot_walk(app, db_session, document):
    """D1397. This document comes from a REMOTE host, at a hostname the caller
    supplied, and every read of it assumed a mapping with a `links` list:
    `webfinger_json['links']` was a KeyError for a document without one, a
    TypeError for a string, and an AttributeError for a list -- and a string
    `links` iterated its characters into the loop one at a time.

    None of these can walk to an actor, so all of them answer None rather than
    raising out of `search_for_feed` into its caller. `search_for_community`
    carries the same guard for the same reason.
    """
    instance, owner = _seed()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = document

    with app.test_request_context('/'):
        with patch('app.feed.util.get_request', side_effect=[response]) as get:
            assert search_for_feed('~remotefeed@remote.example') is None

    assert get.call_count == 1


def test_a_links_entry_that_is_not_an_object_is_skipped(app, db_session):
    """The element, as distinct from the list. `'rel' in <a string>` is a substring
    test whose subscript raises, so one junk entry stopped the walk -- and a real
    entry after it must still be found."""
    instance, owner = _seed()
    feed = _feed(owner, 'remotefeed')
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {'links': [
        'https://remote.example/rel/1',
        42,
        {'rel': 'self', 'type': 'application/activity+json',
         'href': 'https://remote.example/f/remotefeed'}]}

    with app.test_request_context('/'):
        with patch('app.feed.util.get_request',
                   side_effect=[response, _actor_response()]), \
                patch('app.feed.util.actor_json_to_model', return_value=feed), \
                patch('app.feed.util.initialise_new_communities'):
            found = search_for_feed('~remotefeed@remote.example')

    assert found.id == feed.id


def test_a_webfinger_failure_on_the_request_path_is_not_retried(app, db_session):
    """D738, fixed. A failed webfinger used to sleep 3-10 seconds on the
    request thread and retry. By default it now gives up at once."""
    _seed()

    with app.test_request_context('/'):
        with patch('app.feed.util.sleep') as slept, \
                patch('app.feed.util.get_request',
                      side_effect=httpx.HTTPError('boom')) as get:
            assert search_for_feed('~remotefeed@remote.example') is None
        assert slept.call_count == 0
        assert get.call_count == 1


def test_a_webfinger_failure_is_retried_once_after_a_sleep_when_asked(app, db_session):
    """`retry=True`, which the Celery caller passes: one HTTPError is retried
    after `sleep(randint(3, 10))`; a second gives up and returns None. Both are
    asserted through the call count."""
    _seed()

    with app.test_request_context('/'):
        with patch('app.feed.util.sleep') as slept, \
                patch('app.feed.util.get_request',
                      side_effect=[httpx.HTTPError('boom'), _webfinger_response(),
                                   _actor_response()]) as get, \
                patch('app.feed.util.actor_json_to_model', return_value=None):
            assert search_for_feed('~remotefeed@remote.example', retry=True) is None
        assert slept.call_count == 1
        assert get.call_count == 3

    with app.test_request_context('/'):
        with patch('app.feed.util.sleep') as slept, \
                patch('app.feed.util.get_request',
                      side_effect=httpx.HTTPError('boom')) as get:
            assert search_for_feed('~remotefeed@remote.example', retry=True) is None
        assert slept.call_count == 1
        assert get.call_count == 2


@pytest.mark.parametrize('webfinger_status, links, actor_status, actor_type, description', [
    (404, None, 200, 'Feed', 'webfinger says no'),
    (200, [{'href': 'https://remote.example/f/remotefeed'}], 200, 'Feed', 'no rel'),
    (200, [{'rel': 'alternate', 'type': 'text/html',
            'href': 'https://remote.example/f/remotefeed'}], 200, 'Feed', 'wrong rel'),
    (200, None, 404, 'Feed', 'actor fetch fails'),
    (200, None, 200, 'Person', 'not a feed'),
])
def test_the_webfinger_walk_gives_up_at_each_step(app, db_session, webfinger_status,
                                                  links, actor_status, actor_type,
                                                  description):
    """Every way the walk ends in None: a webfinger that is not 200, a links
    entry with no `rel`, an actor fetch that fails, and an actor that is not a
    Feed.

    Five rows because each is a different statement's False arm, and the
    function returns None from all of them -- so only the call counts and the
    description tell them apart. The 'wrong rel' row is the one that keeps
    :79's SECOND operand load-bearing: an entry with a rel that is not 'self'
    passes the `'rel' in links` half.
    """
    _seed()

    with app.test_request_context('/'):
        with patch('app.feed.util.sleep'), \
                patch('app.feed.util.get_request',
                      side_effect=[_webfinger_response(webfinger_status, links),
                                   _actor_response(actor_status, actor_type)]), \
                patch('app.feed.util.actor_json_to_model') as to_model:
            assert search_for_feed('~remotefeed@remote.example') is None

    if description in ('webfinger says no', 'no rel', 'wrong rel'):
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


def test_the_form_choices_list_every_feed_but_the_current_one(app, db_session):
    """feeds_for_form and feeds_for_form_children, over a three-level tree.

    Three levels are the minimum that shows the depth prefix growing: the
    children carry '--' and the grandchildren '----', and a recursion that
    passed the same depth down would give both the same.

    The current feed is excluded at the CHILD level here, not the top, because
    that exclusion is a separate statement in the recursive function and the
    top-level one cannot reach it.
    """
    instance, owner = _seed()
    top = _feed(owner, 'topfeed', title='Top')
    child = _feed(owner, 'childfeed', title='Child', parent_feed_id=top.id)
    _feed(owner, 'grandchildfeed', title='Grandchild', parent_feed_id=child.id)

    with app.test_request_context('/'):
        choices = feeds_for_form(child.id, owner.id)

    labels = [label for _, label in choices]
    assert labels[0] == 'None'
    assert 'Top' in labels
    assert '---- Grandchild' in labels
    assert not any(label.endswith('Child') and 'Grand' not in label
                   for label in labels[1:])


def test_the_form_choices_skip_the_current_feed_at_the_top_level(app, db_session):
    """The same exclusion in the non-recursive half, which is a different
    statement."""
    instance, owner = _seed()
    top = _feed(owner, 'topfeed', title='Top')
    _feed(owner, 'otherfeed', title='Other')

    with app.test_request_context('/'):
        labels = [label for _, label in feeds_for_form(top.id, owner.id)]

    assert 'Top' not in labels
    assert 'Other' in labels


@pytest.mark.parametrize('actor, expected', [
    ('remotefeed@remote.example', 'remote'),
    ('localfeed', 'local'),
    ('LocalFeed', 'local'),
    ('  localfeed  ', 'local'),
])
def test_actor_to_feed_resolves_both_address_shapes(app, db_session, actor, expected):
    """Both arms, plus the case-insensitive comparison and the strip.

    The mixed-case row is what makes func.lower() on both sides load-bearing,
    and a REMOTE feed named 'localfeed' exists in the fixture so the bare-name
    arm's `ap_id=None` filter is load-bearing too: without it the remote row
    could answer a local lookup.
    """
    instance, owner = _seed()
    make_instance('remote.example', software='piefed')
    local = _feed(owner, 'localfeed')
    remote = _feed(owner, 'remotefeed', ap_id='remotefeed@remote.example')

    with app.test_request_context('/'):
        found = actor_to_feed(actor)

    assert found.id == (remote.id if expected == 'remote' else local.id)


@pytest.mark.parametrize('lookup', ['bare', 'local-shortcut'])
def test_a_remote_feed_does_not_answer_a_local_name_lookup(app, db_session, lookup):
    """The `ap_id=None` filter in both lookups that carry one -- actor_to_feed's
    bare-name arm and search_for_feed's local shortcut.

    THE FIXTURE HAS TO BE THE ONLY FEED WITH THAT NAME, and that is a fact
    about the schema rather than a choice: `Feed.name` is unique
    (app/models.py:4093), so a local and a remote feed cannot share one. The
    filter is therefore observable only as the difference between finding the
    remote row and finding nothing -- which is what a bare-name lookup for a
    feed this server does not host must answer.
    """
    instance, owner = _seed()
    make_instance('remote.example', software='piefed')
    _feed(owner, 'onlyremote', ap_id='onlyremote@remote.example')
    server = app.config['SERVER_NAME']

    with app.test_request_context('/'):
        with patch('app.feed.util.get_request') as get, \
                patch('app.feed.util.sleep'):
            if lookup == 'bare':
                assert actor_to_feed('onlyremote') is None
            else:
                assert search_for_feed(f'~onlyremote@{server}') is None
    assert get.call_count == 0


def test_actor_to_feed_returns_nothing_for_an_unknown_name(app, db_session):
    """The None path, which the routes turn into a 404."""
    _seed()
    with app.test_request_context('/'):
        assert actor_to_feed('nosuchfeed') is None


def test_the_edit_form_lists_a_feeds_communities_sorted_and_qualified(app, db_session):
    """feed_communities_for_edit: local communities get '@SERVER_NAME'
    appended, remote ones already carry a host, banned ones are excluded, and
    the result is sorted.

    The fixture adds them in reverse alphabetical order precisely so the sort
    is observable, and the banned one shares a feed with the others so its
    exclusion is the only thing keeping it out.
    """
    instance, owner = _seed()
    feed = _feed(owner, 'listfeed')
    server = app.config['SERVER_NAME']
    remote = make_community(name='zulu', host='remote.example')
    remote.ap_id = 'zulu@remote.example'
    local = make_community(name='alpha', host=server)
    banned = make_community(name='mike', host=server)
    banned.banned = True
    db.session.add_all([FeedItem(feed_id=feed.id, community_id=remote.id),
                        FeedItem(feed_id=feed.id, community_id=local.id),
                        FeedItem(feed_id=feed.id, community_id=banned.id)])
    db.session.commit()

    with app.test_request_context('/'):
        result = feed_communities_for_edit(feed.id)

    lines = result.split('\n')
    assert lines == sorted(lines)
    # Community.link() returns the bare 'name@host' for a local community --
    # no '!' prefix -- and the function appends the server only when the link
    # carries no host at all. Asserted as measured rather than as assumed.
    assert f'alpha@{server}' in lines
    assert any(line.endswith('@remote.example') for line in lines)
    assert not any('mike' in line for line in lines)


def test_a_feed_with_no_communities_initialises_nothing(app, db_session):
    """initialise_new_communities' early return at :109-110."""
    instance, owner = _seed()
    feed = _feed(owner, 'emptyfeed')
    feed.num_communities = 0
    db.session.commit()

    with patch('app.feed.util.retrieve_mods_and_backfill') as backfill:
        initialise_new_communities(feed)

    assert backfill.call_count == 0


def test_a_stale_community_count_stops_the_backfill(app, db_session):
    """:109-110's early return is decided by the COUNTER, not by the rows: a
    feed whose num_communities is 0 but which has member_communities anyway --
    a counter that has drifted -- backfills nothing.

    That is what makes the early return observable at all, and it is worth
    knowing: the counter is maintained by _feed_add_community and
    _feed_remove_community, and anything that writes FeedItem rows without them
    leaves this function blind.
    """
    instance, owner = _seed()
    feed = _feed(owner, 'stalefeed')
    community = make_community(name='alpha', host='remote.example')
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    feed.num_communities = 0
    db.session.commit()

    with app.test_request_context('/'):
        with patch('app.feed.util.retrieve_mods_and_backfill') as backfill:
            initialise_new_communities(feed)

    assert backfill.call_count == 0
    assert backfill.delay.call_count == 0


@pytest.mark.parametrize('debug, expected_inline, expected_delayed', [
    (True, 1, 0),
    (False, 0, 2),
])
def test_new_communities_are_backfilled_once_in_debug_and_all_of_them_otherwise(
        app, db_session, debug, expected_inline, expected_delayed):
    """:112-118. The debug arm BREAKS after the first community -- deliberately,
    per its comment -- and the other dispatches for every one.

    Two empty communities are what make that visible, and a THIRD with posts is
    the control for `community.post_count == 0`: it must be skipped in both
    states.
    """
    instance, owner = _seed()
    feed = _feed(owner, 'newfeed')
    first = make_community(name='alpha', host='remote.example')
    second = make_community(name='bravo', host='remote.example')
    already_full = make_community(name='charlie', host='remote.example')
    already_full.post_count = 5
    db.session.add_all([FeedItem(feed_id=feed.id, community_id=first.id),
                        FeedItem(feed_id=feed.id, community_id=second.id),
                        FeedItem(feed_id=feed.id, community_id=already_full.id)])
    feed.num_communities = 3
    db.session.commit()

    backfill = MagicMock()
    with app.test_request_context('/'):
        with patch('app.feed.util.retrieve_mods_and_backfill', backfill), \
                patch('app.feed.util.current_app', new_callable=MagicMock) as current_app_stub:
            current_app_stub.debug = debug
            initialise_new_communities(feed)

    assert backfill.call_count == expected_inline
    assert backfill.delay.call_count == expected_delayed
    calls = backfill.call_args_list if debug else backfill.delay.call_args_list
    assert all(call.args[0] != already_full.id for call in calls)


def test_searching_for_an_address_without_a_tilde_returns_nothing(app, db_session):
    """:38's False arm, which falls off the end of the function and returns
    None implicitly.

    feed_add_remote prefixes the '~' itself for the shapes it accepts
    (app/feed/routes.py:115, :118), so this is the arm a caller reaches by
    passing an address it has not normalised -- and the answer is None rather
    than a crash or a bare-name lookup.
    """
    instance, owner = _seed()
    _feed(owner, 'localfeed')

    with app.test_request_context('/'):
        with patch('app.feed.util.get_request') as get:
            assert search_for_feed('localfeed') is None
            assert search_for_feed('localfeed@remote.example') is None

    assert get.call_count == 0
