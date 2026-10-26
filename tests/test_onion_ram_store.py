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
import socket
import time

import fakeredis
import pytest
from fakeredis._socket._base import BaseFakeSocket
from flask import Flask
from limits.storage import MemoryStorage, storage_from_string
from redis.exceptions import ConnectionError as RedisConnectionError, RedisError, TimeoutError as RedisTimeoutError

import app as app_pkg
from app import create_app, db, limiter
from app.models import Post, User, count_vote_cast, recent_ai_detections, votes_cast_key, votes_cast_today
from app.onion import onion_user_ttl
from app.onion_store import (OnionRoutedStorage, check_onion_store, onion_store_needed, onion_store_optional,
                             request_redis)
from app.user.routes import import_settings_task
from app.utils import get_deduped_post_ids, get_redis_connection, set_setting
from config import Config, limits_redis_uri
from tests.conftest import TestConfig
from tests.factories import make_community, make_post
from tests.test_onion_pow import (FROM_CADDY, MARKER, ONION_URL, PASSWORD, PROXY, alice, challenge, dora,  # noqa: F401
                                  login_data, onion, onion_get, onion_page, onion_post, open_registration,
                                  register_data)

CLEARNET_IP = '198.51.100.23'
# The Celery broker's own keys, the spec's one exception: its queues (app/__init__.py CELERY_ROUTES), kombu's
# bindings and the unacked set. Not `celery-task-meta-*`: a result is not a broker message.
BROKER_QUEUES = {'celery', 'background', 'send', 'onion_media'}
BROKER_PREFIXES = ('_kombu.', 'unacked')
# The other accepted exception: redis-py locks on rows clearnet and Celery lock too, released within the request.
LOCK_PREFIXES = ('lock:post:', 'lock:user:', 'lock:post_reply:', 'lock:vote:', 'lock:community:')
# Commands that change no key. Everything else sent to the main Redis is counted as a write to its first argument.
READ_ONLY = {'GET', 'MGET', 'EXISTS', 'TTL', 'PTTL', 'TYPE', 'KEYS', 'SCAN', 'HGET', 'HMGET', 'HGETALL', 'HLEN',
             'HEXISTS', 'ZRANGE', 'ZRANGEBYSCORE', 'ZCOUNT', 'ZCARD', 'ZSCORE', 'SMEMBERS', 'SISMEMBER', 'SCARD',
             'LRANGE', 'LLEN', 'STRLEN', 'DBSIZE', 'PING', 'INFO', 'MEMORY', 'MULTI', 'EXEC', 'DISCARD', 'WATCH',
             'UNWATCH', 'SELECT', 'CLIENT', 'HELLO', 'PUBLISH'}


def is_broker_key(key: str) -> bool:
    return key in BROKER_QUEUES or key.startswith(BROKER_PREFIXES)


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


@pytest.fixture
def main_writes(main_redis, monkeypatch):
    """Every write command the main Redis receives, as (command, key): each command as it reaches the fake server,
    so one inside a pipeline or MULTI counts, and so does a key written and deleted again before the test looks."""
    connection = main_redis.connection_pool.get_connection()
    server = connection._server
    main_redis.connection_pool.release(connection)
    writes = []
    process = BaseFakeSocket._process_command

    def recording(socket, fields):
        if socket._server is server and fields:
            command = fields[0].decode().upper()
            if command not in READ_ONLY:
                writes.append((command, fields[1].decode() if len(fields) > 1 else None))
        return process(socket, fields)

    monkeypatch.setattr(BaseFakeSocket, '_process_command', recording)
    return writes


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
    """Every key outside the Celery broker's, with its type, value and TTL: what an onion request must leave as it
    was. A TTL counts down meanwhile, so `unchanged` compares it to the second, with a little slack."""
    snapshot = {}
    for key in redis.keys('*'):
        if is_broker_key(key):
            continue
        kind = redis.type(key)
        value = {'string': redis.get, 'hash': redis.hgetall, 'zset': lambda k: redis.zrange(k, 0, -1, withscores=True),
                 'set': redis.smembers, 'list': lambda k: redis.lrange(k, 0, -1)}[kind](key)
        snapshot[key] = (kind, value, redis.ttl(key))
    return snapshot


def unchanged(before, after, slack=30):
    assert {key: entry[:2] for key, entry in after.items()} == {key: entry[:2] for key, entry in before.items()}
    for key, (_, _, ttl) in before.items():
        assert ttl - slack <= after[key][2] <= ttl if ttl >= 0 else after[key][2] == ttl, key
    return True


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


def test_onion_crawl_writes_nothing_to_the_main_redis(limited_app, world, main_redis, main_writes, onion_redis,
                                                      limits_on):
    alice = world['alice']
    main_redis.set(votes_cast_key(alice.id), 7, ex=5000)                        # her clearnet votes today
    main_redis.set(f'feed:test.piefed.local:{alice.id}:abc', '[]', ex=4000)    # her clearnet feed cache
    before = state(main_redis)
    del main_writes[:]
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

    assert unchanged(before, state(main_redis))
    written = {key for _, key in main_writes}
    assert {key for key in written if not (is_broker_key(key) or key.startswith(LOCK_PREFIXES))} == set(), main_writes
    assert any(key.startswith('lock:user:') for key in written)      # the recorder saw the vote's lock come and go
    db.session.expire_all()
    assert db.session.get(Post, post.id).up_votes == 1
    assert onion_redis.get(next(iter(onion_redis.keys('votes_cast_*')))) == '1'
    assert onion_redis.keys('onion_pow_used:*') and onion_redis.keys('feed:*')
    assert limits_on.onion.storage and not limits_on.main.storage
    fresh = limited_app.test_client()                                              # a failed login keeps a counter
    page = challenge(onion_page(fresh, '/auth/login'))
    onion_post(fresh, '/auth/login', data=proven(page, password='wrong password'))
    assert_no_user_key_outlives_the_hour(onion_redis, alice)


def assert_no_user_key_outlives_the_hour(onion_redis, user):
    """The spec's anti-goal: a running box holds no link between a user and onion use older than an hour. Every key
    of the onion store that names the account (by id, name or email) has a TTL of 1 to 3600 seconds, never -1."""
    named = [key for key in onion_redis.keys('*')
             if any(token in key for token in (f':{user.id}', f'_{user.id}', user.user_name, user.email))]
    assert {key.split(':')[0] for key in named} >= {'feed', 'onion_login_fail'}      # the scan found the keys it is for
    assert [(key, onion_redis.ttl(key)) for key in named if not 0 < onion_redis.ttl(key) <= 3600] == []
    assert any(key.startswith('votes_cast_') for key in named)


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
    assert {name: Config.RATELIMIT_STORAGE_OPTIONS[name] for name in ('main', 'onion')} == {
        'main': limits_redis_uri(Config.CACHE_REDIS_URL), 'onion': limits_redis_uri(Config.ONION_REDIS_URL)}
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


# --------------------------------------------------------------------------
# Per-user tallies count both paths (security review ruling)
# --------------------------------------------------------------------------

def test_vote_quota_counts_both_paths(app, limited_app, world, main_redis, main_writes, onion_redis, monkeypatch):
    """One account's votes are counted where each was cast, but its quota is the sum: voting on the other path is no
    way round it. An onion vote is still counted only in the onion store, and clearnet works with that store down."""
    alice, post = world['alice'], world['post']
    key = votes_cast_key(alice.id)
    main_redis.set(key, 3, ex=5000)
    onion_redis.set(key, 4, ex=5000)
    with app.test_request_context('/'):
        assert votes_cast_today(alice.id) == 7
    with onion_request(app):
        assert votes_cast_today(alice.id) == 7
    monkeypatch.setitem(limited_app.config, 'VOTE_QUOTA', 7)
    client = limited_app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    assert logged_in(onion_post(client, '/auth/login', data=proven(page)))
    assert vote(client, post).status_code == 429                  # 3 on clearnet + 4 on the onion: the quota is spent
    monkeypatch.setitem(limited_app.config, 'VOTE_QUOTA', 8)
    del main_writes[:]
    assert vote(client, post).status_code == 200
    assert onion_redis.get(key) == '5' and main_redis.get(key) == '3'
    assert not [write for write in main_writes if write[1] == key]
    monkeypatch.setattr('app.onion_redis_client', down())
    with app.test_request_context('/'):
        assert votes_cast_today(alice.id) == 3                    # the onion's count is skipped, not an error


def test_the_other_store_is_not_read_without_an_onion(app, main_redis, onion_redis, monkeypatch):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', None)
    onion_redis.set(votes_cast_key(5), 4)
    main_redis.set(votes_cast_key(5), 1)
    with app.test_request_context('/'):
        assert votes_cast_today(5) == 1


def test_ai_detection_tally_merges_both_paths(app, world, main_redis, main_writes, onion_redis, monkeypatch):
    alice = world['alice']
    key = f'ai_detection:user:{alice.id}'
    now = 1_800_000_000.0
    main_redis.zadd(key, {json.dumps({'detection': 'ai', 'n': 1}): now - 100,
                          json.dumps({'detection': 'ai', 'n': 2}): now - 50,
                          json.dumps({'detection': 'human', 'n': 0}): now - 90_000})    # older than a day
    before = state(main_redis)
    del main_writes[:]
    with onion_request(app):
        detections = recent_ai_detections(alice.id, {'detection': 'ai', 'n': 3}, now)
    assert sorted(json.loads(d)['n'] for d in detections) == [1, 2, 3]
    assert unchanged(before, state(main_redis)) and main_writes == []
    assert len(onion_redis.zrange(key, 0, -1)) == 1               # the onion's own entry, only there
    with app.test_request_context('/'):
        detections = recent_ai_detections(alice.id, {'detection': 'human', 'n': 4}, now)
    assert sorted(json.loads(d)['n'] for d in detections) == [1, 2, 3, 4]
    monkeypatch.setattr('app.onion_redis_client', down())
    with app.test_request_context('/'):
        detections = recent_ai_detections(alice.id, {'detection': 'human', 'n': 5}, now)
    assert sorted(json.loads(d)['n'] for d in detections) == [1, 2, 4, 5]


# --------------------------------------------------------------------------
# The startup check
# --------------------------------------------------------------------------

def a_config(**values):
    checked = Flask('checked')
    checked.config.update({'ONION_PROXY_IP': PROXY, 'CACHE_TYPE': 'FileSystemCache',
                           'CACHE_REDIS_URL': 'redis://redis:6379/1', 'CELERY_BROKER_URL': 'redis://redis:6379/0',
                           'ONION_REDIS_URL': 'redis://onion-redis:6379/0', **values})
    return checked


@pytest.mark.parametrize('onion_url', ['redis://redis:6379/2', 'redis://REDIS/5', 'redis://:secret@redis:6379/2'])
def test_an_onion_store_on_the_main_redis_is_refused(onion_url, caplog):
    assert not check_onion_store(a_config(ONION_REDIS_URL=onion_url))
    assert [record.levelname for record in caplog.records] == ['ERROR']
    assert 'ONION_REDIS_URL' in caplog.text


@pytest.mark.parametrize('values', [
    {'ONION_REDIS_URL': 'redis://localhost:6379/2', 'CACHE_REDIS_URL': 'redis://127.0.0.1:6379/1'},
    {'ONION_REDIS_URL': 'unix:///run/redis/redis.sock?db=2', 'CACHE_REDIS_URL': 'unix:///run/redis/redis.sock?db=1'},
    {'ONION_REDIS_URL': 'redis://broker:6379/3', 'CELERY_BROKER_URL': 'redis://broker:6379/0'},
])
def test_the_main_redis_is_recognised_however_it_is_written(values):
    assert not check_onion_store(a_config(**values))


@pytest.mark.parametrize('values', [
    {},                                                               # its own Redis
    {'ONION_REDIS_URL': 'redis://redis:6380/0'},                      # same host, another server
    {'ONION_REDIS_URL': 'unix:///run/onion-redis.sock', 'CACHE_REDIS_URL': 'unix:///run/redis.sock'},
    {'ONION_PROXY_IP': None, 'ONION_REDIS_URL': 'redis://redis:6379/2'},   # no onion: nothing to protect
])
def test_a_separate_onion_store_passes(values, caplog):
    assert check_onion_store(a_config(**values))
    assert caplog.records == []


@pytest.mark.parametrize('cache_type', ['RedisCache', 'flask_caching.backends.RedisSentinelCache', 'redis'])
def test_a_redis_flask_cache_beside_an_onion_is_warned(cache_type, caplog):
    assert check_onion_store(a_config(CACHE_TYPE=cache_type))       # a warning, not a refusal
    assert [record.levelname for record in caplog.records] == ['WARNING']
    assert 'CACHE_TYPE' in caplog.text
    caplog.clear()
    assert check_onion_store(a_config(CACHE_TYPE=cache_type, ONION_PROXY_IP=None)) and caplog.records == []


class SharedConfig(TestConfig):
    ONION_PROXY_IP = PROXY
    ONION_REDIS_URL = TestConfig.CACHE_REDIS_URL.rpartition('/')[0] + '/9'


def test_a_refused_onion_store_fails_closed(app, world, main_redis, caplog):
    """Startup goes on, and the onion behaves as if its store were down: guarded actions 503, buckets in memory."""
    refused = create_app(SharedConfig)
    assert 'ONION_REDIS_URL' in caplog.text
    assert refused.config['RATELIMIT_STORAGE_OPTIONS']['onion'] == 'memory://'
    assert TestConfig.RATELIMIT_STORAGE_OPTIONS['onion'] == 'memory://' and \
        Config.RATELIMIT_STORAGE_OPTIONS['onion'] != 'memory://'      # a copy was changed, not the class's
    with pytest.raises(RedisError):
        app_pkg.onion_redis_client.get('anything')
    client = app.test_client()
    page = challenge(onion_page(client, '/auth/login'))
    assert store_down_answer(onion_post(client, '/auth/login', data=proven(page)))
    assert main_redis.keys('*') == []


# --------------------------------------------------------------------------
# A packet-dropping onion host cannot hang the onion's pages
# --------------------------------------------------------------------------

def test_both_onion_clients_time_out():
    timeouts = {'socket_connect_timeout': Config.ONION_REDIS_TIMEOUT, 'socket_timeout': Config.ONION_REDIS_TIMEOUT}
    assert Config.ONION_REDIS_TIMEOUT == 0.5
    assert Config.RATELIMIT_STORAGE_OPTIONS['onion_options'] == timeouts
    storage = storage_from_string('onion-routed://', **Config.RATELIMIT_STORAGE_OPTIONS)
    kwargs = storage.onion.storage.connection_pool.connection_kwargs
    assert {name: kwargs[name] for name in timeouts} == timeouts
    assert storage.main.storage.connection_pool.connection_kwargs.get('socket_timeout') is None   # main: as before
    create_app(TestConfig)
    kwargs = app_pkg.onion_redis_client.connection_pool.connection_kwargs
    assert {name: kwargs[name] for name in timeouts} == timeouts
    assert app_pkg.redis_client.connection_pool.connection_kwargs.get('socket_timeout') is None


def test_an_onion_store_that_never_answers_times_out():
    """A server that takes the connection and never replies: the onion client gives up after its timeout."""
    with socket.create_server(('127.0.0.1', 0)) as silent:
        port = silent.getsockname()[1]
        client = get_redis_connection(f'redis://127.0.0.1:{port}/0', socket_connect_timeout=0.5, socket_timeout=0.5)
        started = time.monotonic()
        with pytest.raises(RedisTimeoutError):
            client.get('anything')
        assert time.monotonic() - started < 2


# --------------------------------------------------------------------------
# No key that names a user lives on the onion store for more than an hour
# --------------------------------------------------------------------------

def test_an_onion_vote_count_expires_within_the_hour_and_a_clearnet_one_keeps_its_day(app, onion, main_redis,
                                                                                      onion_redis):
    key = votes_cast_key(5)
    with onion_request(app):
        count_vote_cast(5)
        count_vote_cast(5)                      # a later vote does not extend it
    assert 0 < onion_redis.ttl(key) <= 3600 and onion_redis.get(key) == '2'
    with app.test_request_context('/'):
        count_vote_cast(5)
    assert 86000 < main_redis.ttl(key) <= 86400


def test_an_onion_ai_detection_tally_expires_within_the_hour_and_a_clearnet_one_is_as_it_was(app, onion, main_redis,
                                                                                            onion_redis):
    key = 'ai_detection:user:5'
    now = 1_800_000_000.0
    with onion_request(app):
        recent_ai_detections(5, {'detection': 'ai'}, now)
        recent_ai_detections(5, {'detection': 'ai'}, now + 1)           # a later one does not extend it
    assert 0 < onion_redis.ttl(key) <= 3600
    with app.test_request_context('/'):
        recent_ai_detections(5, {'detection': 'ai'}, now)
    assert main_redis.ttl(key) == -1


def test_an_onion_feed_cache_expires_within_the_hour_and_a_clearnet_one_keeps_its_day(app, world, main_redis,
                                                                                     onion_redis, monkeypatch):
    monkeypatch.setattr('app.utils.current_user', world['alice'])
    community_ids = [world['community'].id]
    with onion_request(app):
        get_deduped_post_ids('abc', community_ids, 'hot')
    assert [0 < onion_redis.ttl(key) <= 3600 for key in onion_redis.keys('feed:*')] == [True]
    with app.test_request_context('/'):
        get_deduped_post_ids('abc', community_ids, 'hot')
    assert [86000 < main_redis.ttl(key) <= 86400 for key in main_redis.keys('feed:*')] == [True]


def test_the_onion_store_names_a_user_only_for_an_hour(app, onion):
    """The TTL helper itself: capped on the onion, the caller's own anywhere else."""
    with onion_request(app):
        assert onion_user_ttl(86400) == 3600 and onion_user_ttl(600) == 600
    with app.test_request_context('/'):
        assert onion_user_ttl(86400) == 86400
