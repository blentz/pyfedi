"""`check_user_application` -- the cross-instance ban check.

`app/shared/tasks/users.py:14-87`, one function, 58 statements and 20 branches.
THIS IS THE FIRST TARGET IN THE CAMPAIGN THAT IS NOT FEDERATION-SHAPED, and
nothing from the `app/shared/tasks/` Flag and Announce harnesses transfers. It
makes outbound HTTP with `httpx_client`, sleeps between requests, and randomises
both the payloads and the position of the real value inside them.

THREE CONTROLS, ALL PATCHED IN `app.shared.tasks.users`:

1. `sleep` -- imported BY VALUE at `:1` (`from time import sleep`), so the
   module holds its own reference and patching `time.sleep` would miss it.
   `:50` sleeps `random.randint(1, 30)` seconds PER DOMAIN. Unpatched, this
   file alone would dominate the suite's runtime.
2. `random.randint` -- stubbed to return its lower bound, which makes
   `ip_index` and `email_index` 0, fixes the fake octets, and makes `:50`
   sleep 1 (still patched away by control 1).
3. `httpx_client` -- replaced by a RECORDING DOUBLE, not respx.

WHY A DOUBLE AND NOT respx. One of this sub-project's three production changes
is that `email_response` is never closed, against `ip_response.close()` at
`:48`. respx CANNOT observe that: the `Response` never leaves the function, and
a respx-mocked response may already report `is_closed == True` before `close()`
is called -- an assertion on `is_closed` would pass identically before and after
the fix. That is the unfailable-assertion shape fact 132 exists to catch. The
double's `close()` appends to a list instead, so the assertion fails with one
entry and passes with two.

TWO SESSIONS ARE LIVE IN THIS FUNCTION AT ONCE. `users.py` never calls
`patch_db_session` -- it is one of D314's 21 unpatched task functions -- so
`application` is read through `get_task_session()` (`:15`, `:17`) while
`get_setting` reads through the global `db.session` (app/utils.py:204). Fixture
rows must be COMMITTED before the task runs, or the task session will not see
them.

`get_setting` IS NOT A CACHING TRAP. It is decorated `@cache.memoize` at
app/utils.py:202, which invites a defence against stale values across tests.
`.env.test` sets `CACHE_TYPE=NullCache`, so the memoization is inert under test
and every call queries the database. No defence is needed; do not build one.
"""

from types import SimpleNamespace

import pytest

from app import db
from app.models import UserRegistration
from app.shared.tasks.users import check_user_application
from app.utils import set_setting
from tests.factories import make_instance, make_user, make_user_registration

APPLICANT_IP = '203.0.113.7'
APPLICANT_EMAIL = 'applicant@example.com'


def _seed(ip=APPLICANT_IP, email=APPLICANT_EMAIL):
    """instance, user, application -- committed.

    `ip_address` AND `email` ARE SET HERE AND MUST STAY SET.
    `make_user` (tests/factories.py:40) sets `email` but leaves `ip_address`
    None, and `:36` inserts it into a list that `:39` passes to `','.join`.
    A None member raises TypeError INSIDE the `try` at `:27`, which `:75`
    catches, logs and continues past -- so `num_banned` stays 0, no warning is
    written, and a test asserting "no warning" PASSES FOR THE WRONG REASON.
    `test_the_seed_supplies_an_ip_address` below exists to make that
    regression loud.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    user = make_user(instance, 'applicant', local=True)
    user.ip_address = ip
    user.email = email
    db.session.commit()
    application = make_user_registration(user)
    return SimpleNamespace(instance=instance, user=user, application=application)


def _no_sleep(monkeypatch):
    """Suppress `:50`'s sleep. `users.py:1` does `from time import sleep`, so
    the name lives in the users module and patching `time.sleep` would miss
    it. Unpatched, `:50` sleeps up to 30 seconds PER DOMAIN.
    """
    monkeypatch.setattr('app.shared.tasks.users.sleep', lambda _seconds: None)


def _lowest_randint(monkeypatch):
    """Make `random.randint(a, b)` return `a`.

    Fixes `ip_index` (`:34`) and `email_index` (`:60`) at 0, so the real value
    is inserted at the FRONT of each list and the response index the code
    reads is 0. Also fixes the fake octets and makes `:50`'s argument 1.

    `random` is imported as a module at `:5`, so this patches the shared
    module for the test's duration; `monkeypatch` reverts it.
    """
    import random
    monkeypatch.setattr(random, 'randint', lambda a, _b: a)


class _Response:
    """A scripted stand-in for `httpx.Response` recording its own close.

    Only the three members `check_user_application` touches are provided:
    `status_code` (`:43`, `:69`), `json()` (`:44`, `:70`) and `close()`
    (`:48`, and -- after production change 3 -- the email leg).
    """

    def __init__(self, status_code, payload, closed):
        self.status_code = status_code
        self._payload = payload
        self._closed = closed

    def json(self):
        return self._payload

    def close(self):
        self._closed.append(self)


def _recording_client(monkeypatch, *responses):
    """Replace `httpx_client` in the users module with a recording double.

    `responses` are handed out in call order, one per `post()`. Each is a
    `(status_code, payload)` pair. Running out is an error rather than a
    silent reuse -- a test that makes more requests than it scripted is a
    test whose author lost track of the call sequence.

    Returns `SimpleNamespace(posts=[], closed=[])`:
      `posts`   -- one `(url, data)` per request, in call order.
      `closed`  -- the `_Response` objects on which `close()` was called.
                   THIS LIST IS THE SUBJECT of the email-close test.
    """
    record = SimpleNamespace(posts=[], closed=[])
    scripted = list(responses)

    def post(url, data=None, timeout=None):
        record.posts.append((url, data))
        if not scripted:
            raise AssertionError(
                f'the double was scripted for {len(responses)} responses '
                f'and received request {len(record.posts)} to {url}')
        status_code, payload = scripted.pop(0)
        return _Response(status_code, payload, record.closed)

    monkeypatch.setattr('app.shared.tasks.users.httpx_client',
                        SimpleNamespace(post=post))
    return record


def test_an_absent_application_returns_without_requests(db_session, monkeypatch):
    """`:18`'s first disjunct. `.get()` at `:17` returns None for an absent id.

    The double is scripted for ZERO responses, so any request at all raises
    AssertionError from `_recording_client` rather than passing silently.
    """
    s = _seed()
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch)

    check_user_application(s.application.id + 1000)

    assert client.posts == []


def test_an_application_without_a_user_returns_without_requests(
        db_session, monkeypatch):
    """`:18`'s second disjunct: the row exists but `application.user` is None.

    `UserRegistration.user` is a relationship on `user_id`
    (app/models.py:3641), so clearing the FK empties it.
    """
    s = _seed()
    s.application.user_id = None
    db.session.commit()
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch)

    check_user_application(s.application.id)

    assert client.posts == []


def test_no_ban_check_servers_configured_makes_no_requests(
        db_session, monkeypatch):
    """`:23`'s loop body never runs. `get_setting('ban_check_servers', '')`
    returns '' by default, and `''.split('\\n')` is `['']`, whose single
    member is blank -- so `:24` sends it straight to `:25`'s continue.

    THE LOOP RUNS ONCE HERE, NOT ZERO TIMES. That is worth stating because a
    reader expecting zero iterations would take the next test to be redundant.
    """
    s = _seed()
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch)

    check_user_application(s.application.id)

    assert client.posts == []


def test_the_seed_supplies_an_ip_address(db_session):
    """A guard on the fixture, not on the code.

    If `_seed` stops setting `ip_address`, `:39`'s `','.join` raises TypeError
    inside `:27`'s try, `:75` swallows it, and several tests in this file go
    green while proving nothing. This test makes that regression loud and
    names the reason.
    """
    s = _seed()

    assert s.user.ip_address == APPLICANT_IP
    assert s.user.email == APPLICANT_EMAIL
