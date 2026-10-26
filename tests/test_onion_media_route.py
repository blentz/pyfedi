"""The `/onion_media` route and the Celery fill (onion media proxy spec 2026-10-19, sections 3.2 to 3.4 and 3.6).

The route answers from the disk cache with an `X-Accel-Redirect` that Caddy serves. On a miss an image is filled in
the request, at most two at once (a Redis semaphore), and video, audio and images beyond the two are queued to the
`onion_media` Celery queue with a 503. The fill itself is stubbed (`app.onion_media_routes.fill`), so no test reaches
the network; tests/test_onion_media_fill.py covers it. Redis is the `onion_redis` fakeredis. An onion request is one
from Caddy's address with the onion marker, as in tests/test_onion_pow.py.
"""
import hashlib
import logging
import os
import time

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

import app.onion_media_fill as onion_media_fill
import app.onion_media_routes as onion_media_routes
from app import celery, db
from app.models import SiteDomain
from app.onion_media import cache_name, proxy_url, url_kind
from app.onion_media_fill import BG_SLOT_KEY, DEAD_SECONDS, MAX_REQUEUES, fill_task, queue_fill
from app.onion_store import RefusedOnionStore
from app.storefront import clear_domain_cache
from tests.factories import make_banned_instance, make_instance, make_user

PROXY = '10.89.0.2'
ONION_URL = 'http://abc.onion'
MARKER = {'X-PieFed-Via': 'onion', 'X-Forwarded-For': '10.89.0.7'}
FROM_CADDY = {'REMOTE_ADDR': PROXY}
IMAGE = 'https://media.example/pictrs/image/a.png'
VIDEO = 'https://media.example/v/clip.mp4'
SANDBOX = "default-src 'none'; sandbox"


@pytest.fixture
def onion(app, site, monkeypatch, onion_redis, tmp_path):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    monkeypatch.setitem(app.config, 'MEDIA_PROXY_DIR', str(tmp_path))
    if not SiteDomain.query.filter_by(domain='test.piefed.local').first():
        db.session.add(SiteDomain(domain='test.piefed.local', scheme='https', is_default=True))
    db.session.add(SiteDomain(domain='abc.onion', scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield onion_redis
    clear_domain_cache()


@pytest.fixture
def queued(monkeypatch):
    """Every remote URL handed to the Celery fill task, in order."""
    calls = []
    monkeypatch.setattr(fill_task, 'delay', calls.append)
    return calls


@pytest.fixture
def no_fetch(monkeypatch):
    def fill(remote, *, max_seconds):
        raise AssertionError(f'fetched {remote}')
    monkeypatch.setattr(onion_media_routes, 'fill', fill)


@pytest.fixture
def fills(monkeypatch, tmp_path):
    """The in-request fill: writes the file as the real fill would and records (remote, max_seconds)."""
    calls = []

    def fill(remote, *, max_seconds):
        calls.append((remote, max_seconds))
        path = tmp_path / cache_name(remote, 'image/png')
        path.write_bytes(b'png')
        return path
    monkeypatch.setattr(onion_media_routes, 'fill', fill)
    return calls


def onion_get(client, path):
    return client.get(path, base_url=ONION_URL, headers=MARKER, environ_base=FROM_CADDY)


def assert_served(response, remote, content_type='image/png'):
    assert response.status_code == 200
    assert response.data == b''
    assert response.headers['X-Accel-Redirect'] == '/' + cache_name(remote, content_type)
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Content-Security-Policy'] == SANDBOX


def _flip(text):
    """`text` with its first character changed (a base64 key's last character can carry only padding bits)."""
    return ('b' if text[0] != 'b' else 'c') + text[1:]


def test_url_kind(app):
    assert url_kind('https://a.example/x/photo.JPG') == 'image'
    assert url_kind('https://a.example/x/anim.webp?w=1') == 'image'
    assert url_kind('https://a.example/pictrs/image/0b1c2d') == 'image'
    assert url_kind('https://a.example/v/clip.mp4') == 'av'
    assert url_kind('https://a.example/a/song.FLAC#t=1') == 'av'
    assert url_kind('https://a.example/a/track.weba') == 'av'
    assert url_kind('https://a.example/x/photo.jpeg') == 'image'       # Mastodon's spelling
    assert url_kind('https://a.example/x/photo.JFIF') == 'image'
    assert url_kind('https://a.example/v/clip.mov') == 'av'
    assert url_kind('https://a.example/v/clip.m4v') == 'av'
    assert url_kind('https://a.example/a/voice.opus') == 'av'
    assert url_kind('https://a.example/a/song.aac') == 'av'
    assert url_kind('https://a.example/media/12345') == 'unknown'
    assert url_kind('https://a.example/page.html') == 'unknown'
    assert url_kind('https://a.example/clip.mp4.html') == 'unknown'
    assert url_kind('https://a.example/?f=clip.mp4') == 'unknown'


def test_clearnet_and_bad_signature_404_without_fetch(app, onion, no_fetch, queued, tmp_path):
    client = app.test_client()
    url = proxy_url(IMAGE)
    (tmp_path / cache_name(IMAGE, 'image/png')).write_bytes(b'png')    # even a cached file is not served on clearnet
    assert client.get(url).status_code == 404
    sig, key = url.split('/')[2:4]
    assert onion_get(client, f'/onion_media/{_flip(sig)}/{key}').status_code == 404
    assert onion_get(client, f'/onion_media/{sig}/{_flip(key)}').status_code == 404
    assert onion_get(client, f'/onion_media/{sig}/_w').status_code == 404
    assert onion_get(client, proxy_url(VIDEO)).status_code == 503    # a good signature on the onion does reach the cache
    assert queued == [VIDEO]


def test_hit_answers_accel_redirect_and_refreshes_mtime(app, onion, no_fetch, queued, tmp_path):
    path = tmp_path / cache_name(IMAGE, 'image/png')
    path.write_bytes(b'png')
    os.utime(path, (1_000_000, 1_000_000))
    response = onion_get(app.test_client(), proxy_url(IMAGE))
    assert_served(response, IMAGE)
    assert response.mimetype != 'text/html'          # so the onion HTML rewrite and the page headers leave it alone
    assert path.stat().st_mtime > 1_000_000
    assert queued == []


def test_a_hit_within_the_hour_leaves_the_mtime_so_the_browser_can_revalidate(app, onion, no_fetch, queued, tmp_path):
    """Caddy builds the ETag and Last-Modified from the mtime: touching it on every hit made every revalidation a full
    download. Eviction runs every 15 minutes, so an hour's precision is enough for least recently used."""
    path = tmp_path / cache_name(IMAGE, 'image/png')
    path.write_bytes(b'png')
    recent = time.time() - 3500
    os.utime(path, (recent, recent))
    assert_served(onion_get(app.test_client(), proxy_url(IMAGE)), IMAGE)
    assert path.stat().st_mtime == recent
    stale = time.time() - 3700
    os.utime(path, (stale, stale))
    assert_served(onion_get(app.test_client(), proxy_url(IMAGE)), IMAGE)
    assert path.stat().st_mtime > time.time() - 60


def test_a_hit_on_a_file_evicted_as_it_is_found_still_answers(app, onion, no_fetch, monkeypatch, tmp_path):
    path = tmp_path / cache_name(IMAGE, 'image/png')
    path.write_bytes(b'png')
    monkeypatch.setattr(onion_media_routes, 'cached_file', lambda remote: tmp_path / 'gone.png')
    response = onion_get(app.test_client(), proxy_url(IMAGE))
    assert response.status_code == 200 and response.headers['X-Accel-Redirect'] == '/gone.png'   # Caddy answers 404


def test_hit_keeps_its_sandbox_csp_for_a_signed_in_viewer(app, onion, no_fetch, tmp_path):
    make_instance('test.piefed.local', software='piefed')    # user.instance_id FK target
    user = make_user(None, 'viewer', local=True)
    client = app.test_client()
    with client.session_transaction(base_url=ONION_URL) as session:
        session['_user_id'] = str(user.id)
    (tmp_path / cache_name(VIDEO, 'video/mp4')).write_bytes(b'mp4')
    assert_served(onion_get(client, proxy_url(VIDEO)), VIDEO, 'video/mp4')


def test_image_miss_fills_in_request(app, onion, fills, queued):
    response = onion_get(app.test_client(), proxy_url(IMAGE))
    assert_served(response, IMAGE)
    assert fills == [(IMAGE, 15)]
    assert queued == []
    assert onion.keys('onion_media:inflight:*') == []        # the slot is given back


def test_unknown_extension_miss_fills_in_request_as_an_image(app, onion, fills, queued):
    remote = 'https://media.example/media/12345'
    assert_served(onion_get(app.test_client(), proxy_url(remote)), remote)
    assert fills == [(remote, 15)]


def test_image_fill_that_fails_is_404_and_gives_back_its_slot(app, onion, monkeypatch, queued):
    monkeypatch.setattr(onion_media_routes, 'fill', lambda remote, *, max_seconds: None)
    assert onion_get(app.test_client(), proxy_url(IMAGE)).status_code == 404
    assert onion.keys('onion_media:inflight:*') == []
    assert queued == []


def test_slot_is_given_back_when_the_fill_raises(app, onion, monkeypatch):
    def fill(remote, *, max_seconds):
        raise RuntimeError('boom')
    monkeypatch.setattr(onion_media_routes, 'fill', fill)
    monkeypatch.setitem(app.config, 'PROPAGATE_EXCEPTIONS', True)
    with pytest.raises(RuntimeError):
        onion_get(app.test_client(), proxy_url(IMAGE))
    assert onion.keys('onion_media:inflight:*') == []


def test_a_slot_retaken_after_it_expired_is_not_given_back(app, onion, monkeypatch, tmp_path):
    """A fill that outlived its slot's 20 s leaves the slot to whichever request took it since."""
    def fill(remote, *, max_seconds):
        onion.set('onion_media:inflight:0', 'another request')
        path = tmp_path / cache_name(remote, 'image/png')
        path.write_bytes(b'png')
        return path
    monkeypatch.setattr(onion_media_routes, 'fill', fill)
    assert_served(onion_get(app.test_client(), proxy_url(IMAGE)), IMAGE)
    assert onion.get('onion_media:inflight:0') == 'another request'


def test_an_image_fill_takes_a_twenty_second_slot(app, onion, monkeypatch, tmp_path):
    seen = {}

    def fill(remote, *, max_seconds):
        seen.update({key: onion.ttl(key) for key in onion.keys('onion_media:inflight:*')})
        return None
    monkeypatch.setattr(onion_media_routes, 'fill', fill)
    onion_get(app.test_client(), proxy_url(IMAGE))
    assert seen == {'onion_media:inflight:0': 20}


def test_third_concurrent_image_miss_is_queued_503(app, onion, no_fetch, queued):
    onion.set('onion_media:inflight:0', 'one', ex=20)
    onion.set('onion_media:inflight:1', 'two', ex=20)
    client = app.test_client()
    for _ in range(2):
        response = onion_get(client, proxy_url(IMAGE))
        assert response.status_code == 503
        assert response.headers['Retry-After'] == '10'
        assert response.mimetype != 'text/html'
    assert queued == [IMAGE]                                    # requested twice, queued once
    assert onion.get('onion_media:inflight:0') == 'one' and onion.get('onion_media:inflight:1') == 'two'


def test_video_miss_is_queued_never_fetched_in_request(app, onion, no_fetch, queued):
    client = app.test_client()
    audio = 'https://media.example/a/song.ogg'
    for remote in (VIDEO, VIDEO, audio):
        response = onion_get(client, proxy_url(remote))
        assert response.status_code == 503
        assert response.headers['Retry-After'] == '30'
    assert queued == [VIDEO, audio]
    assert onion.keys('onion_media:inflight:*') == []


def test_hit_on_an_instance_banned_since_it_was_cached_is_404(app, onion, no_fetch, queued, tmp_path):
    other = 'https://Other.example/b.png'
    for remote in (IMAGE, other):
        (tmp_path / cache_name(remote, 'image/png')).write_bytes(b'png')
    make_banned_instance('media.example')
    client = app.test_client()
    assert onion_get(client, proxy_url(IMAGE)).status_code == 404
    assert_served(onion_get(client, proxy_url(other)), other)
    assert queued == []


def test_image_miss_whose_host_is_busy_is_queued_503_and_another_host_fills(app, onion, fills, queued, monkeypatch):
    onion.set(HOST, 'a background fill', ex=630)
    client = app.test_client()
    response = onion_get(client, proxy_url(IMAGE))
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '10'
    assert queued == [IMAGE]
    assert fills == []
    assert onion.keys('onion_media:inflight:*') == []           # the in-request slot it took is given back
    other = 'https://Other.example/b.png'
    seen = {}
    original = onion_media_routes.fill

    def fill(remote, *, max_seconds):
        seen.update({key: onion.ttl(key) for key in onion.keys('onion_media:host:*')})
        return original(remote, max_seconds=max_seconds)
    monkeypatch.setattr(onion_media_routes, 'fill', fill)
    assert_served(onion_get(client, proxy_url(other)), other)
    assert seen == {HOST: onion.ttl(HOST), 'onion_media:host:other.example': 45}    # the host key is lowercase
    assert onion.keys('onion_media:host:other.example') == []
    assert fills == [(other, 15)]


def test_dead_url_404(app, onion, no_fetch, queued, tmp_path):
    onion.set('onion_media:dead:' + hashlib.sha256(IMAGE.encode()).hexdigest(), '1', ex=DEAD_SECONDS)
    (tmp_path / cache_name(IMAGE, 'image/png')).write_bytes(b'png')
    assert onion_get(app.test_client(), proxy_url(IMAGE)).status_code == 404
    assert queued == []


def test_queue_fill_de_duplicates_for_ten_minutes(app, onion, queued):
    queue_fill(VIDEO)
    queue_fill(VIDEO)
    assert queued == [VIDEO]
    key = 'onion_media:queued:' + hashlib.sha256(VIDEO.encode()).hexdigest()
    assert 590 < onion.ttl(key) <= 600


HOST = 'onion_media:host:media.example'


@pytest.fixture
def requeued(monkeypatch):
    """Every (args, countdown) fill_task queued itself again with."""
    calls = []
    monkeypatch.setattr(fill_task, 'apply_async', lambda args, countdown: calls.append((args, countdown)))
    return calls


@pytest.fixture
def background(monkeypatch):
    """The fill as fill_task calls it: records (remote, max_seconds) and the slots held while it runs."""
    calls = []

    def fill(remote, *, max_seconds):
        held = {key: onion_media_fill.app_pkg.onion_redis_client.ttl(key)
                for key in onion_media_fill.app_pkg.onion_redis_client.keys('onion_media:*')
                if not key.startswith('onion_media:queued:')}
        calls.append((remote, max_seconds, held))
    monkeypatch.setattr(onion_media_fill, 'fill', fill)
    return calls


def test_fill_task_fills_video_with_a_ten_minute_deadline_and_an_image_with_one_minute(app, onion_redis, background,
                                                                                       requeued):
    assert BG_SLOT_KEY == 'onion_media:bg_inflight'
    fill_task(VIDEO)
    assert background == [(VIDEO, 600, {BG_SLOT_KEY: 630, HOST: 630})]
    fill_task(IMAGE)                                            # an image holds the one background slot too
    assert background[1] == (IMAGE, 60, {BG_SLOT_KEY: 90, HOST: 90})
    assert onion_redis.keys('*') == []                         # every slot given back
    assert requeued == []


def test_a_task_while_the_background_slot_is_held_requeues_without_fetching(app, onion_redis, background, requeued,
                                                                          queued):
    """At most one of the background worker's slots does onion fills, image or not, so other background work runs."""
    queue_fill(VIDEO)
    onion_redis.set(BG_SLOT_KEY, 'another task', ex=630)
    onion_redis.expire('onion_media:queued:' + hashlib.sha256(VIDEO.encode()).hexdigest(), 5)
    other = 'https://other.example/a/song.mp3'
    for remote in (VIDEO, other, 'https://media.example/media/12345', IMAGE):
        fill_task(remote)
    assert background == []
    assert requeued == [((VIDEO, 1), 30), ((other, 1), 30), (('https://media.example/media/12345', 1), 30),
                        ((IMAGE, 1), 30)]
    assert onion_redis.ttl('onion_media:queued:' + hashlib.sha256(VIDEO.encode()).hexdigest()) > 590   # still de-duped
    assert onion_redis.get(BG_SLOT_KEY) == 'another task'
    assert onion_redis.keys('onion_media:host:*') == []
    assert queued == [VIDEO]


def test_a_task_requeued_twenty_times_is_dropped_and_its_queued_key_left_to_lapse(app, onion_redis, background,
                                                                                requeued):
    queued_key = 'onion_media:queued:' + hashlib.sha256(VIDEO.encode()).hexdigest()
    onion_redis.set(BG_SLOT_KEY, 'another task', ex=630)
    assert MAX_REQUEUES == 20
    for attempt in range(MAX_REQUEUES):
        onion_redis.delete(queued_key)                         # lapsed: each re-queue sets it again
        fill_task(VIDEO, attempt)
        assert requeued[-1] == ((VIDEO, attempt + 1), 30)
        assert 590 < onion_redis.ttl(queued_key) <= 600
    onion_redis.expire(queued_key, 5)
    fill_task(VIDEO, MAX_REQUEUES)                              # the 21st run
    assert len(requeued) == MAX_REQUEUES
    assert background == []
    assert onion_redis.ttl(queued_key) <= 5                    # left to lapse, so the next view queues it again
    assert not onion_media_fill.is_dead(VIDEO)


def test_background_fill_whose_host_is_busy_requeues_and_another_host_proceeds(app, onion_redis, background,
                                                                              requeued):
    onion_redis.set(HOST, 'a request', ex=45)
    fill_task(IMAGE)
    fill_task(VIDEO)                                            # the background slot each took is given back
    assert requeued == [((IMAGE, 1), 30), ((VIDEO, 1), 30)]
    assert background == []
    assert onion_redis.get(BG_SLOT_KEY) is None
    other = 'https://other.example/b.png'
    fill_task(other)
    assert background == [(other, 60, {HOST: onion_redis.ttl(HOST), BG_SLOT_KEY: 90,
                                        'onion_media:host:other.example': 90})]


def test_fill_task_routes_to_onion_media_queue(app):
    assert fill_task.name == 'app.onion_media_fill.fill_task'
    assert celery.conf.CELERY_ROUTES[fill_task.name] == {'queue': 'onion_media'}


@pytest.fixture(params=['down', 'refused'])
def store_down(request, monkeypatch):
    """The onion store unreachable: a Redis that raises, or the RefusedOnionStore a failed startup check installs."""
    if request.param == 'refused':
        store = RefusedOnionStore()
    else:
        class Down:
            def __getattr__(self, name):
                def fail(*args, **kwargs):
                    raise RedisConnectionError('down')
                return fail
        store = Down()
    monkeypatch.setattr('app.onion_redis_client', store)
    return store


@pytest.mark.parametrize('remote', [IMAGE, VIDEO])
def test_a_miss_with_the_onion_store_down_is_404_not_500_and_logs_no_url(app, onion, store_down, no_fetch, queued,
                                                                          caplog, remote):
    with caplog.at_level(logging.DEBUG):
        response = onion_get(app.test_client(), proxy_url(remote))
    assert response.status_code == 404
    assert queued == []
    assert 'media.example' not in caplog.text
    assert remote not in caplog.text
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_give_back_with_the_onion_store_down_is_silent_and_takes_nothing(app, store_down):
    assert onion_media_fill.take_slot('onion_media:inflight:0', 20) is None
    onion_media_fill.give_back('onion_media:inflight:0', 'token')


def test_a_fill_that_raises_with_the_store_down_still_frees_what_it_can(app, onion, monkeypatch, caplog):
    """The slots were taken, then the store went down mid-fill: give_back must not turn the 404 into a 500."""
    taken = []

    def fill(remote, *, max_seconds):
        monkeypatch.setattr('app.onion_redis_client', RefusedOnionStore())
        taken.append(remote)
        return None
    monkeypatch.setattr(onion_media_routes, 'fill', fill)
    assert onion_get(app.test_client(), proxy_url(IMAGE)).status_code == 404
    assert taken == [IMAGE]
