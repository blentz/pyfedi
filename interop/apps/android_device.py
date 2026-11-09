"""The emulator as the apps-runner sees it: adb over the network (android-emulator:5037, ANDROID_ADB_SERVER_HOST and
_PORT, which adbutils reads), UI automation through uiautomator2. Every wait is bounded."""
import re
import time
from pathlib import Path

import adbutils
import uiautomator2


class Device:
    def __init__(self):
        devices = adbutils.adb.device_list()
        if not devices:
            raise RuntimeError('android: the emulator has no device on its adb server')
        self.adb = devices[0]
        self.ui = uiautomator2.connect(self.adb.serial)
        self.ui.implicitly_wait(20)   # how long an element's click, text or wait looks for it before failing

    def install(self, apk: Path, package: str) -> None:
        """A clean install: no data from an earlier run (an app remembering a login would hide a broken login)."""
        if package in self.adb.list_packages():
            self.adb.uninstall(package)
        self.adb.install(str(apk), nolaunch=True, flags=['-r', '-g'])

    def launch(self, package: str) -> None:
        self.adb.shell(['logcat', '-c'])   # a failure's log then starts at this app
        self.ui.app_start(package, wait=True, stop=True)

    def stop(self, package: str) -> None:
        self.ui.app_stop(package)

    def version(self, package: str) -> str:
        return self.ui.app_info(package).get('versionName') or '?'

    def wait_text(self, *texts: str, timeout: float = 30.0, contains: bool = True):
        """The first element showing any of `texts` (as text or as content description, ignoring case: Material
        buttons show their labels in capitals), or AssertionError."""
        deadline = time.monotonic() + timeout
        while True:
            for text in texts:
                pattern = f'(?is).*{re.escape(text)}.*' if contains else f'(?is){re.escape(text)}'
                for found in (self.ui(textMatches=pattern), self.ui(descriptionMatches=pattern)):
                    if found.exists:
                        return found
            if time.monotonic() > deadline:
                raise AssertionError(f'android: none of {texts!r} showed in {timeout}s; on screen: {self.screen_text()[:600]}')
            time.sleep(0.5)

    def tap(self, *texts: str, timeout: float = 30.0, exact: bool = False) -> None:
        self.wait_text(*texts, timeout=timeout, contains=not exact).click()

    def showing(self, text: str, exact: bool = False) -> bool:
        """Whether `text` shows now (no waiting), as wait_text matches it."""
        pattern = f'(?is){re.escape(text)}' if exact else f'(?is).*{re.escape(text)}.*'
        return self.ui(textMatches=pattern).exists or self.ui(descriptionMatches=pattern).exists

    def tap_tab(self, label: str, timeout: float = 30.0) -> None:
        """Tap the lowest element labelled `label`: a bottom tab bar's, when a page shows the same label higher up."""
        pattern = f'(?is){re.escape(label)}(\\s.*)?'   # Flutter appends to a tab's label ("Search\nTab 2 of 5")
        deadline = time.monotonic() + timeout
        while True:
            # read again each round: a WebView re-renders under us, and an element listed a moment ago can be gone
            bounds = []
            for selector in (self.ui(textMatches=pattern), self.ui(descriptionMatches=pattern)):
                try:
                    bounds += [element.info['bounds'] for element in selector]
                except uiautomator2.UiObjectNotFoundError:
                    pass
            if bounds:
                lowest = max(bounds, key=lambda b: b['bottom'])
                self.ui.click((lowest['left'] + lowest['right']) // 2, (lowest['top'] + lowest['bottom']) // 2)
                return
            if time.monotonic() > deadline:
                raise AssertionError(f'android: no tab {label!r} in {timeout}s; on screen: {self.screen_text()[:600]}')
            time.sleep(0.5)

    def tap_until(self, tap, *expected: str, attempts: int = 3, wait: float = 10.0):
        """`tap()`, then wait for any of `expected`; tap again if it does not show. An app that has just started, or
        whose keyboard is still animating, can swallow a tap: the second lands. Bounded, and the last failure says
        what the screen showed."""
        for attempt in range(attempts):
            tap()
            try:
                return self.wait_text(*expected, timeout=wait)
            except AssertionError:
                if attempt == attempts - 1:
                    raise

    def wait_foreground(self, package: str, timeout: float = 30.0) -> None:
        """Until `package` is the app on screen: back from a browser it sent somewhere, say."""
        deadline = time.monotonic() + timeout
        while self.ui.app_current().get('package') != package:
            if time.monotonic() > deadline:
                raise AssertionError(f'android: {package} never came back to the screen in {timeout}s; on screen: '
                                     f'{self.ui.app_current().get("package")}: {self.screen_text()[:400]}')
            time.sleep(0.5)

    def type_focused(self, text: str) -> None:
        """Type into whatever has focus, through the keyboard: for a field the hierarchy does not show (Flutter leaves
        one under the keyboard out). `input text` takes %s for a space."""
        self.adb.shell(['input', 'text', text.replace(' ', '%s')])

    def next_field(self) -> None:
        """Tab: focus moves to the next field, as Flutter's focus traversal does on a hardware keyboard."""
        self.adb.shell(['input', 'keyevent', 'KEYCODE_TAB'])
        time.sleep(0.5)

    def reveal(self, text: str, swipes: int = 4) -> None:
        """Scroll `text` into view by swiping up: Flutter leaves a control under the keyboard out of the hierarchy, and
        its sheets close on back, so hiding the keyboard is not an option there."""
        for _ in range(swipes):
            if self.showing(text):
                return
            self.ui.swipe_ext('up', scale=0.4)
            time.sleep(0.8)
        self.wait_text(text, timeout=5)

    def hide_keyboard(self) -> None:
        """Back, only while the keyboard shows: with no keyboard, back closes the app's sheet or screen instead."""
        # the keyboard's own window is in the hierarchy uiautomator dumps while it shows (dumpsys input_method's
        # mInputShown is gone from Android 17)
        if re.search(r'package="[^"]*inputmethod[^"]*"', self.ui.dump_hierarchy()):
            self.ui.press('back')
            time.sleep(0.5)

    def type_into(self, field, text: str) -> None:
        field.click()
        field.clear_text()
        field.set_text(text)

    def screen_text(self) -> str:
        """What the screen shows, for a failure's detail: every text and content description in the hierarchy."""
        xml = self.ui.dump_hierarchy()
        parts = []
        for attribute in ('text="', 'content-desc="'):
            for chunk in xml.split(attribute)[1:]:
                value = chunk.split('"', 1)[0]
                if value:
                    parts.append(value)
        return ' | '.join(parts)

    def screenshot(self, path: Path) -> None:
        """The screen, and beside it the device log since the app launched and the view hierarchy: what went wrong
        underneath it, and what each control is called."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.ui.screenshot(str(path))
        path.with_suffix('.log').write_text(self.adb.shell(['logcat', '-d', '-v', 'time']))
        path.with_suffix('.xml').write_text(self.ui.dump_hierarchy())   # every view, with its resource id
