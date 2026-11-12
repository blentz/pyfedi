"""The client-app harness's PieFed world, made through PieFed's own API on the fresh PieFed each run starts with
(run.sh's fresh_piefed): dora's community `apps` with a post every app must show and a photo, which alice (the account
every app logs in as) has joined; tess's video on tube.test; the podcast episode in pod_media.

Owner ruling H1 (2026-11-12): every test starts from the same state and leaves nothing behind. Once the world is made,
`baseline` snapshots PieFed (stack_state.py); `clean_state`, around every test, restores it before the test and again in
its teardown, and checks the restore. On the emulator, `device` takes away after each test every app installed since
the session began, Chrome's data and the media a driver pushed."""
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
import ssl
import struct
import time
import zlib

import httpx
import pytest

from interop.apps import stack_state
from interop.apps.android_device import Device
from interop.apps.matrix import RESULTS
from interop.clients import CA, PieFed, PieFedWeb, form_errors, wait_for

ALICE = ('alice', 'interop-alice-pw')
DORA = ('dora', 'interop-dora-pw')
COMMUNITY = 'apps'
SEED_TITLE = 'Interop apps: a post every app must show'
# A photo post of dora's, for the photo apps (Pixelfed's shows a profile's photos alone, by pf_type: plan W1.4a)
PHOTO_TITLE = 'Interop apps photo'
# The public domain apps with no account browse (piefed.test is private, R219), its uploader and its video
TUBE, TESS = 'tube.test', ('tess', 'interop-tess-pw')
TUBE_TITLE = 'Interop apps video'
VIDEO_FIXTURE = '/app/tests/fixtures/media/video_720p.mp4'   # 720p: it gains 480p and 360p renditions (W2.1b)
# The episode podcast players subscribe to, in alice's podcast community pod_media
EPISODE = 'Interop apps episode'
EPISODE_FIXTURE = '/app/tests/fixtures/media/audio.mp3'
NS = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}
RUN = os.environ.get('APPS_RUN_ID') or time.strftime('%Y%m%d%H%M%S')


def _community(piefed):
    found = piefed.http.get('/community', params={'name': COMMUNITY})
    return found.json()['community_view']['community'] if found.status_code == 200 else None


@pytest.fixture(scope='session')
def world():
    alice, dora = PieFed(*ALICE), PieFed(*DORA)
    dora.http.post('/community', json={'name': COMMUNITY, 'title': 'Interop apps',
                                       'description': 'Third-party app checks'}).raise_for_status()
    community = wait_for(lambda: _community(dora))
    seed = dora.post(SEED_TITLE, 'Seeded by interop/apps/conftest.py.', community['id'])['post']
    uploaded = dora.http.post('/upload/image', files={'file': ('photo.png', png(), 'image/png')})
    photo = dora.post(PHOTO_TITLE, '', community['id'], url=uploaded.raise_for_status().json()['url'])['post']
    alice.http.post('/community/follow', json={'community_id': community['id'], 'follow': True}).raise_for_status()
    return {'alice': alice, 'dora': dora, 'community': community, 'seed': seed, 'photo': photo}


@pytest.fixture(scope='session')
def baseline(world):
    """PieFed once the session's world is made, the video transcoded and the episode in its feed: what every test
    starts from and is put back to."""
    world['video'] = tube_video()
    tess_avatar()
    world['podcast_feed'] = podcast_feed()
    return stack_state.take()


@pytest.fixture(autouse=True)
def clean_state(baseline):
    """PieFed as `baseline` before the test and again after it, whatever the test did or left half done."""
    stack_state.restore(baseline)
    yield
    stack_state.restore(baseline)


def png(width: int = 64, height: int = 48) -> bytes:
    """A plain blue PNG, made without an imaging library (the apps runner has none)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    rows = b''.join(b'\x00' + b'\x30\x60\xc0' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def status_id(post: dict) -> str:
    """The Mastodon status id PieFed gives a post from the alpha API (app/mastodon/ids.py): the second since 2000 it
    was posted, above the table bit (0: a post) and its row id."""
    published = datetime.fromisoformat(post['published'].rstrip('Z'))
    return str(int((published - datetime(2000, 1, 1)).total_seconds()) << 32 | post['id'])


def marker(app: str) -> str:
    """A string unique to this app in this run, for the app to post and the test to find."""
    return f'interop-{app}-{RUN}'


def find_comment(piefed: PieFed, post_id: int, text: str, timeout: float = 30.0):
    def look():
        replies = piefed.http.get('/comment/list', params={'post_id': post_id, 'limit': 50, 'sort': 'New'})
        return next((c for c in replies.raise_for_status().json()['comments'] if text in c['comment']['body']), None)
    return wait_for(look, timeout=timeout)


def find_post(piefed: PieFed, community_id: int, text: str, timeout: float = 30.0):
    def look():
        posts = piefed.http.get('/post/list', params={'community_id': community_id, 'sort': 'New', 'limit': 50})
        return next((p for p in posts.raise_for_status().json()['posts'] if text in p['post']['title']), None)
    return wait_for(look, timeout=timeout)


@pytest.fixture(scope='session')
def emulator():
    """The emulator (android_device.py), connected on first use (a web-only run never touches adb), and the apps it
    had when the session began."""
    found = Device()
    found.session_packages = set(found.adb.list_packages())
    return found


@pytest.fixture
def device(emulator):
    """The emulator for one test: before it and in its teardown, every app installed since the session began is
    uninstalled (with its data and accounts), and Chrome's data and the pushed media are taken away."""
    emulator.reset(emulator.session_packages)
    yield emulator
    emulator.reset(emulator.session_packages)


@pytest.fixture(scope='session')
def tmp_results():
    """Where screenshots of a failing step go, beside the results."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    return RESULTS


@pytest.fixture(scope='session')
def http():
    """A plain client for piefed.test, as an app with no account sees it."""
    return httpx.Client(base_url='https://piefed.test', verify=ssl.create_default_context(cafile=CA), timeout=15)


def _listed_video(http):
    found = http.get('/api/v1/videos', params={'sort': '-publishedAt', 'count': 100}).raise_for_status().json()
    return next((video for video in found['data'] if video['name'] == TUBE_TITLE), None)


def tube_video() -> dict:
    """TUBE_TITLE on tube.test, as PeerTube's API shows it: uploaded by tess to her community the first time, then
    reused. Listed once PieFed's media pipeline has made it ready; returned once its renditions are listed too."""
    with httpx.Client(base_url=f'https://{TUBE}', verify=ssl.create_default_context(cafile=CA), timeout=15) as http:
        video = _listed_video(http)
        if video is None:
            r = PieFedWeb(*TESS, host=TUBE).submit_media('tube', 'video', TUBE_TITLE, VIDEO_FIXTURE, 'video/mp4')
            assert r.status_code == 302, f'upload answered {r.status_code}: {form_errors(r.text)}'
            video = wait_for(lambda: _listed_video(http), timeout=120, interval=2)
        return wait_for(lambda: _with_renditions(http, video['id']), timeout=180, interval=2)


def tess_avatar() -> None:
    """tess with an avatar, as an uploader has: P2Play reads an account's first avatar unguarded and stops on one
    with none (its AccountBanner), as it would against PeerTube itself."""
    tess = PieFed(*TESS, host=TUBE)
    uploaded = tess.http.post('/upload/user_image', files={'file': ('tess.png', png(96, 96), 'image/png')})
    tess.http.put('/user/save_user_settings', json={'avatar': uploaded.raise_for_status().json()['url']}) \
        .raise_for_status()


def _with_renditions(http, video_id):
    video = http.get(f'/api/v1/videos/{video_id}').raise_for_status().json()
    labels = {entry['resolution']['label'] for entry in video['files']}
    return video if {'480p', '360p'} <= labels else None


def episode_items(http, feed_url):
    """The feed's items titled EPISODE with an enclosure."""
    feed = ET.fromstring(http.get(feed_url).raise_for_status().content)
    return [item for item in feed.iter('item') if item.findtext('title') == EPISODE and item.find('enclosure') is not None]


def podcast_feed() -> str:
    """The feed URL of pod_media, once EPISODE is in it. The harness instance is private (R219), so the URL carries
    alice's RSS token, as its community page's link does."""
    web = PieFedWeb(*ALICE)
    link = re.search(r'/community/pod_media/feed\?token=([\w-]+)', web.http.get('/c/pod_media').text)
    assert link, 'the pod_media page has no feed link carrying the member token'
    feed_url = f'https://piefed.test/community/pod_media/feed?token={link[1]}'
    r = web.submit_media('pod_media', 'audio', EPISODE, EPISODE_FIXTURE, 'audio/mpeg')
    assert r.status_code == 302, f'upload answered {r.status_code}: {form_errors(r.text)}'
    looks = iter(range(10_000))   # the feed is cached per URL and query string: a fresh query each look
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=15) as http:
        wait_for(lambda: episode_items(http, f'{feed_url}&t=wait{next(looks)}'), timeout=90, interval=2)
    return feed_url
