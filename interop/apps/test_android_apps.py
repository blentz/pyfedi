"""The PieFed-capable Android apps, each the newest release (apks.py), installed clean on the emulator and driven as
alice: log in, show the seeded post, comment on it. The comment carries a marker found afterwards through PieFed's API.

Viewing is a search for the seeded post, as on a busy instance: a feed's first screen holds whatever else the
stack carries (after `interop/run.sh test` it is full of Loops, Mastodon and Pixelfed posts).

The emulator trusts the harness CA as a system CA (android/boot.sh) and resolves piefed.test through the stack's DNS,
so the apps reach PieFed exactly as they would a public instance. Controls are found by resource id where the app has
them (native apps) and by the text or content description they show otherwise.
"""
import io
import ssl
import time

import httpx
import pytest

from interop.apps import apks, steps
from interop.apps.conftest import ALICE, PHOTO_TITLE, SEED_TITLE, TESS, TUBE, TUBE_TITLE, VIDEO_FIXTURE, find_comment, find_post, \
    marker, png, tube_video
from interop.apps.matrix import RESULTS, load_matrix
from interop.clients import CA, wait_for

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
        field = d.ui(className='android.widget.EditText', instance=0)
        d.type_into(field, QUERY)
        # The keyboard, just shown, can swallow the first enter: no search runs, and the query sits in the field. A
        # repeated enter must go to the field: with focus elsewhere it activated the You tab, whose accounts sheet
        # then covered the results. Focus the field again before each.
        d.tap_until(lambda: (field.click(), d.ui.press('enter')), SEED_TITLE, wait=20)   # in the Posts results
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
        d.hide_keyboard()   # the server field's keyboard, still up, kept the form's second input out of the tree once
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
        # a cold run's send_keys left the editor empty (the WebView's editor not yet taking input), and Post sends
        # nothing then: type again through the keyboard until the text shows
        for _ in range(3):
            if d.ui(textContains=text).wait(timeout=5):
                break
            editor.click()
            d.type_focused(text)
        d.wait_text(text, timeout=10)
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


def piefed_consent(d, package, signed_in=None):
    """PieFed's half of an OAuth login (plan W0.2), in the browser the app opened: its login page as alice, the consent
    page's Allow, and then the app itself back on screen, holding the code PieFed sent it, and signed in. The login
    form's fields are found by order: they are the page's only inputs (its nav has no search box logged out)."""
    d.wait_text('Log In', 'Allow', timeout=60, contains=False)   # PieFed's buttons, not an app's "Log in with…"
    if not d.showing('Allow', exact=True):
        d.type_into(d.ui(className='android.widget.EditText', instance=0), ALICE[0])
        d.type_into(d.ui(className='android.widget.EditText', instance=1), ALICE[1])
        d.hide_keyboard()
        log_in = d.ui(className='android.widget.Button', text='Log In')   # not the "Log in" heading

        def submit():
            if log_in.exists:   # gone once the form went: the next page is only slow
                log_in.click()
        # a tap as the keyboard goes down can be swallowed, leaving the form as it was
        log_in.wait(timeout=30)
        d.tap_until(submit, 'to use your account', wait=30)
    # an app asking many scopes (Pixelfed's eight) pushes Allow below the fold: it is scrolled into view, the button
    # and not the heading "Allow <app> to use your account?", and clear of PieFed's fixed bottom nav on a phone
    d.wait_text('to use your account', timeout=60)
    d.reveal('Allow', swipes=6, exact=True, above=0.8)
    # Chrome can swallow a tap while the consent page settles: PieFed then gets no POST and the page stays. Tap
    # again while it still shows Allow; once the POST goes, the app comes back.
    for attempt in range(3):
        d.tap('Allow', exact=True, timeout=60)
        try:
            d.wait_foreground(package, timeout=20)
            break
        except AssertionError:
            if attempt == 2 or not d.showing('Allow', exact=True):
                raise
    d.wait_text(*(signed_in or SIGNED_IN), timeout=60)


def chrome_first_run(d):
    """A fresh Chrome may ask about itself before it shows the custom tab (android/boot.sh turns most of it off)."""
    for question in ('Use without an account', 'Accept & continue', 'No thanks'):
        if d.showing(question, exact=True):
            d.tap(question, exact=True)


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

    def post(self):
        # from the search the view left open: the seeded post's thread, its reply, the marker
        d, text = self.device, marker('tusky-android')
        d.tap(SEED_TITLE)
        # A thread item is one accessible view, its buttons folded into accessibility actions uiautomator cannot run:
        # the seeded post's reply icon is at its item's bottom left (item_status_detailed's action row)
        d.wait_text('Favorite', timeout=30)   # the thread, its seeded post detailed
        first = d.ui(className='android.view.View', clickable=True, instance=0).info['bounds']
        d.ui.click(first['left'] + 75, first['bottom'] - 60)
        d.type_into(d.ui(resourceId=self.package + ':id/composeEditField'), text)
        # its send button sits above the keyboard; back would close the composer (and ask to save a draft)
        d.ui(resourceId=self.package + ':id/composeTootButton').click(timeout=30)
        find_comment(self.world['alice'], self.world['seed']['id'], text, timeout=60)
        return f'replied {text!r} to the seeded post; PieFed has it as a comment'


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

    def post(self):
        d, text = self.device, marker('mastodon-android')
        d.tap(SEED_TITLE)
        d.tap('Reply to', timeout=30)   # the thread's reply bar ("Reply to dora"), which opens the composer
        field = d.ui(className='android.widget.EditText', instance=0)
        field.wait(timeout=30)
        # set, not typed: typing after the @dora it holds lost its keys, and the bare mention it then posted was a
        # duplicate the next time
        d.type_into(field, text)
        # Publish is in the composer's top bar, above the keyboard: a back press for the keyboard could close the
        # composer instead while it is still animating
        d.tap('Publish', exact=True)
        find_comment(self.world['alice'], self.world['seed']['id'], text, timeout=60)
        return f'replied {text!r} to the seeded post; PieFed has it as a comment'


class NewPipe:
    """NewPipe (TeamNewPipe/NewPipe): native, with no accounts. Settings > Content > PeerTube instances takes an
    instance's URL (it reads /api/v1/config) and makes it the PeerTube service's; the drawer's service picker lists that
    service under the instance's name, and it opens on its Trending kiosk, /api/v1/videos?sort=-trending. The video's
    page plays its file. It browses tube.test: piefed.test is private (R219), and NewPipe cannot sign in."""

    def __init__(self, device, package, world):
        self.device, self.world, self.package = device, world, package
        self.id = lambda name: f'{package}:id/{name}'

    def login(self):
        raise AssertionError('NewPipe has no login: the matrix marks it inapplicable')

    def _first_run(self):
        """NewPipe opens on a notice dialog ("Keep Android Open"), shown a moment after launch: wait for it, accept it.
        A version that shows none gets on with it once the drawer shows."""
        d = self.device
        d.wait_text('Keep Android Open', 'Open Drawer', timeout=60)
        while d.ui(resourceId='android:id/button1').exists:
            d.ui(resourceId='android:id/button1').click()
            d.ui(resourceId='android:id/button1').wait_gone(timeout=10)

    def _add_instance(self):
        d = self.device
        d.ui(description='Open Drawer').click(timeout=30)
        d.tap('Settings', exact=True)
        d.tap('Content', exact=True)
        d.reveal('PeerTube instances')
        d.tap('PeerTube instances', exact=True)
        d.ui(resourceId=self.id('addInstanceButton')).click(timeout=30)
        d.type_into(d.ui(className='android.widget.EditText', instance=0), f'https://{TUBE}')
        d.tap('OK', exact=True)
        d.wait_text(TUBE, timeout=60)   # validated: PieFed answered /api/v1/config
        d.ui(text=TUBE).sibling(className='android.widget.RadioButton').click(timeout=30)
        for _ in range(3):
            d.ui.press('back')

    def _choose_peertube(self):
        d = self.device
        d.ui(description='Open Drawer').click(timeout=30)
        d.ui(resourceId=self.id('drawer_header_action_button')).click(timeout=30)   # the service picker
        d.tap(TUBE, exact=True)   # the PeerTube service, under its instance's name (/api/v1/config)

    def view(self):
        video = tube_video()
        d = self.device
        self._first_run()
        self._add_instance()
        self._choose_peertube()
        d.wait_text(TUBE_TITLE, timeout=90)
        d.tap(TUBE_TITLE)
        d.wait_text(video['account']['displayName'], timeout=60)   # the video's page, its uploader under it
        d.ui(resourceId=self.id('surfaceView')).wait(timeout=60)    # the player, playing the file
        # its quality menu offers the renditions (W2.1b): the controls show on a tap, the menu on the quality button
        # (NewPipe may start below 720p on mobile data, so the button may show any of them)
        d.tap_until(lambda: d.ui(resourceId=self.id('surfaceView')).click(), '720p', '480p', '360p', wait=10)
        d.ui(resourceId=self.id('qualityTextView')).click(timeout=15)
        for label in ('720p', '480p', '360p'):
            d.wait_text(label, timeout=15)
        return f"NewPipe lists {TUBE_TITLE!r} from {TUBE}, plays it, and offers its 480p and 360p renditions"

    def post(self):
        raise AssertionError('NewPipe cannot upload: the matrix marks it inapplicable')


class PeerTubeApp:
    """PeerTube's own app (Framasoft, framasoft/peertube/mobile-application; Flutter, so its labels are content
    descriptions). Its onboarding's "I know where to go" opens the platform list with a login offer to follow; the
    unlabelled plus ("Add a platform") takes a host, which it checks against /api/v1/config (serverVersion >= 7.2.0,
    D4); the platform's page offers "Login to <name>" (the password grant, W2.2). It browses tube.test, where tess and
    her video live: piefed.test is private (R219), and an account belongs to its own domain."""

    def __init__(self, device, package, world):
        self.device, self.world = device, world

    def _type(self, field, text: str) -> None:
        """Its fields take no set_text: focus one with a tap and type through the keyboard."""
        field.click(timeout=30)
        time.sleep(0.5)
        self.device.type_focused(text)

    def _add_platform(self):
        d = self.device
        d.wait_text('I know where to go', timeout=90, contains=False)
        time.sleep(2)   # its picture loads and moves the buttons: a tap too soon lands on "Discover content" above
        d.tap('I know where to go', exact=True)
        add = d.ui(description='Add a platform')
        if not add.wait(timeout=15):   # it went to Explore all the same, whose platform list is empty offline
            d.tap_tab('Platforms')
        add.click(timeout=30)
        self._type(d.ui(className='android.widget.EditText', instance=0), TUBE)
        d.hide_keyboard()
        d.tap('Connect', exact=True)
        d.wait_text('Confirm', 'Login to', timeout=60)   # a confirm dialog first, unless told not to show it again
        if d.showing('Confirm', exact=True):
            d.tap('Confirm', exact=True)

    def login(self):
        d = self.device
        self._add_platform()
        d.tap('Login to', timeout=60)   # "Login to <the instance's name>", offered once the platform opens
        d.wait_text('Or e-mail address', timeout=30)   # the login screen: its fields show their helper text alone
        self._type(d.ui(className='android.widget.EditText', instance=0), TESS[0])
        self._type(d.ui(className='android.widget.EditText', instance=1), TESS[1])   # Tab does not reach it
        d.hide_keyboard()
        d.tap_tab('Login')   # the button, the lowest "Login": the screen's heading says it too
        forgotten = d.wait_text("I've forgotten my password", timeout=10)
        assert forgotten.wait_gone(timeout=60), f'the login screen stayed; on screen: {d.screen_text()[:600]}'
        d.ui(description='Manage accounts').click(timeout=60)   # the account sheet names who is signed in
        d.wait_text(f'{TESS[0]}@{TUBE}', TESS[0], timeout=30)
        d.ui.press('back')
        return f'logged in to {TUBE} as {TESS[0]} with its username and password'

    def view(self):
        d = self.device
        video = tube_video()
        d.tap_tab('Platforms')   # the platform's page: its videos under "Online videos"
        # seeded after the page loaded, on a cold stack: pull it down to load again
        d.tap_until(lambda: d.ui.swipe_ext('down', scale=0.6), TUBE_TITLE, wait=15)
        d.tap(TUBE_TITLE)   # its item ("Video item, <duration>, <channel>, <title>, ...")
        d.wait_text('comment', 'Comments', timeout=60)   # the video's page: its comments tab ("0 comment")
        text = self._comment(video)
        return f'it lists {TUBE_TITLE!r} on {TUBE}, opens it and comments {text!r}, which PieFed has'

    def _comment(self, video) -> str:
        d, text = self.device, marker('peertube-android-comment')
        # the comment list loads as it scrolls into view, under the description; its button is "Comment" exactly
        # (reveal would stop at the "0 comment" tab)
        for _ in range(6):
            if d.showing('Comment', exact=True):
                break
            d.ui.swipe_ext('up', scale=0.4)
            time.sleep(1)
        d.tap('Comment', exact=True, timeout=30)
        field = d.ui(className='android.widget.EditText', instance=0)
        self._type(field, text)
        bounds = field.info['bounds']
        middle = (bounds['top'] + bounds['bottom']) // 2
        # the send button has no label: the button right of the field, at its height (Flutter marks it focusable, not
        # clickable)
        send = [element.info['bounds'] for element in d.ui(className='android.widget.Button')
                if element.info['bounds']['left'] >= bounds['right'] - 5
                and element.info['bounds']['top'] <= middle <= element.info['bounds']['bottom']]
        assert send, f'no send button right of the comment field; on screen: {d.screen_text()[:600]}'
        d.ui.click((send[0]['left'] + send[0]['right']) // 2, (send[0]['top'] + send[0]['bottom']) // 2)
        find_comment(self.world['alice'], video['id'], text, timeout=60)
        d.wait_text(text, timeout=30)
        return text

    def post(self):
        """Upload a clip to tess's channel through the app's own screens (W2.3, ruling U4): Creator > Publish >
        Upload file opens Android's photo picker on the clip put in Movies; its preview > Next; the channel >
        Start upload; the details (the title, from the file's name, set again) > Next > Publish my video. PieFed
        lists the video once its media pipeline has made it ready."""
        d, text = self.device, marker('peertube-android')
        clip = f'/sdcard/Movies/{text}.mp4'
        d.adb.sync.push(VIDEO_FIXTURE, clip)
        d.scan_media()
        d.tap_tab('Creator')
        d.tap('Publish', exact=True, timeout=30)
        d.tap('Upload file', exact=True)
        picked = d.ui(descriptionContains='Video')   # the picker's tiles: "Video taken on <date>"
        picked.click(timeout=60)
        d.tap('Next', exact=True, timeout=60)   # the preview
        d.wait_text('Select a video channel', timeout=60)
        d.tap('tube', exact=True)
        d.tap('Start upload', exact=True)
        title = d.ui(className='android.widget.EditText', instance=0)   # the details' first field, the title
        title.wait(timeout=90)
        # the picker hands the app a copy with a name of its own, which the title is made from: cleared with the
        # field's own Clear (Flutter's fields take no clear_text), then typed
        d.ui(description='Clear').click(timeout=15)
        self._type(title, text)
        d.hide_keyboard()
        d.tap('Next', exact=True)
        d.tap('Publish my video', exact=True, timeout=60)
        with httpx.Client(base_url=f'https://{TUBE}', verify=ssl.create_default_context(cafile=CA), timeout=15) as http:
            def listed():
                found = http.get('/api/v1/videos', params={'sort': '-publishedAt', 'count': 100})
                return next((video for video in found.raise_for_status().json()['data'] if video['name'] == text),
                            None)
            video = wait_for(listed, timeout=300, interval=3)
        return f"uploaded {text!r} to tess's channel with PeerTube's resumable upload; PieFed lists it as video " \
               f"{video['uuid']}"


class Jerboa:
    """Jerboa (LemmyNet/jerboa; Jetpack Compose). It asks nodeinfo who a server is and refuses one that is not Lemmy;
    PieFed tells it Lemmy 0.19.11 (plan D3, ruling L1) and serves /api/v3 (W3). Its login is in the drawer: Menu, the
    account header ("Anonymous") > Add account > Instance, Email or username, Password > Login, the
    lowest (the screen's title says it too). Its search finds communities only: the seeded post is found in alice's
    home feed, her subscriptions, which hold dora's community."""

    def __init__(self, device, package, world):
        self.device, self.world = device, world

    def _startup_dialogs(self):
        """A fresh install opens on dialogs over its feed, one after another: the F-Droid build's notice about Google's
        developer verification (OK) and its changelog (Done). Each is closed until the feed's Menu shows."""
        d = self.device
        for _ in range(4):
            if d.ui(description='Menu').wait(timeout=15) and not d.showing('OK', exact=True) \
                    and not d.showing('Done', exact=True):
                return
            for button in ('OK', 'Done'):
                if d.showing(button, exact=True):
                    d.tap(button, exact=True)
                    time.sleep(1)

    def login(self):
        d = self.device
        self._startup_dialogs()
        d.ui(description='Menu').click(timeout=60)
        # the account header ("Anonymous") opens the account list; its arrow's description is no guide to its state
        d.tap_until(lambda: d.tap('Anonymous', exact=True), 'Add account')
        d.tap('Add account', exact=True)
        d.wait_text('Email or username', timeout=30)
        for index, text in enumerate(('piefed.test', ALICE[0], ALICE[1])):
            d.type_into(d.ui(className='android.widget.EditText', instance=index), text)
        d.hide_keyboard()
        d.tap_tab('Login')   # the button, the lowest "Login"
        # it goes back to the feed with its drawer still open, whose header names her once the site document has
        # (my_user, read with no null check)
        d.wait_text(f'{ALICE[0]}@piefed.test', timeout=90)
        if d.showing('Close navigation menu'):
            d.ui(description='Close navigation menu').click(timeout=15)
        return 'logged in as alice@piefed.test; nodeinfo told it Lemmy'

    def view(self):
        d = self.device
        d.reveal(SEED_TITLE, swipes=6)
        return 'its home feed, her subscriptions, shows the seeded post'

    def post(self):
        d, text = self.device, marker('jerboa-android')
        d.tap(SEED_TITLE)
        d.tap_until(lambda: d.ui(description='Reply to post').click(timeout=30), 'Type your comment')
        editor = d.ui(className='android.widget.EditText', instance=0)
        editor.wait(timeout=30)
        d.type_into(editor, text)
        d.ui(description='Send reply').click(timeout=15)
        find_comment(self.world['alice'], self.world['seed']['id'], text, timeout=60)
        d.wait_text(text, timeout=30)
        return f'commented {text!r} on the seeded post with the Lemmy API; PieFed has it'


class PixelfedApp:
    """Pixelfed's own app (pixelfed/pixelfed-rn, React Native; plan W1.4a). Its release reads /api/v2.1/config before
    signing in and goes on only for a Pixelfed of 0.14.4 or later, which PieFed says on Pixelfed's paths alone
    (ruling X1). Its welcome screen's SERVER button > the server > Sign
    in; PieFed's pages open in the browser, and PieFed sends the code back to pixelfed://. Its tabs are icons labelled
    Home, Trending, Notifications, Search and Profile; the camera is the home screen's top left. A profile shows only
    its photos and videos (pf_type, `_pe=1`)."""

    def __init__(self, device, package, world):
        self.device, self.world, self.package = device, world, package


    def login(self):
        d = self.device
        # its welcome screen: a SERVER button naming pixelfed.social, then Sign in
        server = d.ui(descriptionContains='SERVER')
        server.wait(timeout=90)
        # its server sheet opens with the current server selected and the keyboard up; the sheet's field is not
        # always in the hierarchy, so the server is typed through the keyboard, over the selection
        d.tap_until(lambda: server.click(timeout=15), 'Pick your server', wait=10)   # a tap can be swallowed
        for _ in range(3):
            d.type_focused('piefed.test')
            if d.ui(text='Use piefed.test').wait(timeout=10):
                break
            field = d.ui(className='android.widget.EditText', instance=0)
            if field.exists:   # a keyboard that added or kept a character: set the field itself
                field.set_text('piefed.test')
                if d.ui(text='Use piefed.test').wait(timeout=10):
                    break
        d.hide_keyboard()
        d.wait_text('Use piefed.test', timeout=30, contains=False)
        d.tap_until(lambda: d.tap('Use piefed.test', exact=True), 'Sign in', wait=15)   # a tap can be swallowed
        # Sign in reads /api/v2.1/config (a Pixelfed of 0.14.4 or later) and opens PieFed's pages in the browser
        d.tap_until(lambda: d.tap('Sign in', exact=True), 'Log In', 'Allow', wait=30)
        chrome_first_run(d)
        piefed_consent(d, self.package, signed_in=('Your story', 'Trending'))
        return 'logged in as alice through OAuth; its /api/v2.1/config check was told Pixelfed 0.15.2'

    def view(self):
        d = self.device
        d.tap_tab('Search')
        # its search screen: one field, whose results (/api/v2/search) come as it is typed into
        d.type_into(d.ui(className='android.widget.EditText', instance=0), 'dora')
        d.hide_keyboard()   # its suggestion strip offers a "dora" too
        d.ui(descriptionStartsWith='dora, @dora').click(timeout=60)   # her account, from the results' People
        # the grid's tiles carry no label: its first tile is the first thing to tap below the Posts and Reposts tabs
        d.wait_text('Reposts', timeout=60)
        tabs_bottom = d.ui(description='Reposts').info['bounds']['bottom']
        d.tap_until(lambda: d.ui.click(d.ui.info['displayWidth'] // 6, tabs_bottom + d.ui.info['displayWidth'] // 6),
                    PHOTO_TITLE, wait=20)
        d.wait_text(PHOTO_TITLE, timeout=60)
        return f"dora's profile shows her photo {PHOTO_TITLE!r} in its grid (pf_type photo), and it opens"

    def post(self):
        """A photo post through its camera, as its people post: the home screen's camera (top left) > its gallery >
        Android's photo picker on a picture put in Pictures > Add > its Edit screen's Next > a caption > Share. The
        app uploads the photo (/api/v2/media) and posts it (/api/v1/statuses); PieFed has it in alice's microblog as
        an image post."""
        d, text = self.device, marker('pixelfed-android')
        d.adb.sync.push(io.BytesIO(png(320, 240)), f'/sdcard/Pictures/{text}.png')
        d.scan_media()
        for _ in range(4):   # view left it on dora's photo, a screen over her profile and the search, with no tabs
            if d.ui(description='Home').exists:
                break
            d.ui.press('back')
            time.sleep(1)
        d.tap_tab('Home')
        # the home screen's camera, top left of its header, is an unlabelled icon
        width, height = d.ui.info['displayWidth'], d.ui.info['displayHeight']
        d.tap_until(lambda: d.ui.click(width * 0.075, height * 0.08), 'Photo', wait=20)
        # its camera: the gallery, bottom left, is an icon font's glyph (Ionicons' images)
        d.ui(description='\uf391').click(timeout=30)
        d.ui(descriptionContains='Photo taken').click(timeout=60)   # the photo picker's tile of the pushed picture
        d.tap('Add (1)', exact=True, timeout=30)   # the picker takes several: its Add confirms the one
        d.wait_text('Filter', 'Crop', timeout=60)   # its Edit screen: filters and crop, as picked
        d.tap('Next', exact=True)
        # New Post opens with the keyboard up, and its fields are out of the hierarchy while it shows
        d.wait_text('New Post', timeout=30)
        # its fields, the photo's alt text then the caption, are below the photo, where the keyboard it opens with
        # covers them, and a list leaves what it does not show out of the hierarchy: the form is scrolled up to them
        caption = d.ui(className='android.widget.EditText', instance=1)
        for _ in range(6):
            if caption.wait(timeout=3):
                break
            d.ui.swipe(width // 2, int(height * 0.45), width // 2, int(height * 0.15), duration=0.3)
        # the field leaves the hierarchy again as the layout shifts: tapped where it was, and typed through the keyboard
        bounds = caption.info['bounds']
        d.ui.click((bounds['left'] + bounds['right']) // 2, (bounds['top'] + bounds['bottom']) // 2)
        time.sleep(1)
        d.type_focused(text)   # not to be seen in the hierarchy while the keyboard shows: PieFed's post says it
        time.sleep(1)
        d.tap('Share', exact=True)   # in the header, above the keyboard
        alice = self.world['alice']
        found = find_post(alice, alice.microblogs_id(), text, timeout=90)['post']
        # a photo, not words alone: an image post once PieFed's media pipeline has the upload (a link until then)
        wait_for(lambda: alice.get_post(found['id'])['post']['post_type'] == 'Image', timeout=120, interval=3)
        return f"posted a photo captioned {text!r} through its camera; PieFed has it as alice's image post " \
               f"{found['id']}"

DRIVERS = {'newpipe-android': NewPipe, 'summit-android': Summit, 'voyager-android': VoyagerAndroid, 'thunder-android': Thunder,
           'tusky-android': Tusky, 'mastodon-android': MastodonAndroid, 'peertube-android': PeerTubeApp,
           'jerboa-android': Jerboa, 'pixelfed-android': PixelfedApp}


@pytest.mark.parametrize('name', sorted(DRIVERS), ids=sorted(DRIVERS))
def test_android_app(world, device, name):
    app = MATRIX[name]
    with httpx.Client(timeout=300, follow_redirects=True) as http:
        apk, version = apks.fetch(app['apk'], http)
    if app['apk'].get('extract_native_libs'):
        apk = apks.with_native_libs_extracted(apk)
    device.reset_shared_state()   # each app starts as if it were the first: no browser session, no pushed media
    device.install(apk, app['package'])
    device.launch(app['package'])
    driver = DRIVERS[name](device, app['package'], world)
    try:
        steps.run(name, app, {'login': driver.login, 'view': driver.view, 'post': driver.post}, version=version,
                  on_failure=lambda step: device.screenshot(RESULTS / f'{name}-{step}.png'))
    finally:
        device.stop(app['package'])
