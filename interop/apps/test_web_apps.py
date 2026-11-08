"""The PieFed-capable web apps, driven in Chromium (Playwright) as alice: log in, show the seeded post in c/apps, and
comment on it. Each comment carries a marker unique to the app and run, found afterwards through PieFed's own API.

Each app is the newest image its project publishes (run.sh records which: results/images.txt), served by Caddy on a
name of its own. The browser trusts the harness CA by ignoring certificate errors: Chromium reads no CA bundle file,
and the apps' TLS handling is not under test (their Android builds are, against a system CA). Selectors are the
accessible names the apps give their controls, so a renamed button fails here with what the screen showed instead.
"""
import re

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from interop.apps import steps
from interop.apps.conftest import ALICE, SEED_TITLE, find_comment, marker
from interop.apps.matrix import RESULTS, load_matrix

MATRIX = load_matrix()
TIMEOUT = 30_000


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    # a phone-sized window: the apps are mobile-first, and their desktop layouts hide controls behind other menus
    context = browser.new_context(ignore_https_errors=True, viewport={'width': 412, 'height': 915})
    context.set_default_timeout(TIMEOUT)
    page = context.new_page()
    # what went wrong underneath the screen, written beside a failed step's screenshot
    page.trouble = []
    page.on('console', lambda message: message.type == 'error' and page.trouble.append(f'console: {message.text}'))
    page.on('requestfailed', lambda request: page.trouble.append(
        f'failed: {request.method} {request.url} {request.failure}'))
    page.on('response', lambda response: (response.status >= 400 or 'piefed.test' in response.url)
            and page.trouble.append(f'{response.status}: {response.request.method} {response.url}'))
    yield page
    context.close()


def _screenshot(page: Page, name: str):
    def capture(step):
        RESULTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(RESULTS / f'{name}-{step}.png'))
        stored = page.evaluate('Object.keys(localStorage).map(k => k + "=" + localStorage.getItem(k).slice(0, 300))')
        (RESULTS / f'{name}-{step}.log').write_text('\n'.join([f'at: {page.url}', *page.trouble, *stored]) + '\n')
    return capture


def _on_screen(page: Page) -> str:
    return page.locator('body').aria_snapshot()[:1500]


class Voyager:
    """Voyager (aeharding/voyager): Ionic. Accounts tab > Get Started > Log In > server > (PieFed's experimental-support
    alert) > credentials > Confirm."""

    def __init__(self, page: Page, base: str, world: dict):
        self.page, self.base, self.world = page, base, world

    def login(self):
        page = self.page
        page.goto(self.base + '/', wait_until='networkidle')
        page.get_by_role('tab', name='piefed.test').click()
        page.get_by_role('button', name='Get Started').click()
        page.get_by_role('button', name='Log In', exact=True).click()
        page.get_by_role('searchbox', name='search text').first.fill('piefed.test')
        page.get_by_role('button', name='Next').click()
        page.get_by_role('button', name='I understand').click()   # "Piefed support is experimental"
        page.get_by_role('textbox', name='Username or email').fill(ALICE[0])
        page.get_by_role('textbox', name='Password').fill(ALICE[1])
        page.get_by_role('button', name='Confirm').click()
        expect(page.get_by_text('alice@piefed.test').first).to_be_visible()
        return 'logged in as alice@piefed.test'

    def view(self):
        self.page.goto(f'{self.base}/posts/piefed.test/c/apps', wait_until='networkidle')
        expect(self.page.get_by_text(SEED_TITLE).first).to_be_visible()
        return 'c/apps lists the seeded post'

    def post(self):
        page, text = self.page, marker('voyager-web')
        page.get_by_text(SEED_TITLE).first.click()
        page.get_by_role('button', name='Reply').first.click()
        # the open sheet: Ionic keeps closed modals (the login one) in the DOM, and the editor is a contenteditable
        modal = page.locator('ion-modal.show-modal').filter(has_text='New Comment')
        modal.locator('[contenteditable="true"]').fill(text)
        modal.locator('ion-button[type="submit"]').click()
        expect(page.get_by_text(text).first).to_be_visible()
        find_comment(self.world['alice'], self.world['seed']['id'], text)
        return f'commented {text!r} on the seeded post; PieFed has it'


class Blorp:
    """Blorp (blorp-labs/blorp): the account menu > Login / Signup / Change instance (the default instance is
    piefed.test, REACT_APP_DEFAULT_INSTANCE) > credentials > Sign In. Comments in a rich-text sheet."""

    def __init__(self, page: Page, base: str, world: dict):
        self.page, self.base, self.world = page, base, world

    def login(self):
        page = self.page
        page.goto(self.base + '/', wait_until='networkidle')
        page.get_by_role('button', name='Open account/app settings menu').click()
        page.get_by_role('button', name='Login / Signup / Change instance').click()
        expect(page.get_by_text('Login to piefed.test')).to_be_visible()
        page.get_by_role('textbox', name='Username').fill(ALICE[0])
        page.get_by_role('textbox', name='Enter password').fill(ALICE[1])
        page.get_by_role('button', name='Sign In').click()
        page.get_by_role('button', name='Open account/app settings menu').click()
        expect(page.get_by_text('alice').first).to_be_visible()
        page.keyboard.press('Escape')
        return 'logged in as alice'

    def view(self):
        self.page.goto(f'{self.base}/home/c/apps@piefed.test', wait_until='networkidle')
        expect(self.page.get_by_text(SEED_TITLE).first).to_be_visible()
        return 'c/apps lists the seeded post'

    def post(self):
        page, text = self.page, marker('blorp-web')
        page.get_by_text(SEED_TITLE).first.click()
        page.get_by_role('button', name='Add a comment...').click()
        # a TipTap editor, rendered in a portal outside the sheet's dialog element
        page.locator('.ProseMirror[contenteditable="true"]').last.fill(text)
        page.get_by_role('button', name='Confirm', exact=True).last.click()
        expect(page.get_by_text(text).first).to_be_visible()
        find_comment(self.world['alice'], self.world['seed']['id'], text)
        return f'commented {text!r} on the seeded post; PieFed has it'


class Photon:
    """Photon (xyphyn/photon): SvelteKit, its instance fixed to piefed.test as PieFed (PUBLIC_INSTANCE_URL and
    PUBLIC_INSTANCE_TYPE=piefedalpha). /login > credentials > Log in."""

    def __init__(self, page: Page, base: str, world: dict):
        self.page, self.base, self.world = page, base, world

    def login(self):
        page = self.page
        page.goto(self.base + '/login', wait_until='networkidle')
        page.get_by_role('textbox', name='Username *').fill(ALICE[0])
        page.get_by_role('textbox', name='Password *').fill(ALICE[1])
        page.get_by_role('button', name='Log in').click()
        page.wait_for_url(lambda url: not url.rstrip('/').endswith('/login'))
        return 'logged in as alice'

    def view(self):
        self.page.goto(f'{self.base}/c/apps@piefed.test', wait_until='networkidle')
        expect(self.page.get_by_text(SEED_TITLE).first).to_be_visible()
        return 'c/apps lists the seeded post'

    def post(self):
        page, text = self.page, marker('photon-web')
        page.get_by_text(SEED_TITLE).first.click()
        page.get_by_role('button', name='Add a comment').click()
        page.get_by_role('textbox', name='Join the conversation').fill(text)
        page.get_by_role('button', name='Submit').click()
        expect(page.get_by_text(text).first).to_be_visible()
        find_comment(self.world['alice'], self.world['seed']['id'], text)
        return f'commented {text!r} on the seeded post; PieFed has it'


def piefed_consent(page: Page):
    """PieFed's half of an OAuth login (plan W0.2): its login page, as alice, then the consent page's Allow."""
    page.wait_for_url(lambda url: 'piefed.test/auth/login' in url or 'piefed.test/oauth/authorize' in url)
    if '/auth/login' in page.url:
        page.get_by_role('textbox', name='User name').fill(ALICE[0])
        page.get_by_role('textbox', name='Password').fill(ALICE[1])
        page.get_by_role('button', name='Log In', exact=True).click()
        page.wait_for_url(lambda url: 'piefed.test/oauth/authorize' in url)
    page.get_by_role('button', name='Allow').click()


# Whether any localStorage value names the account, as a JSON string or the whole value, bare or with its domain:
# Mastodon apps keep the signed-in account (and its token) there, which no layout change moves
HOLDS_ACCOUNT = """(name) => {
    const named = new RegExp('(^|")' + name + '(@piefed\\.test)?("|$)');
    return Object.keys(localStorage).some(key => named.test(localStorage.getItem(key) || ''));
}"""


def holds_account(page: Page, name: str):
    page.wait_for_function(HOLDS_ACCOUNT, arg=name, timeout=TIMEOUT)


def no_timelines():
    raise steps.Unsupported('PieFed serves no Mastodon timelines yet (plan W1.2)')


def no_statuses():
    raise steps.Unsupported('PieFed takes no Mastodon statuses yet (plan W1.3)')


class Phanpy:
    """Phanpy (cheeaun/phanpy): a static Mastodon app. Log in > instance > PieFed's login and consent in a popup >
    back with a code, which the browser trades for a token at /oauth/token (with PKCE: PieFed's RFC 8414 metadata
    offers S256)."""

    def __init__(self, page: Page, base: str, world: dict):
        self.page, self.base, self.world = page, base, world

    def login(self):
        page = self.page
        page.goto(self.base + '/#/login', wait_until='networkidle')
        page.get_by_role('textbox').first.fill('piefed.test')
        # PieFed's pages open in a popup, which Phanpy watches for the code it comes back with
        with page.expect_popup() as opened:
            page.get_by_role('button', name='Continue with piefed.test').click()
        piefed_consent(opened.value)
        page.wait_for_url(lambda url: url.startswith(self.base) and '?code=' not in url)
        holds_account(page, ALICE[0])
        return 'logged in as alice through OAuth'

    view, post = staticmethod(no_timelines), staticmethod(no_statuses)


class Elk:
    """Elk (elk-zone/elk): Nuxt, whose server registers the app and trades the code. Sign in > server (its default is
    piefed.test, NUXT_PUBLIC_DEFAULT_SERVER) > PieFed's login and consent > back."""

    def __init__(self, page: Page, base: str, world: dict):
        self.page, self.base, self.world = page, base, world

    def login(self):
        page = self.page
        page.goto(self.base + '/', wait_until='networkidle')
        page.get_by_role('button', name=re.compile('sign in', re.I)).first.click()
        page.get_by_role('textbox').first.fill('piefed.test')
        page.keyboard.press('Enter')
        piefed_consent(page)
        page.wait_for_url(lambda url: url.startswith(self.base))
        holds_account(page, ALICE[0])
        return 'logged in as alice through OAuth'

    view, post = staticmethod(no_timelines), staticmethod(no_statuses)


DRIVERS = {'voyager-web': Voyager, 'blorp-web': Blorp, 'photon-web': Photon, 'phanpy-web': Phanpy, 'elk-web': Elk}


@pytest.mark.parametrize('name', sorted(DRIVERS), ids=sorted(DRIVERS))
def test_web_app(world, page, name):
    app = MATRIX[name]
    driver = DRIVERS[name](page, app['web'], world)
    steps.run(name, app, {'login': driver.login, 'view': driver.view, 'post': driver.post},
              version=app['image'], on_failure=_screenshot(page, name))
