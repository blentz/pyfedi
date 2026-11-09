"""The client-app harness's PieFed world, made through PieFed's own API: dora's community `apps` with a post every app
must show, which alice (the account every app logs in as) has joined. Idempotent, so a re-run reuses it."""
import os
from datetime import datetime
import ssl
import time

import httpx
import pytest

from interop.apps.android_device import Device
from interop.apps.matrix import RESULTS
from interop.clients import CA, PieFed, PieFedWeb, form_errors, wait_for

ALICE = ('alice', 'interop-alice-pw')
DORA = ('dora', 'interop-dora-pw')
COMMUNITY = 'apps'
SEED_TITLE = 'Interop apps: a post every app must show'
# The public domain apps with no account browse (piefed.test is private, R219), its uploader and its video
TUBE, TESS = 'tube.test', ('tess', 'interop-tess-pw')
TUBE_TITLE = 'Interop apps video'
VIDEO_FIXTURE = '/app/tests/fixtures/media/video_720p.mp4'   # 720p: it gains 480p and 360p renditions (W2.1b)
RUN = os.environ.get('APPS_RUN_ID') or time.strftime('%Y%m%d%H%M%S')


def _community(piefed):
    found = piefed.http.get('/community', params={'name': COMMUNITY})
    return found.json()['community_view']['community'] if found.status_code == 200 else None


@pytest.fixture(scope='session')
def world():
    alice, dora = PieFed(*ALICE), PieFed(*DORA)
    community = _community(dora)
    if community is None:
        dora.http.post('/community', json={'name': COMMUNITY, 'title': 'Interop apps',
                                           'description': 'Third-party app checks'}).raise_for_status()
        community = wait_for(lambda: _community(dora))
    posts = dora.http.get('/post/list', params={'community_id': community['id'], 'sort': 'New', 'limit': 50})
    seed = next((p['post'] for p in posts.raise_for_status().json()['posts'] if p['post']['title'] == SEED_TITLE), None)
    if seed is None:
        seed = dora.post(SEED_TITLE, 'Seeded by interop/apps/conftest.py.', community['id'])['post']
    alice.http.post('/community/follow', json={'community_id': community['id'], 'follow': True}).raise_for_status()
    return {'alice': alice, 'dora': dora, 'community': community, 'seed': seed}


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
def device():
    """The emulator (android_device.py), connected on first use: a web-only run never touches adb."""
    return Device()


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


def _with_renditions(http, video_id):
    video = http.get(f'/api/v1/videos/{video_id}').raise_for_status().json()
    labels = {entry['resolution']['label'] for entry in video['files']}
    return video if {'480p', '360p'} <= labels else None
