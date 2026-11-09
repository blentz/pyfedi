"""The PieFed-capable Android apps, each the newest release (apks.py), installed clean on the emulator and driven as
alice: log in, show the seeded post, comment on it. The comment carries a marker found afterwards through PieFed's API.

Viewing is a search for the seeded post, as on a busy instance: a feed's first screen holds whatever else the
stack carries (after `interop/run.sh test` it is full of Loops, Mastodon and Pixelfed posts).

The emulator trusts the harness CA as a system CA (android/boot.sh) and resolves piefed.test through the stack's DNS,
so the apps reach PieFed exactly as they would a public instance. Controls are found by resource id where the app has
them (native apps) and by the text or content description they show otherwise.
"""
import time

import httpx
import pytest

from interop.apps import apks, steps
from interop.apps.conftest import ALICE, SEED_TITLE, find_comment, marker
from interop.apps.matrix import RESULTS, load_matrix

MATRIX = load_matrix()
# what the apps search for: a prefix of the seeded title, so that only a result, never the search field still holding
# the query, shows the whole title the post step taps
QUERY = SEED_TITLE[:20]


class Summit:
    """Summit (idunnololz/summit): native. You > Sign in or sign up > instance, username, password > Login. After the
    login its home feed is All@piefed.test; a post's reply action opens the comment editor."""

    def __init__(self, device, package, world):
        self.device, self.world = device, world
        self.id = lambda name: f'{package}:id/{name}'

    def login(self):
        d = self.device
        d.ui(description='You').click(timeout=30)
        d.tap('Sign in or sign up')
        d.type_into(d.ui(resourceId=self.id('instance_edit_text')), 'piefed.test')
        d.type_into(d.ui(resourceId=self.id('username_edit_text')), ALICE[0])
        d.type_into(d.ui(resourceId=self.id('password_edit_text')), ALICE[1])
        d.hide_keyboard()   # it covers the Login button
        d.ui(resourceId=self.id('login')).click(timeout=15)
        d.wait_text('alice@piefed.test', timeout=60)
        return 'logged in as alice@piefed.test'

    def view(self):
        d = self.device
        d.tap_tab('Search')
        d.type_into(d.ui(className='android.widget.EditText', instance=0), QUERY)
        d.ui.press('enter')
        d.wait_text(SEED_TITLE, timeout=60)   # in the Posts results
        return 'its search finds the seeded post'

    def post(self):
        d, text = self.device, marker('summit-android')
        d.tap(SEED_TITLE)
        d.tap_until(lambda: d.ui(resourceId=self.id('pa_reply')).click(timeout=30), 'Send comment')   # can be swallowed
        d.type_into(d.ui(resourceId=self.id('comment_edit_text')), text)
        d.ui(resourceId=self.id('send_comment')).click(timeout=15)
        find_comment(self.world['alice'], self.world['seed']['id'], text, timeout=60)
        d.wait_text(text, timeout=30)
        return f'commented {text!r} on the seeded post; PieFed has it'


class VoyagerAndroid:
    """Voyager's Android build (Capacitor: the web app in the system WebView, which needs Blink 140 or newer). The
    WebView's accessibility tree is what uiautomator sees; buttons show their labels in capitals. The account tab is
    named after the default server (lemmy.zip) until a login."""

    def __init__(self, device, package, world):
        self.device, self.world = device, world

    def login(self):
        d = self.device
        d.tap_tab('lemmy.zip', timeout=60)   # the account tab, not a feed post naming the server
        # the account tab shows a guest page with Get Started, or goes straight to the Welcome sheet
        d.wait_text('Get Started', 'Pick another server', timeout=30)
        if d.showing('Get Started', exact=True):
            d.tap('Get Started', exact=True)
        d.tap('Log In', exact=True)   # not the guest page's "Log in to vote, comment and post!"
        d.type_into(d.ui(className='android.widget.EditText', instance=0), 'piefed.test')   # the server search
        d.tap('Next')
        d.tap('I understand')   # "Piefed support is experimental"
        # The form's inputs reach the WebView's accessibility tree late (after Ionic's page transition) and without
        # their labels: wait for both, longer than an element's implicit wait.
        d.wait_text('You are logging in to', timeout=30)
        password = d.ui(className='android.widget.EditText', instance=1)
        if not password.wait(timeout=60):
            raise AssertionError(f'android: the login form never showed two inputs; on screen: {d.screen_text()[:400]}')
        d.type_into(d.ui(className='android.widget.EditText', instance=0), ALICE[0])
        d.type_into(password, ALICE[1])
        d.hide_keyboard()
        d.tap('Confirm', exact=True)
        # it lands on the profile or on the feed; either way the account tab is now named after piefed.test
        # (a tap while it is still navigating there is swallowed)
        d.tap_until(lambda: d.tap_tab('piefed.test', timeout=60), 'alice@piefed.test', wait=20)
        return 'logged in as alice@piefed.test'

    def view(self):
        d = self.device
        d.tap_tab('Search')
        d.type_into(d.ui(className='android.widget.EditText', instance=0), QUERY)
        # submitting the search opens its posts results (src/routes/pages/search/SearchPage.tsx: router.push of the
        # posts link), which a tap on the "Posts with" suggestion did not do reliably; a second submit is harmless
        d.tap_until(lambda: d.ui.press('enter'), SEED_TITLE, wait=20)
        return 'its search finds the seeded post'

    def post(self):
        d, text = self.device, marker('voyager-android')
        d.tap(SEED_TITLE)
        d.tap_until(lambda: d.tap('Reply', exact=True), 'New Comment')   # can be swallowed
        editor = d.ui(className='android.widget.EditText', instance=0)
        editor.wait(timeout=30)
        editor.click()
        d.ui.send_keys(text)
        d.tap('Post')
        find_comment(self.world['alice'], self.world['seed']['id'], text, timeout=60)
        d.wait_text(text, timeout=30)
        return f'commented {text!r} on the seeded post; PieFed has it'


class Thunder:
    """Thunder (thunder-app/thunder): Flutter, whose semantics give uiautomator content descriptions and hint-only
    fields. Offline, its "Unable to load" page's Account Settings > Add Account: instance (it then says "PieFed support is
    currently in beta"), username, password > Log in. Its sheet closes on back, so the keyboard is left up."""

    def __init__(self, device, package, world):
        self.device, self.world = device, world

    def login(self):
        d = self.device
        # Offline (the emulator reaches only the harness) it cannot load its default instance, lemmy.world, and every
        # tab shows "Unable to load lemmy.world" with an Account Settings link: the way to its accounts sheet. A tap
        # just after launch can be swallowed.
        d.wait_text('Unable to load', timeout=60)
        d.tap_until(lambda: d.tap('Account Settings', exact=True), 'Add Account')
        d.tap('Add Account', exact=True)
        d.type_into(d.ui(className='android.widget.EditText', instance=0), 'piefed.test')
        d.wait_text('PieFed support', timeout=30)   # it recognised PieFed (nodeinfo)
        # The beta notice pushes the form under the keyboard, out of Flutter's hierarchy, on a cold start: type by focus,
        # Tab from field to field, rather than finding each field
        d.next_field()
        d.type_focused(ALICE[0])
        d.next_field()
        d.type_focused(ALICE[1])
        d.reveal('Log in')   # under the keyboard
        d.tap('Log in')
        d.wait_text('Subscriptions', timeout=60)   # the logged-in home feed
        return 'logged in as alice; its feed is her subscriptions'

    def view(self):
        d = self.device
        d.tap_tab('Search')
        d.type_into(d.ui(className='android.widget.EditText', instance=0), QUERY)
        d.ui.press('enter')
        d.tap('Posts', exact=True)   # it searches communities first
        d.wait_text(SEED_TITLE, timeout=60)
        return 'its search finds the seeded post'

    def post(self):
        d, text = self.device, marker('thunder-android')
        d.tap(SEED_TITLE)
        d.tap_until(lambda: d.ui(description='Reply').click(timeout=30), 'Create Comment')   # can be swallowed
        editor = d.ui(className='android.widget.EditText', instance=0)
        editor.wait(timeout=30)
        d.type_into(editor, text)
        d.ui(className='android.widget.Button', description='Create Comment').click(timeout=15)
        # not looked for on screen: the thread sorts by Top, so a new comment lands below the fold
        find_comment(self.world['alice'], self.world['seed']['id'], text, timeout=60)
        return f'commented {text!r} on the seeded post; PieFed has it'


def piefed_consent(d, package):
    """PieFed's half of an OAuth login (plan W0.2), in the browser the app opened: its login page as alice, the consent
    page's Allow, and then the app itself back on screen, holding the code PieFed sent it, and signed in. The login
    form's fields are found by order: they are the page's only inputs (its nav has no search box logged out)."""
    d.wait_text('Log In', 'Allow', timeout=60, contains=False)   # PieFed's buttons, not an app's "Log in with…"
    if not d.showing('Allow', exact=True):
        d.type_into(d.ui(className='android.widget.EditText', instance=0), ALICE[0])
        d.type_into(d.ui(className='android.widget.EditText', instance=1), ALICE[1])
        d.hide_keyboard()
        d.ui(className='android.widget.Button', text='Log In').click(timeout=30)   # not the "Log in" heading
    d.tap('Allow', exact=True, timeout=60)
    d.wait_foreground(package, timeout=60)
    d.wait_text(*SIGNED_IN, timeout=60)


def chrome_first_run(d):
    """A fresh Chrome may ask about itself before it shows the custom tab (android/boot.sh turns most of it off)."""
    for question in ('Use without an account', 'Accept & continue', 'No thanks'):
        if d.showing(question, exact=True):
            d.tap(question, exact=True)


def no_statuses():
    raise steps.Unsupported('PieFed takes no Mastodon statuses yet (plan W1.3)')


# what a Mastodon app shows once it holds a token (and only then): its home screen's tabs, or alice's handle
SIGNED_IN = ('@alice', 'Notifications', 'Home')


class Tusky:
    """Tusky (tuskyapp/Tusky): native. Its login screen takes the server; its menu's Login with Browser opens PieFed's
    pages in a Chrome custom tab (its default, its own WebView, cannot pass: android/boot.sh), and PieFed sends the code
    back to oauth2redirect://, which Tusky catches."""

    def __init__(self, device, package, world):
        self.device, self.world, self.package = device, world, package
        self.id = lambda name: f'{package}:id/{name}'

    def login(self):
        d = self.device
        d.type_into(d.ui(resourceId=self.id('domainEditText')), 'piefed.test')
        d.hide_keyboard()
        d.ui(description='More options').click(timeout=30)
        d.tap('Login with Browser')
        chrome_first_run(d)
        piefed_consent(d, self.package)
        return 'logged in as alice through OAuth'

    def view(self):
        d = self.device
        d.ui(description='Search').click(timeout=30)
        d.wait_text('Posts', timeout=30)   # the search screen, its field focused (a SearchView: no EditText to find)
        d.type_focused(QUERY)
        d.ui.press('enter')
        d.wait_text(SEED_TITLE, timeout=60)   # in the Posts results
        return 'its search finds the seeded post'

    post = staticmethod(no_statuses)


class MastodonAndroid:
    """Mastodon's own app (mastodon/mastodon-android): Log in > the server, typed into its picker > Next; PieFed's pages
    open in a Chrome custom tab, and PieFed sends the code back to mastodon-android-auth://."""

    def __init__(self, device, package, world):
        self.device, self.world, self.package = device, world, package

    def login(self):
        d = self.device
        d.tap('Log in', exact=True, timeout=60)
        # the picker looks the typed server up and lists it under the field: the listed row (a TextView, not the field
        # showing the same text) chooses it, and Next goes on to its login
        d.type_into(d.ui(className='android.widget.EditText', instance=0), 'piefed.test')
        d.ui(className='android.widget.TextView', text='piefed.test').click(timeout=30)
        d.hide_keyboard()
        d.tap('Next', exact=True)
        chrome_first_run(d)
        piefed_consent(d, self.package)
        return 'logged in as alice through OAuth'

    def view(self):
        d = self.device
        d.tap_tab('Explore')
        d.tap('Search Mastodon', exact=True)   # a button until tapped, which opens the search field focused
        d.type_focused(QUERY)
        d.ui.press('enter')
        d.wait_text(SEED_TITLE, timeout=60)
        return 'its search finds the seeded post'

    post = staticmethod(no_statuses)


DRIVERS = {'summit-android': Summit, 'voyager-android': VoyagerAndroid, 'thunder-android': Thunder,
           'tusky-android': Tusky, 'mastodon-android': MastodonAndroid}


@pytest.mark.parametrize('name', sorted(DRIVERS), ids=sorted(DRIVERS))
def test_android_app(world, device, name):
    app = MATRIX[name]
    with httpx.Client(timeout=300, follow_redirects=True) as http:
        apk, version = apks.fetch(app['apk'], http)
    device.install(apk, app['package'])
    device.launch(app['package'])
    driver = DRIVERS[name](device, app['package'], world)
    try:
        steps.run(name, app, {'login': driver.login, 'view': driver.view, 'post': driver.post}, version=version,
                  on_failure=lambda step: device.screenshot(RESULTS / f'{name}-{step}.png'))
    finally:
        device.stop(app['package'])
