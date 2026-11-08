"""The PieFed-capable Android apps, each the newest release (apks.py), installed clean on the emulator and driven as
alice: log in, show the seeded post, comment on it. The comment carries a marker found afterwards through PieFed's API.

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
        d.ui(description='Home').click(timeout=15)
        d.wait_text('All@piefed.test', timeout=30)
        d.wait_text(SEED_TITLE, timeout=60)
        return 'its All@piefed.test feed lists the seeded post'

    def post(self):
        d, text = self.device, marker('summit-android')
        d.tap(SEED_TITLE)
        d.ui(resourceId=self.id('pa_reply')).click(timeout=30)
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
        d.tap('lemmy.zip', timeout=60)
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
        d.wait_text('alice@piefed.test', timeout=60)
        return 'logged in as alice@piefed.test'

    def view(self):
        d = self.device
        d.tap_tab('Posts')
        d.wait_text(SEED_TITLE, timeout=60)
        return 'its piefed.test feed lists the seeded post'

    def post(self):
        d, text = self.device, marker('voyager-android')
        d.tap(SEED_TITLE)
        d.tap('Reply')
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
    fields. Account > Manage Accounts > Add Account: instance (it then says "PieFed support is currently in beta"),
    username, password > Log in. Its sheet closes on back, so the keyboard is left up."""

    def __init__(self, device, package, world):
        self.device, self.world = device, world

    def login(self):
        d = self.device
        d.tap('Account', timeout=60)
        d.tap('Manage Accounts')
        d.tap('Add Account')
        d.type_into(d.ui(className='android.widget.EditText', instance=0), 'piefed.test')
        d.wait_text('PieFed support', timeout=30)   # it recognised PieFed (nodeinfo)
        d.type_into(d.ui(className='android.widget.EditText', instance=1), ALICE[0])
        d.type_into(d.ui(className='android.widget.EditText', instance=2), ALICE[1])
        d.reveal('Log in')   # under the keyboard
        d.tap('Log in')
        d.wait_text('Subscriptions', timeout=60)   # the logged-in home feed
        return 'logged in as alice; its feed is her subscriptions'

    def view(self):
        self.device.wait_text(SEED_TITLE, timeout=60)
        return 'her subscriptions feed lists the seeded post'

    def post(self):
        d, text = self.device, marker('thunder-android')
        d.tap(SEED_TITLE)
        d.ui(description='Reply').click(timeout=30)
        editor = d.ui(className='android.widget.EditText', instance=0)
        editor.wait(timeout=30)
        d.type_into(editor, text)
        d.ui(className='android.widget.Button', description='Create Comment').click(timeout=15)
        # not looked for on screen: the thread sorts by Top, so a new comment lands below the fold
        find_comment(self.world['alice'], self.world['seed']['id'], text, timeout=60)
        return f'commented {text!r} on the seeded post; PieFed has it'


DRIVERS = {'summit-android': Summit, 'voyager-android': VoyagerAndroid, 'thunder-android': Thunder}


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
