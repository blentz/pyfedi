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


class Shell:
    """adbutils' v1 shell as the harness meets it: some answers come back cut short."""

    def __init__(self, answers):
        self.answers, self.ran = list(answers), []

    def __call__(self, device, cmdargs, timeout=None):
        self.ran.append(cmdargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def cut_short():
    return AdbError('shell output invalid', 'ime list -s -a; echo X4EXIT:$?', b'com.example/.Ime\n')


def test_a_read_cut_short_is_asked_again():
    shell = Shell([cut_short(), 'ime-list'])
    assert android_device.retrying_reads(shell)(object(), 'ime list -s -a') == 'ime-list'
    assert shell.ran == ['ime list -s -a', 'ime list -s -a']


def test_a_read_cut_short_every_time_gives_up():
    shell = Shell([cut_short(), cut_short(), cut_short()])
    with pytest.raises(AdbError, match='shell output invalid'):
        android_device.retrying_reads(shell)(object(), 'dumpsys window')
    assert len(shell.ran) == 3


def test_a_command_that_changes_something_is_never_run_twice():
    """Its output was cut short after it ran: typing again would type twice."""
    shell = Shell([cut_short()])
    with pytest.raises(AdbError):
        android_device.retrying_reads(shell)(object(), 'input text hello')
    assert shell.ran == ['input text hello']


def test_any_other_error_is_not_retried():
    shell = Shell([AdbError('device offline')])
    with pytest.raises(AdbError, match='offline'):
        android_device.retrying_reads(shell)(object(), 'ime list -s -a')
    assert len(shell.ran) == 1


def test_it_is_put_on_adbutils_device_once(monkeypatch):
    class Base:
        def _shell_v1(self, cmdargs, timeout=None):
            return 'ok'
    monkeypatch.setattr(android_device, '_AdbDevice', Base)
    android_device.install_read_retry()
    android_device.install_read_retry()
    assert Base._shell_v1.retries_reads and Base._shell_v1.__wrapped__.__name__ == '_shell_v1'
    assert Base()._shell_v1('getprop ro.build') == 'ok'


def test_without_adbutils_there_is_nothing_to_put_it_on(monkeypatch):
    monkeypatch.setattr(android_device, '_AdbDevice', None)
    android_device.install_read_retry()   # the unit suite's stand-in module: nothing to do, and no error
