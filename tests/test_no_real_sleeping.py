"""No test waits out a retry delay in app code.

`get_request` retries a failed fetch after `sleep(random.randint(3, 10))`, and the
ActivityPub fetch helpers after `time.sleep(3)`. Before the conftest fixture became
autouse, only the files that opted in were spared: a profile of the coverage run put
"a server that never answers" tests at 7 to 17 seconds each, waiting real seconds.

The fixture neutralises sleeping in app code only -- every `from time import sleep`
binding and every `import time` module under app/ -- and leaves the real `time`
module alone, so a library's own polling is untouched, and a test that patches the
real module (`time.time`, `time.monotonic`) still reaches app code.
"""
import time

import app.activitypub.util
import app.community.util
import app.utils

# Long enough that a real sleep would be unmistakable, short enough that the RED run
# finishes inside the per-test timeout.
DELAY = 5


def _elapsed(sleep):
    started = time.monotonic()
    sleep(DELAY)
    return time.monotonic() - started


def test_a_from_time_import_sleep_binding_returns_at_once():
    assert _elapsed(app.utils.sleep) < 1
    assert _elapsed(app.community.util.sleep) < 1


def test_an_import_time_module_sleep_returns_at_once():
    assert _elapsed(app.activitypub.util.time.sleep) < 1


def test_the_real_time_module_still_sleeps():
    started = time.monotonic()
    time.sleep(0.05)
    assert time.monotonic() - started >= 0.05


def test_a_patch_to_the_real_time_module_still_reaches_app_code(monkeypatch):
    monkeypatch.setattr(time, 'time', lambda: 42.0)
    assert app.activitypub.util.time.time() == 42.0


def test_a_test_can_still_record_an_app_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr('app.utils.sleep', slept.append)
    app.utils.sleep(3)
    assert slept == [3]
