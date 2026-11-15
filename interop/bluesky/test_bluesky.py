"""Connected accounts P2, end to end (spec 2026-11-13-connected-accounts-design.md §9): the harness PieFed connects a
Bluesky account on the harness PDS by atproto OAuth, a microblog goes there with its hashtag facet, its deletion
follows, and disconnecting revokes the tokens. Run by `interop/run.sh bluesky` in the bluesky-runner container
(Playwright: the PDS's sign-in page is a web app)."""
import ssl

import httpx
import pytest
from playwright.sync_api import sync_playwright

from interop.clients import CA, PieFed, wait_for

PDS = 'https://pds.test'
HANDLE = 'alice.pds.test'
PDS_PASSWORD = 'interop-alice-bsky-pw'
PIEFED = 'https://piefed.test'


@pytest.fixture(scope='module')
def http():
    with httpx.Client(verify=ssl.create_default_context(cafile=CA), timeout=20) as client:
        yield client


@pytest.fixture(scope='module')
def did(http):
    """alice's Bluesky account on the harness PDS (made once: the PDS keeps it between runs of one stack)."""
    answer = http.post(f'{PDS}/xrpc/com.atproto.server.createAccount',
                       json={'email': 'alice@pds.test', 'handle': HANDLE, 'password': PDS_PASSWORD})
    assert answer.status_code == 200 or 'already taken' in answer.text, answer.text
    return http.get(f'https://{HANDLE}/.well-known/atproto-did').text.strip()


def posts_of(http, did) -> list[dict]:
    answer = http.get(f'{PDS}/xrpc/com.atproto.repo.listRecords',
                      params={'repo': did, 'collection': 'app.bsky.feed.post', 'limit': 100})
    return [record['value'] | {'uri': record['uri']} for record in answer.raise_for_status().json()['records']]


@pytest.fixture
def browser_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.on('dialog', lambda dialog: dialog.accept())   # Disconnect asks first (confirm_first)
        yield page
        browser.close()


def sign_in_to_piefed(page):
    page.goto(f'{PIEFED}/auth/login')
    page.fill('input[name=user_name]', 'alice')
    page.fill('input[name=password]', 'interop-alice-pw')
    page.click('input[type=submit], button[type=submit]')
    page.wait_for_load_state('networkidle')


def connect(page):
    page.goto(f'{PIEFED}/user/settings/connections')
    page.fill('input[name=hint]', HANDLE)
    page.get_by_role('button', name='Connect Bluesky').click()
    try:   # the first sign-in of a run waits for the PDS to fetch PieFed's client metadata
        page.wait_for_url(f'{PDS}/**', timeout=60_000)
    except Exception:
        raise AssertionError(f'never reached the PDS: at {page.url}: {page.inner_text("body")[:500]}') from None
    page.wait_for_load_state('networkidle')
    page.locator('input[type=password]').fill(PDS_PASSWORD)
    page.locator('button[type=submit]').first.click()
    for _ in range(60):   # the PDS asks for consent the first time only; click it once, while it can be clicked
        if page.url.startswith(f'{PIEFED}/'):
            break
        authorize = page.get_by_role('button', name='Authorize')
        if authorize.count() and authorize.is_enabled():
            authorize.click(no_wait_after=True)
            break
        page.wait_for_timeout(500)
    page.wait_for_url(f'{PIEFED}/user/settings/connections', timeout=60_000)


@pytest.fixture
def connected(browser_page, did, http):
    """alice's PieFed account connected to her Bluesky one; disconnected again after the test."""
    before = {post['uri'] for post in posts_of(http, did)}
    sign_in_to_piefed(browser_page)
    connect(browser_page)
    yield browser_page
    browser_page.goto(f'{PIEFED}/user/settings/connections')
    if browser_page.get_by_role('button', name='Disconnect').count():
        browser_page.get_by_role('button', name='Disconnect').click()
        browser_page.wait_for_load_state('networkidle')
    for post in posts_of(http, did):   # nothing the test made stays on the PDS
        assert post['uri'] in before, f'left behind on the PDS: {post}'


def test_connecting_shows_the_account(connected):
    body = connected.inner_text('body')
    assert f'Connected Bluesky: {HANDLE}' in body and 'Working' in body


def test_a_microblog_goes_to_bluesky_and_its_deletion_follows(connected, did, http):
    piefed = PieFed('alice', 'interop-alice-pw')
    marker = 'Hello Bluesky from PieFed #piefed'
    post = piefed.post(marker, marker, piefed.microblogs_id())['post']   # the API wants a title; the body repeats it

    def copied():
        return next((record for record in posts_of(http, did) if record['text'].startswith(marker)), None)
    record = wait_for(copied, timeout=60)
    tags = [feature['tag'] for facet in record.get('facets', []) for feature in facet['features']
            if feature['$type'] == 'app.bsky.richtext.facet#tag']
    assert tags == ['piefed']

    connected.goto(f"{PIEFED}/post/{post['id']}")
    assert 'Posted' in connected.inner_text('#crossposts')

    # spec 19.2: PieFed chose the record key (a TID), and a retry asks getRecord whether an earlier attempt made it
    rkey = record['uri'].rsplit('/', 1)[-1]
    assert len(rkey) == 13

    def lookup():
        return http.get(f'{PDS}/xrpc/com.atproto.repo.getRecord',
                        params={'repo': did, 'collection': 'app.bsky.feed.post', 'rkey': rkey})
    assert lookup().status_code == 200

    piefed.delete(post['id'])
    wait_for(lambda: copied() is None, timeout=60)
    gone = lookup()   # what PieFed reads as "no such record": anything else stops a retry
    assert (gone.status_code, gone.json().get('error')) == (400, 'RecordNotFound'), gone.text


def test_disconnecting_forgets_the_account(connected):
    connected.get_by_role('button', name='Disconnect').click()
    connected.wait_for_load_state('networkidle')
    assert 'Disconnected alice.pds.test' in connected.inner_text('body')
    assert connected.get_by_role('button', name='Disconnect').count() == 0
