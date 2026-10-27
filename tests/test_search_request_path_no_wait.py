"""C1 residual R1 item 1 (D738/D775): a resolver reached from a request path never sleeps and never retries.

search_for_user, search_for_community, search_for_feed and create_actor_from_remote run on web requests (the
search box, the API's resolve_object, a subscribe or follow route). A remote that does not answer used to hold
the request thread for get_request's own sleep-and-retry (3-10 s, then a second attempt with a doubled timeout),
and search_for_user and search_for_community wrapped another sleep and retry around that. remote_object_to_json
slept 3 s before its retry, and the API's resolve_object and resolve_remote_post_from_search reach it from a
request.

Owner's ruling: a request path makes one capped attempt; a Celery caller keeps its retry. So every resolver
defaults to retry=False and passes it to get_request (get_request(retry=False): one attempt, no sleep), and the
Celery callers -- bulk_follow, process_topic_communities, add_remote_community_from_post, mentioned_users (send_post
and send_reply) and the feed refresh -- ask for retry=True. remote_object_to_json keeps its 3-second retry for its
Celery callers; the request paths pass retry_delay=0.
"""
from unittest.mock import Mock, patch

import httpx
import pytest
from flask import g

from app.activitypub.actor import create_actor_from_remote
from app.activitypub.util import remote_object_to_json, resolve_remote_post_from_search
from app.admin.util import process_topic_communities
from app.api.alpha.utils.misc import get_resolve_object
from app.community.util import search_for_community
from app.feed.util import search_for_feed
from app.instance.util import bulk_follow
from app.shared.tasks.maintenance import add_remote_community_from_post
from app.shared.tasks.pages import mentioned_users
from app.user.utils import search_for_user
from app.utils import get_request
from tests.factories import give_site_a_key

SERVER = 'remote.test'
WEBFINGER = f'https://{SERVER}/.well-known/webfinger'
ACTOR = f'https://{SERVER}/u/someone'
OBJECT = f'https://{SERVER}/objects/1'


@pytest.fixture
def sleeps(monkeypatch):
    """One Mock for every sleep a resolver or get_request could reach."""
    slept = Mock()
    monkeypatch.setattr('app.utils.sleep', slept)
    monkeypatch.setattr('app.community.util.sleep', slept)
    monkeypatch.setattr('app.feed.util.sleep', slept)
    for module in ('app.user.utils', 'app.activitypub.actor', 'app.activitypub.util'):
        monkeypatch.setattr(f'{module}.time.sleep', slept)
    return slept


@pytest.fixture
def env(app, db_session, site):
    g.admin_ids = []
    g.site = give_site_a_key()   # the signed retry signs as the instance actor


def self_link():
    return {'links': [{'rel': 'self', 'type': 'application/activity+json', 'href': ACTOR}]}


def refused():
    return httpx.ConnectError('no route')


# --- get_request -----------------------------------------------------------


@pytest.mark.parametrize('error', [httpx.ConnectError('no route'), httpx.ReadError('reset'),
                                   httpx.ReadTimeout('slow')], ids=['connect', 'read', 'timeout'])
def test_get_request_without_retry_makes_one_attempt_and_does_not_sleep(app, http_mock, sleeps, error):
    route = http_mock.get(OBJECT).mock(side_effect=error)

    with pytest.raises(httpx.HTTPError):
        get_request(OBJECT, retry=False)

    assert route.call_count == 1
    sleeps.assert_not_called()


def test_get_request_retries_when_a_housekeeping_task_asks(app, http_mock, sleeps):
    route = http_mock.get(OBJECT).mock(side_effect=[refused(), httpx.Response(200, json={})])

    assert get_request(OBJECT, retry=True).status_code == 200
    assert route.call_count == 2
    sleeps.assert_called_once()


# --- the resolvers on a request path ----------------------------------------


def test_search_for_user_fetches_the_actor_once(env, http_mock, sleeps):
    http_mock.get(WEBFINGER).respond(200, json=self_link())
    actor = http_mock.get(ACTOR).mock(side_effect=refused())

    assert search_for_user(f'@someone@{SERVER}') is None
    assert actor.call_count == 1
    sleeps.assert_not_called()


def test_search_for_user_asks_webfinger_once(env, http_mock, sleeps):
    webfinger = http_mock.get(WEBFINGER).mock(side_effect=refused())

    with pytest.raises(httpx.HTTPError):
        search_for_user(f'@someone@{SERVER}')
    assert webfinger.call_count == 1
    sleeps.assert_not_called()


def test_search_for_user_signs_once(env, http_mock, sleeps):
    http_mock.get(WEBFINGER).respond(200, json=self_link())
    http_mock.get(ACTOR).respond(401)

    with patch('app.user.utils.signed_get_request', side_effect=refused()) as signed:
        assert search_for_user(f'@someone@{SERVER}') is None
    assert signed.call_count == 1
    sleeps.assert_not_called()


def test_a_retrying_user_search_signs_again_after_a_failed_signature(env, http_mock, sleeps):
    http_mock.get(WEBFINGER).respond(200, json=self_link())
    http_mock.get(ACTOR).respond(401)
    forbidden = Mock(status_code=403)

    with patch('app.user.utils.signed_get_request', side_effect=[refused(), forbidden]) as signed:
        assert search_for_user(f'@someone@{SERVER}', retry=True) is None
    assert signed.call_count == 2
    sleeps.assert_called_once_with(3)


def test_a_retrying_user_search_gives_up_after_two_failed_signatures(env, http_mock, sleeps):
    http_mock.get(WEBFINGER).respond(200, json=self_link())
    http_mock.get(ACTOR).respond(401)

    with patch('app.user.utils.signed_get_request', side_effect=refused()) as signed:
        assert search_for_user(f'@someone@{SERVER}', retry=True) is None
    assert signed.call_count == 2
    sleeps.assert_called_once_with(3)


def test_search_for_community_asks_webfinger_once(env, http_mock, sleeps):
    webfinger = http_mock.get(WEBFINGER).mock(side_effect=refused())

    assert search_for_community(f'!memes@{SERVER}') is None
    assert webfinger.call_count == 1
    sleeps.assert_not_called()


def test_search_for_community_fetches_the_group_once(env, http_mock, sleeps):
    http_mock.get(WEBFINGER).respond(200, json=self_link())
    actor = http_mock.get(ACTOR).mock(side_effect=refused())

    with pytest.raises(httpx.HTTPError):
        search_for_community(f'!memes@{SERVER}')
    assert actor.call_count == 1
    sleeps.assert_not_called()


def test_search_for_community_retries_webfinger_when_asked(env, http_mock, sleeps):
    webfinger = http_mock.get(WEBFINGER).mock(side_effect=refused())

    assert search_for_community(f'!memes@{SERVER}', retry=True) is None
    assert webfinger.call_count == 4   # get_request's own retry, twice
    assert sleeps.call_count == 3


@pytest.mark.parametrize('webfinger_fails', [True, False], ids=['webfinger', 'feed'])
def test_search_for_feed_asks_each_url_once(env, http_mock, sleeps, webfinger_fails):
    webfinger = http_mock.get(WEBFINGER)
    if webfinger_fails:
        webfinger.mock(side_effect=refused())
    else:
        webfinger.respond(200, json=self_link())
    actor = http_mock.get(ACTOR).mock(side_effect=refused()) if not webfinger_fails else None

    if webfinger_fails:
        assert search_for_feed(f'~news@{SERVER}', signed_in=True) is None
    else:
        with pytest.raises(httpx.HTTPError):
            search_for_feed(f'~news@{SERVER}', signed_in=True)
    assert webfinger.call_count == 1
    assert webfinger_fails or actor.call_count == 1
    sleeps.assert_not_called()


def test_create_actor_from_remote_fetches_an_actor_url_once(env, http_mock, sleeps):
    actor = http_mock.get(ACTOR).mock(side_effect=refused())

    assert create_actor_from_remote(ACTOR) is None
    assert actor.call_count == 1
    sleeps.assert_not_called()


@pytest.mark.parametrize('webfinger_fails', [True, False], ids=['webfinger', 'actor'])
def test_create_actor_from_remote_asks_each_webfinger_url_once(env, http_mock, sleeps, webfinger_fails):
    webfinger = http_mock.get(WEBFINGER)
    if webfinger_fails:
        webfinger.mock(side_effect=refused())
    else:
        webfinger.respond(200, json=self_link())
    actor = http_mock.get(ACTOR).mock(side_effect=refused()) if not webfinger_fails else None

    assert create_actor_from_remote(f'someone@{SERVER}') is None
    assert webfinger.call_count == 1
    assert webfinger_fails or actor.call_count == 1
    sleeps.assert_not_called()


def test_create_actor_from_remote_retries_when_asked(env, http_mock, sleeps):
    """The housekeeping tasks' path, unchanged: get_request's own retry. Its second failure is a plain HTTPError,
    which the fetch loop does not retry again."""
    actor = http_mock.get(ACTOR).mock(side_effect=refused())

    assert create_actor_from_remote(ACTOR, retry=True) is None
    assert actor.call_count == 2
    sleeps.assert_called_once()


# --- remote_object_to_json on a request path --------------------------------


def test_the_api_resolve_fetches_an_object_once(env, api_baseline, http_mock, sleeps):
    route = http_mock.get(OBJECT).mock(side_effect=refused())

    with pytest.raises(Exception, match='No object found.'):
        get_resolve_object(None, {'q': OBJECT}, user_id=api_baseline.user2.id)
    assert route.call_count == 1
    sleeps.assert_not_called()


def test_resolve_remote_post_from_search_fetches_an_object_once(env, http_mock, sleeps):
    route = http_mock.get(OBJECT).mock(side_effect=refused())

    assert resolve_remote_post_from_search(OBJECT) is None
    assert route.call_count == 1
    sleeps.assert_not_called()


def test_remote_object_to_json_keeps_its_retry_for_celery_callers(env, http_mock, sleeps):
    route = http_mock.get(OBJECT).mock(side_effect=refused())

    assert remote_object_to_json(OBJECT) is None
    assert route.call_count == 2
    sleeps.assert_called_once_with(3)


# --- the Celery callers keep their retry -------------------------------------


def test_bulk_follow_asks_for_the_retry(env, api_baseline):
    with patch('app.instance.util.search_for_user', return_value=None) as search:
        bulk_follow(api_baseline.user2.id, [f'someone@{SERVER}'])
    search.assert_called_once_with(f'someone@{SERVER}', retry=True)


def test_process_topic_communities_asks_for_the_retry(env):
    with patch('app.admin.util.search_for_community', return_value=None) as search:
        process_topic_communities({'communities': [f'!memes@{SERVER}']}, 1)
    search.assert_called_once_with(f'!memes@{SERVER}', retry=True)


def test_add_remote_community_from_post_asks_for_the_retry(env):
    with patch('app.shared.tasks.maintenance.search_for_community', return_value=None) as search:
        add_remote_community_from_post({'url': f'https://{SERVER}/c/memes'})
    search.assert_called_once_with(f'!memes@{SERVER}', retry=True)


def test_mentioned_users_asks_for_the_retry(env):
    post = Mock(body=f'hello @someone@{SERVER}', author=Mock(user_name='author'))
    with patch('app.shared.tasks.pages.search_for_user', return_value=None) as search:
        assert mentioned_users(post) == []
    search.assert_called_once_with(f'someone@{SERVER}', allow_fetch=True, retry=True, home=None)


# --- fix round 2: a GET retries only when a housekeeping task asks --------------


FEED_OWNERS = f'{ACTOR}/owners'


def test_a_feed_search_asks_for_its_owners_once(env, http_mock, sleeps):
    """actor_json_to_model's Feed branch fetches the owners collection on the search request (review I1)."""
    http_mock.get(WEBFINGER).respond(200, json=self_link())
    http_mock.get(ACTOR).respond(200, json={'id': ACTOR, 'type': 'Feed', 'preferredUsername': 'news',
                                            'attributedTo': FEED_OWNERS, 'following': f'{ACTOR}/following'})
    owners = http_mock.get(FEED_OWNERS).mock(side_effect=refused())

    with pytest.raises(httpx.HTTPError):
        search_for_feed(f'~news@{SERVER}', signed_in=True)
    assert owners.call_count == 1
    sleeps.assert_not_called()


def test_get_request_does_not_retry_unless_asked(app, http_mock, sleeps):
    route = http_mock.get(OBJECT).mock(side_effect=refused())

    with pytest.raises(httpx.HTTPError):
        get_request(OBJECT)
    assert route.call_count == 1
    sleeps.assert_not_called()
