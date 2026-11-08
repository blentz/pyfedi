"""The client-app harness's PieFed world, made through PieFed's own API: dora's community `apps` with a post every app
must show, which alice (the account every app logs in as) has joined. Idempotent, so a re-run reuses it."""
import os
import ssl
import time

import httpx
import pytest

from interop.apps.android_device import Device
from interop.apps.matrix import RESULTS
from interop.clients import CA, PieFed, wait_for

ALICE = ('alice', 'interop-alice-pw')
DORA = ('dora', 'interop-dora-pw')
COMMUNITY = 'apps'
SEED_TITLE = 'Interop apps: a post every app must show'
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
