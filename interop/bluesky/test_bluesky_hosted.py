"""ATProto P2, end to end (spec 2026-11-23-atproto-federation-design.md §11, steps 1 to 3): alice creates her Bluesky
account on the harness PDS in one click and her handle resolves on her domain; her microblog post lands in her
repo, as does her profile; tess, on the second domain, waits for a place since the harness has one
(ATPROTO_MAX_ACCOUNTS=1). Run by `interop/run.sh bluesky` in the bluesky-runner container."""
import os
import ssl

import httpx
import pytest
from playwright.sync_api import sync_playwright

from interop.clients import CA, PieFed, goto, wait_for

PDS = 'https://pds.test'
PLC = 'https://plc.test'


@pytest.fixture(scope='module')
def http():
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=20) as client:
        yield client


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        yield chromium
        chromium.close()


def signed_in(browser, host, user, password):
    page = browser.new_context(ignore_https_errors=True).new_page()
    page.on('dialog', lambda dialog: dialog.accept())   # Delete asks first (confirm_first)
    goto(page, f'https://{host}/auth/login')
    page.fill('input[name=user_name]', user)
    page.fill('input[name=password]', password)
    page.click('input[type=submit], button[type=submit]')
    page.wait_for_load_state('networkidle')
    page.goto(f'https://{host}/user/settings/connections')
    return page


def records_of(http, did, collection):
    answer = http.get(f'{PDS}/xrpc/com.atproto.repo.listRecords', params={'repo': did, 'collection': collection})
    return answer.raise_for_status().json()['records']


def clear_leftover(http, handle):
    """An account a run that stopped part way left on the PDS (it keeps accounts between runs of one stack)."""
    answer = http.get(f'{PDS}/xrpc/com.atproto.identity.resolveHandle', params={'handle': handle})
    if answer.status_code == 200:
        http.post(f'{PDS}/xrpc/com.atproto.admin.deleteAccount', auth=('admin', os.environ['PDS_ADMIN_PASSWORD']),
                  json={'did': answer.json()['did']}).raise_for_status()


def test_creating_posting_and_waiting(browser, http):
    for handle in ('alice.piefed.test', 'tess.tube.test'):
        clear_leftover(http, handle)
    alice = signed_in(browser, 'piefed.test', 'alice', 'interop-alice-pw')
    alice.get_by_role('button', name='Create @alice.piefed.test').click()
    alice.wait_for_load_state('networkidle')
    assert 'Your Bluesky account @alice.piefed.test is ready.' in alice.inner_text('body'), alice.inner_text('body')

    # step 1: the handle resolves on alice's own domain, and her DID names it and the PDS
    did = http.get('https://alice.piefed.test/.well-known/atproto-did').text.strip()
    assert did.startswith('did:plc:'), did
    document = http.get(f'{PLC}/{did}').json()
    assert document['alsoKnownAs'] == ['at://alice.piefed.test']
    assert document['service'][0]['serviceEndpoint'] == PDS

    # her profile went with it: her name, and a link back
    profile = wait_for(lambda: next(iter(records_of(http, did, 'app.bsky.actor.profile')), None), timeout=60)
    assert profile['value']['description'].endswith('https://piefed.test/u/alice')

    # step 2: a microblog post lands in her repo
    piefed = PieFed('alice', 'interop-alice-pw')
    marker = 'Hello from a Bluesky account made by PieFed'
    post = piefed.post(marker, marker, piefed.microblogs_id())['post']

    def copied():
        return next((record for record in records_of(http, did, 'app.bsky.feed.post')
                     if record['value']['text'].startswith(marker)), None)
    wait_for(copied, timeout=60)

    # step 3: the one place is taken: tess, on tube.test, waits
    tess = signed_in(browser, 'tube.test', 'tess', 'interop-tess-pw')
    tess.get_by_role('button', name='Create @tess.tube.test').click()
    tess.wait_for_load_state('networkidle')
    body = tess.inner_text('body')
    assert 'you are on the waiting list' in body and 'On the waiting list: number 1' in body, body
    unknown = http.get(f'{PDS}/xrpc/com.atproto.identity.resolveHandle', params={'handle': 'tess.tube.test'})
    assert unknown.status_code == 400, unknown.text   # no account yet

    # deleting: the post's copy goes, then the account
    piefed.delete(post['id'])
    wait_for(lambda: copied() is None, timeout=60)
    alice.goto('https://piefed.test/user/settings/connections')
    alice.get_by_role('button', name='Delete this Bluesky account').click()
    alice.wait_for_load_state('networkidle')
    assert http.get('https://alice.piefed.test/.well-known/atproto-did').status_code == 404
    tess.goto('https://tube.test/user/settings/connections')
    tess.get_by_role('button', name='Leave the waiting list').click()
    tess.wait_for_load_state('networkidle')
