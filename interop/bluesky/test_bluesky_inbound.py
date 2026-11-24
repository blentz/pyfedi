"""ATProto P3, end to end (spec 2026-11-23-atproto-federation-design.md §11, step 5): alice on PieFed follows bob, an
account on the harness PDS, by his handle; his post from before the follow is read from his PDS, and the one he
writes after arrives through Jetstream and PieFed's consumer (the piefed-jetstream service); his delete follows.
Run by `interop/run.sh bluesky` in the bluesky-runner container."""
import ssl
import time

import httpx
import pytest
from playwright.sync_api import sync_playwright

from interop.clients import CA, goto, wait_for

PDS = 'https://pds.test'
PIEFED = 'https://piefed.test'
HANDLE = 'bob.pds.test'
PASSWORD = 'interop-bob-bsky-pw'


@pytest.fixture(scope='module')
def http():
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=20) as client:
        yield client


@pytest.fixture(scope='module')
def bob(http):
    """bob's session on the harness PDS (made once: the PDS keeps it between runs of one stack)."""
    made = http.post(f'{PDS}/xrpc/com.atproto.server.createAccount',
                     json={'email': 'bob@pds.test', 'handle': HANDLE, 'password': PASSWORD})
    if made.status_code != 200:
        assert 'already taken' in made.text, made.text
        made = http.post(f'{PDS}/xrpc/com.atproto.server.createSession',
                         json={'identifier': HANDLE, 'password': PASSWORD})
    session = made.raise_for_status().json()
    return {'did': session['did'], 'headers': {'Authorization': f"Bearer {session['accessJwt']}"}}


def write(http, bob, text):
    record = {'$type': 'app.bsky.feed.post', 'text': text,
              'createdAt': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())}
    answer = http.post(f'{PDS}/xrpc/com.atproto.repo.createRecord', headers=bob['headers'],
                       json={'repo': bob['did'], 'collection': 'app.bsky.feed.post', 'record': record})
    return answer.raise_for_status().json()['uri']


@pytest.fixture
def alice():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        goto(page, f'{PIEFED}/auth/login')
        page.fill('input[name=user_name]', 'alice')
        page.fill('input[name=password]', 'interop-alice-pw')
        page.click('input[type=submit], button[type=submit]')
        page.wait_for_load_state('networkidle')
        yield page
        browser.close()


def profile_text(page) -> str:
    page.goto(f'{PIEFED}/u/{HANDLE}')
    return page.inner_text('body')


def test_following_a_bluesky_account_brings_its_posts(alice, http, bob):
    stamp = str(int(time.time()))
    before = write(http, bob, f'Written before the follow {stamp}')

    body = profile_text(alice)
    assert HANDLE in body, body[:500]
    if 'Unfollow' in body:   # a run that stopped part way left the follow
        alice.get_by_role('link', name='Unfollow', exact=True).first.click()
        alice.wait_for_load_state('networkidle')
        profile_text(alice)
    alice.get_by_role('link', name='Follow', exact=True).first.click()
    alice.wait_for_load_state('networkidle')

    # the post from before the follow: read from bob's PDS when the follow was made
    wait_for(lambda: f'Written before the follow {stamp}' in profile_text(alice), timeout=60)

    # the post after it: through Jetstream and the consumer
    after = write(http, bob, f'Written after the follow {stamp}')
    wait_for(lambda: f'Written after the follow {stamp}' in profile_text(alice), timeout=90)

    # his delete follows
    rkey = after.rsplit('/', 1)[-1]
    http.post(f'{PDS}/xrpc/com.atproto.repo.deleteRecord', headers=bob['headers'],
              json={'repo': bob['did'], 'collection': 'app.bsky.feed.post', 'rkey': rkey}).raise_for_status()
    def gone():   # as a logged-out reader: alice is an admin, who still sees deleted posts
        return f'Written after the follow {stamp}' not in http.get(f'{PIEFED}/u/{HANDLE}').text
    wait_for(gone, timeout=90)

    # cleanup: the follow goes, and so do bob's posts on the PDS
    profile_text(alice)
    alice.get_by_role('link', name='Unfollow', exact=True).first.click()
    alice.wait_for_load_state('networkidle')
    http.post(f'{PDS}/xrpc/com.atproto.repo.deleteRecord', headers=bob['headers'], json={
        'repo': bob['did'], 'collection': 'app.bsky.feed.post', 'rkey': before.rsplit('/', 1)[-1]})
