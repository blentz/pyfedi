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
        self.wait_text(label, timeout=timeout)
        pattern = f'(?is){re.escape(label)}'
        found = [element for selector in (self.ui(textMatches=pattern), self.ui(descriptionMatches=pattern))
                 for element in selector]
        lowest = max(found, key=lambda element: element.info['bounds']['bottom'])
        lowest.click()

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
        path.parent.mkdir(parents=True, exist_ok=True)
        self.ui.screenshot(str(path))
