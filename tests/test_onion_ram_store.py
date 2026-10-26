"""Onion state lives in a RAM-only Redis (onion unlinkability spec 2026-10-26, goal 7 and section 5, "The main Redis
rule" and "The RAM-only store").

No key in the main Redis may be written, or have its value or TTL changed, because of an onion request; Celery broker
messages are the one exception. The onion's keys go to the onion store (`app.onion_redis_client`), and its rate-limit
buckets to the limiter's onion storage. Two fakeredis servers stand for the two Redises (`redis_double`, the main;
`onion_redis`, the onion store), so a test can tell which one a key reached. The limiter's two storages are in-memory
stand-ins, as the limits library's Redis storage needs Lua, which fakeredis has not got here.
"""
import io
import json

import fakeredis
import pytest
from limits.storage import MemoryStorage, storage_from_string
from redis.exceptions import ConnectionError as RedisConnectionError

from app import create_app, db, limiter
from app.models import Post, User
from app.onion_store import OnionRoutedStorage, onion_store_needed, onion_store_optional, request_redis
from app.user.routes import import_settings_task
from app.utils import set_setting
from config import Config, limits_redis_uri
from tests.conftest import TestConfig
from tests.factories import make_community, make_post
from tests.test_onion_pow import (FROM_CADDY, MARKER, ONION_URL, PASSWORD, PROXY, alice, challenge, dora,  # noqa: F401
                                  login_data, onion, onion_get, onion_page, onion_post, open_registration,
                                  register_data)

CLEARNET_IP = '198.51.100.23'
NOT_BROKER = ('celery', 'kombu', '_kombu', 'unacked')


class PlainLock:
    """A redis-py lock without Lua: fakeredis has no EVALSHA here, which Lock.release needs. It still writes its key
    and deletes it again, as the real one does."""

    def __init__(self, redis, name, timeout=None):
        self.redis, self.name, self.timeout = redis, name, timeout

    def __enter__(self):
        assert self.redis.set(self.name, '1', nx=True, ex=int(self.timeout or 10))
        return self

    def __exit__(self, *exc):
        self.redis.delete(self.name)
        return False


@pytest.fixture
def main_redis(redis_double, monkeypatch):
    monkeypatch.setattr(redis_double, 'lock', lambda name, timeout=None, **kwargs: PlainLock(redis_double, name, timeout))
    return redis_double


class LimitedConfig(TestConfig):
    RATELIMIT_ENABLED = True
    ONION_PROXY_IP = PROXY
    WTF_CSRF_ENABLED = True      # the login and register forms render their token
    ENABLE_ALPHA_API = 'true'


@pytest.fixture(scope='module')
def limited_app():
    """An app with the limiter on: Flask-Limiter builds no storage for an app it is off in (the suite's)."""
    limited = create_app(LimitedConfig)
    limiter.enabled = False
    yield limited
    limiter.enabled = False


@pytest.fixture
def limits_on(limited_app, monkeypatch):
    """The limiter on, its two storages in memory, so a test sees which one each bucket went to."""
    storage = limiter.storage
    monkeypatch.setattr(storage, 'main', MemoryStorage())
    monkeypatch.setattr(storage, 'onion', MemoryStorage())
    monkeypatch.setattr(storage, 'onion_fallback', None)
    limiter.enabled = True
    try:
        yield storage
    finally:
        limiter.enabled = False


def down():
    """A Redis that does not answer: every command raises ConnectionError."""
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis.FakeRedis(server=server, decode_responses=True)


class DownStorage(MemoryStorage):
    """A limiter storage whose Redis does not answer."""

    @property
    def base_exceptions(self):
        return ConnectionError

    def incr(self, *args, **kwargs):
        raise ConnectionError('down')

    def get(self, *args, **kwargs):
        raise ConnectionError('down')

    def get_expiry(self, *args, **kwargs):
        raise ConnectionError('down')

    def clear(self, *args, **kwargs):
        raise ConnectionError('down')


def state(redis):
    """Every key outside the Celery broker's, with its value and TTL: what an onion request must leave as it was."""
    snapshot = {}
    for key in redis.keys('*'):
        if key.startswith(NOT_BROKER):
            continue
        kind = redis.type(key)
        value = {'string': redis.get, 'hash': redis.hgetall, 'zset': lambda k: redis.zrange(k, 0, -1, withscores=True),
                 'set': redis.smembers, 'list': lambda k: redis.lrange(k, 0, -1)}[kind](key)
        snapshot[key] = (kind, value, redis.ttl(key))
    return snapshot


@pytest.fixture
def world(app, site, alice, open_registration, main_redis, monkeypatch):
    """A community with a post by someone else, on the default domain, and alice, who logs in over the onion."""
    set_setting('onion_pow_difficulty', 0)      # any nonce solves a challenge
    site.private_instance = False
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    community = make_community('news')
    author = User.query.filter_by(user_name='founder').first()
    post = make_post(community, author, 'https://test.piefed.local/post/1', title='hello')
    return {'community': community, 'post': post, 'alice': alice}


def pages(post):
    """The index, a community, a post and a user, as the onion shows them."""
    return ('/', '/c/news@test.piefed.local', f'/post/{post.id}', '/u/founder@test.piefed.local')


def vote(client, post):
    """An upvote, as the post page's button sends it (hx-post, with the page's CSRF token in a header)."""
    token = onion_page(client, f'/post/{post.id}').find('meta', attrs={'name': 'csrf-token'})['content']
    return client.post(f'/post/{post.id}/upvote/default', base_url=ONION_URL, headers={**MARKER, 'X-CSRFToken': token},
                       environ_base=FROM_CADDY)


def logged_in(response):
    return response.status_code == 302 and '/auth/login' not in response.headers['Location']


def proven(page, **data):
    return login_data(page.csrf, pow_token=page.token, pow_nonce='a', **data)


def test_onion_crawl_writes_nothing_to_the_main_redis(limited_app, world, main_redis, onion_redis, limits_on):
    before = state(main_redis)
    client = limited_app.test_client()
    post = world['post']
    for path in pages(post) + ('/auth/register', '/testredis'):
        assert onion_page(client, path) is not None
    page = challenge(onion_page(client, '/auth/login'))
    assert not logged_in(onion_post(client, '/auth/login', data=proven(page, password='wrong password')))
    page = challenge(onion_page(client, '/auth/login'))
    assert logged_in(onion_post(client, '/auth/login', data=proven(page)))                  # a PoW spend
    assert onion_get(client, '/home/new/popular?result_id=abc').status_code == 200                        # the feed cache
    assert vote(client, post).status_code == 200
    api = limited_app.test_client(use_cookies=False)
    assert onion_post(api, '/api/alpha/user/login', json={'username': 'alice', 'password': PASSWORD}).status_code == 200

    assert state(main_redis) == before
    db.session.expire_all()
    assert db.session.get(Post, post.id).up_votes == 1
    assert onion_redis.get(next(iter(onion_redis.keys('votes_cast_*')))) == '1'
    assert onion_redis.keys('onion_pow_used:*') and onion_redis.keys('feed:*')
    assert limits_on.onion.storage and not limits_on.main.storage


def test_onion_guard_keys_are_in_the_onion_store(app, world, main_redis, onion_redis):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    onion_post(client, '/auth/login', data=proven(page, password='wrong password'))
    assert onion_get(client, '/auth/onion_wait?form=login').status_code == 200
    assert onion_redis.hget('onion_login_fail:typed:alice', 'count') == '1'
    assert onion_redis.hget(f'onion_login_fail:acct:{world["alice"].id}', 'count') == '1'
    assert len(onion_redis.keys('onion_pow_used:*')) == 1
    assert len(onion_redis.keys('onion_wait_current:*')) == 1
    assert not main_redis.keys('onion*')


@pytest.fixture
def onion_store_down(monkeypatch):
    monkeypatch.setattr('app.onion_redis_client', down())


def store_down_answer(response):
    assert response.status_code == 503
    assert 'Please try again shortly.' in response.get_data(as_text=True)
    return True


def test_onion_login_is_503_when_the_onion_store_is_down(app, world, onion_store_down):
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))       # the form itself needs no store
    assert store_down_answer(onion_post(client, '/auth/login', data=proven(page)))
    assert store_down_answer(onion_get(client, '/auth/register'))         # its captcha waits in the store
    set_setting('captcha_enabled', False)
    page = challenge(onion_page(client, '/auth/register'))
    assert store_down_answer(onion_post(client, '/auth/register',
                                        data=register_data(page.csrf, pow_token=page.token, pow_nonce='a')))
    assert store_down_answer(onion_get(client, '/auth/onion_wait?form=login'))
    api = app.test_client(use_cookies=False)
    response = onion_post(api, '/api/alpha/user/login', json={'username': 'alice', 'password': PASSWORD})
    assert store_down_answer(response)
    assert response.get_json() == {'code': 503, 'message': 'Please try again shortly.', 'status': 'Service Unavailable'}
    assert response.headers['Retry-After'] == '30'


def test_onion_browsing_works_when_the_onion_store_is_down(limited_app, world, limits_on, onion_store_down,
                                                          monkeypatch):
    monkeypatch.setattr(limits_on, 'onion', DownStorage())
    client = limited_app.test_client()
    post = world['post']
    for path in pages(post) + ('/auth/login', '/testredis'):
        assert onion_page(client, path) is not None
    with client.session_transaction(base_url=ONION_URL) as stored:
        stored['_user_id'] = str(world['alice'].id)
        stored['_fresh'] = True
    for path in pages(post):        # logged in: a profile shows the viewer's vote quota, read from the store
        assert onion_page(client, path) is not None
    assert onion_get(client, '/home/new/popular?result_id=abc').status_code == 200      # the feed cache is skipped
    assert store_down_answer(vote(client, post))      # a vote needs its quota
    assert limits_on.onion_fallback.storage     # the onion's buckets were counted in memory meanwhile
    assert not limits_on.main.storage


def test_clearnet_login_unaffected_when_the_onion_store_is_down(app, world, dora, main_redis, onion_store_down,
                                                                monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', False)
    client = app.test_client()
    response = client.post('/auth/login', data={'user_name': 'dora', 'password': PASSWORD, 'timezone': 'UTC',
                                                'submit': 'Log In'},
                           environ_base={'REMOTE_ADDR': CLEARNET_IP})
    assert response.status_code == 302 and not response.headers['Location'].endswith('/auth/login')


def test_clearnet_rate_limit_keys_still_in_the_main_redis(limited_app, world, limits_on):
    client = limited_app.test_client()
    assert client.get('/auth/login', environ_base={'REMOTE_ADDR': CLEARNET_IP}).status_code == 200
    assert any(CLEARNET_IP in key for key in limits_on.main.storage)
    assert not limits_on.onion.storage
    onion_page(client, '/auth/login')
    assert any('onion' in key for key in limits_on.onion.storage)
    assert all('onion' not in key for key in limits_on.main.storage)


# --------------------------------------------------------------------------
# The parts
# --------------------------------------------------------------------------

def test_the_limiter_storages_are_the_two_redises():
    assert Config.RATELIMIT_STORAGE_URI == 'onion-routed://'
    assert Config.RATELIMIT_STORAGE_OPTIONS == {'main': limits_redis_uri(Config.CACHE_REDIS_URL),
                                               'onion': limits_redis_uri(Config.ONION_REDIS_URL)}
    assert limits_redis_uri('unix:///run/redis.sock?db=2') == 'redis+unix:///run/redis.sock?db=2'
    assert limits_redis_uri('redis://onion-redis:6379/0') == 'redis://onion-redis:6379/0'
    storage = storage_from_string('onion-routed://', main='redis://main-redis:6379/1', onion='redis://onion-redis:6379/0')
    assert isinstance(storage, OnionRoutedStorage)
    assert storage.main.storage.connection_pool.connection_kwargs['host'] == 'main-redis'
    assert storage.onion.storage.connection_pool.connection_kwargs['host'] == 'onion-redis'
    assert storage.base_exceptions is storage.main.base_exceptions


def test_the_routed_storage_outside_the_onion(app):
    storage = storage_from_string('onion-routed://', main='memory://', onion='memory://')
    with app.test_request_context('/'):
        assert storage.incr('k', 60) == 1
        assert storage.get('k') == 1 and storage.get_expiry('k') > 0
        storage.clear('k')
        assert storage.get('k') == 0
    assert storage.check()
    storage.main.incr('a', 60)
    storage.onion.incr('b', 60)
    storage.onion.incr('c', 60)
    assert storage.reset() == 3
    assert not storage.main.storage and not storage.onion.storage


def test_the_onion_fallback_is_made_once(app, onion):
    storage = storage_from_string('onion-routed://', main='memory://', onion='memory://')
    storage.onion = DownStorage()
    with app.test_request_context('/', base_url=ONION_URL, headers=MARKER,
                                  environ_base={'werkzeug.proxy_fix.orig': FROM_CADDY}):
        assert storage.incr('k', 60) == 1
        fallback = storage.onion_fallback
        assert storage.incr('k', 60) == 2 and storage.onion_fallback is fallback
    assert not storage.main.storage


def onion_request(app):
    return app.test_request_context('/', base_url=ONION_URL, headers=MARKER,
                                    environ_base={'werkzeug.proxy_fix.orig': FROM_CADDY})


def test_request_redis(app, onion, main_redis, onion_redis):
    assert request_redis() is main_redis                 # outside a request
    with app.test_request_context('/'):
        assert request_redis() is main_redis
    with onion_request(app):
        assert request_redis() is onion_redis


@pytest.mark.parametrize('guard', [onion_store_needed, onion_store_optional])
def test_clearnet_redis_errors_go_on_as_before(app, guard):
    with app.test_request_context('/'), pytest.raises(RedisConnectionError):
        with guard():
            raise RedisConnectionError('main down')


def test_the_optional_block_is_skipped_on_the_onion(app, onion):
    reached = []
    with onion_request(app):
        with onion_store_optional():
            raise RedisConnectionError('onion down')
        reached.append('after')
    assert reached == ['after']


def test_an_onion_settings_import_waits_in_the_onion_store(app, world, main_redis, onion_redis, monkeypatch):
    """The uploaded file waits in the onion store, and the import task, told only the key, finds it there."""
    client = app.test_client()
    with client.session_transaction(base_url=ONION_URL) as stored:
        stored['_user_id'] = str(world['alice'].id)
        stored['_fresh'] = True
    token = onion_page(client, '/user/settings/import_export').find('meta', attrs={'name': 'csrf-token'})['content']
    queued = []
    monkeypatch.setattr('app.user.routes.import_settings', queued.append)
    contents = json.dumps({'followed_communities': []}).encode()
    response = onion_post(client, '/user/settings/import_export', content_type='multipart/form-data',
                          data={'submit': 'Import', 'csrf_token': token, 'import_file': (io.BytesIO(contents), 'settings.json')})
    assert response.status_code == 302
    [key] = queued
    assert onion_redis.get(key) == contents.decode() and not main_redis.keys('import:*')
    import_settings_task(world['alice'].id, key)
    assert not onion_redis.exists(key)
