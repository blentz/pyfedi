"""The client-app harness's emulator helper (interop/apps/android_device.py), against a fake device: launching an app
waits for it in front, through a transient adb error, and gives up after its deadline."""
import sys
import types

import pytest

# The unit suite's image has no adbutils or uiautomator2 (they drive a real emulator): stand-ins, for the import alone
for _name in ('adbutils', 'uiautomator2'):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from interop.apps import android_device  # noqa: E402  (after the stand-ins)
from interop.apps.android_device import Device  # noqa: E402


class AdbError(Exception):
    """adb's error as the harness meets it: a shell whose output came back cut short."""


class FakeUi:
    def __init__(self, answers):
        self.answers, self.started = list(answers), []

    def app_start(self, package, wait=False, stop=False):
        assert not wait   # uiautomator2's own wait reads `ps -A`, whose output adb can cut short
        self.started.append((package, stop))

    def app_current(self):
        answer = self.answers.pop(0) if self.answers else {'package': 'other'}
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeAdb:
    def shell(self, cmd):
        return ''


def device(answers) -> Device:
    found = object.__new__(Device)
    found.ui, found.adb = FakeUi(answers), FakeAdb()
    return found


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(android_device.time, 'sleep', lambda seconds: None)


def test_launch_waits_through_a_transient_adb_error_until_the_app_is_in_front():
    d = device([AdbError('shell output invalid'), {'package': 'launcher'}, {'package': 'org.app'}])
    d.launch('org.app')
    assert d.ui.started == [('org.app', True)] and d.ui.answers == []


def test_launch_gives_up_when_the_app_never_comes_to_the_front(monkeypatch):
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(android_device.time, 'monotonic', lambda: next(clock))
    with pytest.raises(AssertionError, match='org.app is not in front'):
        device([]).launch('org.app')
