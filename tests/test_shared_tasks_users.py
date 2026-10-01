"""`check_user_application` -- the cross-instance ban check.

`app/shared/tasks/users.py:14-88`, one function, 58 statements and 20 branches.
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
    A None member raises TypeError INSIDE the `try` at `:27`, which `:76`
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
        if isinstance(self._payload, _RaisingPayload):
            raise ValueError('simulated decode failure')
        return self._payload

    def close(self):
        self._closed.append(self)


class _RaisingPayload:
    """A payload whose `json()` raises, standing in for a decode failure
    inside `:27`'s try. Reaching `:76`'s handler by a NATURAL raise rather
    than an injected one keeps the test on the same path a real transport or
    decode error would take."""


_RAISING_PAYLOAD = _RaisingPayload()


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

    A REAL DOMAIN IS CONFIGURED, and the logger is the assertion, not just
    `client.posts`. Dropping the `or not application.user` disjunct alone
    does not change `client.posts` here: with no domain configured the loop
    body never runs either way, and even WITH a domain configured a mutant
    that removed the disjunct would proceed into the loop, dereference
    `application.user.ip_address` at `:36`, raise AttributeError on the
    `None`, and have that swallowed by `:76-78`'s per-domain `except` before
    any request is sent -- so `client.posts == []` would hold under the
    mutant too. The `errors` list is what actually distinguishes "returned
    before the loop" (no log call) from "entered the loop and failed inside
    it" (one log call), so it is what makes this test kill that mutant.
    """
    import app.shared.tasks.users as users_module

    s = _seed()
    s.application.user_id = None
    db.session.commit()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch)
    errors = []
    monkeypatch.setattr(users_module.current_app.logger, 'error',
                         lambda msg: errors.append(msg))

    check_user_application(s.application.id)

    assert client.posts == []
    assert errors == []


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
    inside `:27`'s try, `:76` swallows it, and several tests in this file go
    green while proving nothing. This test makes that regression loud and
    names the reason.
    """
    s = _seed()

    assert s.user.ip_address == APPLICANT_IP
    assert s.user.email == APPLICANT_EMAIL


def test_a_blank_line_in_the_setting_is_skipped(db_session, monkeypatch):
    """`:24`'s `if not domain.strip()` reaching `:25`'s continue, with a real
    domain after it proving the loop CONTINUES rather than aborting.

    One response is scripted for the IP leg and one for the email leg of the
    single real domain. A second domain's worth of requests would exhaust the
    script and raise.
    """
    s = _seed()
    set_setting('ban_check_servers', '\n   \nreal.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, [False]))

    check_user_application(s.application.id)

    assert [url for url, _data in client.posts] == [
        'https://real.example/api/is_ip_banned',
        'https://real.example/api/is_email_banned',
    ]


def test_the_real_ip_is_hidden_among_three_fakes(db_session, monkeypatch):
    """`:29-36`. Three fake IPs are generated and the real one INSERTED at
    `ip_index`, so the request carries four addresses and the server cannot
    tell which is under test.

    With `_lowest_randint` the index is 0, so the real address is first.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, [False]))

    check_user_application(s.application.id)

    _url, data = client.posts[0]
    submitted = data['ip_addresses'].split(',')
    assert len(submitted) == 4
    assert submitted[0] == APPLICANT_IP


def test_a_banned_ip_at_the_real_index_counts(db_session, monkeypatch):
    """`:43`, `:46` and `:47` all taken: status 200, results truthy and long
    enough, and the element at `ip_index` true.

    THE ASSERTION IS THE WARNING TEXT, not merely that a warning exists. The
    text names the count, so it distinguishes one ban from two -- which is
    what separates this test from `test_both_legs_banned_counts_twice`. The
    email leg is scripted clean here, so the 1 can only have come from the IP
    leg.

    Written against D319, a real production defect where `:81-82` passed the
    params dict as a second positional argument to `text()` instead of to
    `session.execute()`, so reaching `num_banned > 0` at `:80` raised
    `TypeError: text() takes 1 positional argument but 2 were given` and the
    `warning` column was never written. This test was marked to expect that
    failure until Task 10 fixed `:81-82`; it now passes for real, and the
    warning text it asserts is the proof the write happened.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [True]), (200, [False]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.get(UserRegistration, s.application.id).warning == '1 instances have banned this account.'


def test_a_non_200_ip_response_counts_nothing(db_session, monkeypatch):
    """`:43`'s false arm. A 500 skips the whole result block.

    Asserts both legs' URLs were actually requested, in the straight-line
    order `:37` then `:63` impose. Without this, a regression that makes
    `:27`'s try raise before `httpx_client.post` is ever reached -- a None
    `ip_address` breaking `:39`'s `','.join` is the known example, but not
    the only possible one -- would be swallowed by `:76`'s `except`, skip
    both legs, leave `warning` at None, and this test would pass having made
    ZERO requests while claiming to exercise `:43`.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (500, None), (200, [False]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.get(UserRegistration, s.application.id).warning is None
    assert [url for url, _data in client.posts] == [
        'https://real.example/api/is_ip_banned',
        'https://real.example/api/is_email_banned',
    ]


def test_an_empty_ip_result_list_counts_nothing(db_session, monkeypatch):
    """`:46`'s `if ip_results` guard, and its `len(ip_results) > ip_index`
    conjunct: an empty list is falsy AND too short. Both fail together here,
    which is why the next test exists to separate them.

    Asserts both legs' URLs were actually requested, in the straight-line
    order `:37` then `:63` impose -- see
    `test_a_non_200_ip_response_counts_nothing` for why this guards against a
    swallowed exception making the "nothing counted" assertion pass on zero
    requests.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, []), (200, [False]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.get(UserRegistration, s.application.id).warning is None
    assert [url for url, _data in client.posts] == [
        'https://real.example/api/is_ip_banned',
        'https://real.example/api/is_email_banned',
    ]


def test_a_false_result_at_the_real_index_counts_nothing(db_session, monkeypatch):
    """`:46`'s last conjunct alone: the list is truthy and long enough, but
    the element at `ip_index` is False.

    SEPARATES the third conjunct from the first two, which the empty-list test
    above fails simultaneously.

    Asserts both legs' URLs were actually requested, in the straight-line
    order `:37` then `:63` impose -- see
    `test_a_non_200_ip_response_counts_nothing` for why this guards against a
    swallowed exception making the "nothing counted" assertion pass on zero
    requests.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(
        monkeypatch, (200, [False, True, True, True]), (200, [False]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.get(UserRegistration, s.application.id).warning is None
    assert [url for url, _data in client.posts] == [
        'https://real.example/api/is_ip_banned',
        'https://real.example/api/is_email_banned',
    ]


def test_the_real_email_is_hidden_among_three_fakes(db_session, monkeypatch):
    """`:53-62`. Three fake addresses, the real one inserted at
    `email_index` -- 0 under `_lowest_randint`."""
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, [False]))

    check_user_application(s.application.id)

    _url, data = client.posts[1]
    submitted = data['emails'].split(',')
    assert len(submitted) == 4
    assert submitted[0] == APPLICANT_EMAIL


def test_a_banned_email_counts(db_session, monkeypatch):
    """`:69`, `:72` and `:73` taken on the email leg, with the IP leg clean --
    so the count of 1 in the warning text can only have come from email.

    `num_banned` reaches 1, so `:80`'s true arm runs `:81-82` -- see
    `test_a_banned_ip_at_the_real_index_counts` for D319, the defect this
    test was originally written against and marked to expect failure on.
    Task 10 fixed `:81-82`, and this test now passes, asserting the warning
    was actually persisted for the email leg.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [False]), (200, [True]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.get(UserRegistration, s.application.id).warning == '1 instances have banned this account.'


def test_a_non_200_email_response_counts_nothing(db_session, monkeypatch):
    """`:69`'s false arm.

    Asserts both legs' URLs were actually requested, in the straight-line
    order `:37` then `:63` impose -- see
    `test_a_non_200_ip_response_counts_nothing` for why this guards against a
    swallowed exception making the "nothing counted" assertion pass on zero
    requests.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (500, None))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.get(UserRegistration, s.application.id).warning is None
    assert [url for url, _data in client.posts] == [
        'https://real.example/api/is_ip_banned',
        'https://real.example/api/is_email_banned',
    ]


def test_an_empty_email_result_list_counts_nothing(db_session, monkeypatch):
    """`:72`'s `if email_results` guard and its length conjunct together.

    Asserts both legs' URLs were actually requested, in the straight-line
    order `:37` then `:63` impose -- see
    `test_a_non_200_ip_response_counts_nothing` for why this guards against a
    swallowed exception making the "nothing counted" assertion pass on zero
    requests.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, []))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.get(UserRegistration, s.application.id).warning is None
    assert [url for url, _data in client.posts] == [
        'https://real.example/api/is_ip_banned',
        'https://real.example/api/is_email_banned',
    ]


def test_a_false_result_at_the_real_email_index_counts_nothing(
        db_session, monkeypatch):
    """`:72`'s last conjunct alone, separated from the first two.

    Asserts both legs' URLs were actually requested, in the straight-line
    order `:37` then `:63` impose -- see
    `test_a_non_200_ip_response_counts_nothing` for why this guards against a
    swallowed exception making the "nothing counted" assertion pass on zero
    requests.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(
        monkeypatch, (200, [False]), (200, [False, True, True, True]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.get(UserRegistration, s.application.id).warning is None
    assert [url for url, _data in client.posts] == [
        'https://real.example/api/is_ip_banned',
        'https://real.example/api/is_email_banned',
    ]


def test_both_legs_banned_counts_twice(db_session, monkeypatch):
    """`num_banned` accumulating across the two legs of ONE domain.

    The warning text names 2, which no single-leg test can produce -- this is
    what proves `:47` and `:73` increment the same counter. THIS IS NOT AN
    ENDORSEMENT OF THE WORDING: `num_banned` counts LEGS (IP and email
    checked separately), not INSTANCES, so one domain banning on both legs
    reads "2 instances have banned this account." for a single instance.
    Registered as **D323** in
    `docs/superpowers/specs/2026-08-25-coverage-campaign-findings.md` -- not
    fixed, because fixing the string would be a fourth production change.
    This test pins CURRENT (miscounted) behaviour and must keep passing.

    `num_banned` reaches 2, so `:80`'s true arm runs `:81-82` -- see
    `test_a_banned_ip_at_the_real_index_counts` for D319, the defect this
    test was originally written against and marked to expect failure on.
    Task 10 fixed `:81-82`, and this test now passes, asserting both legs'
    counts landed in the one persisted warning.

    THE ASSERTED STRING IS THE MODULE'S OWN MISCOUNT (D323), NOT A CORRECT
    COUNT: `num_banned` is incremented once per LEG (`:47` IP, `:73` email),
    so this single-domain, both-legs-banned scenario produces "2 instances
    have banned this account." for ONE banning instance. This test pins
    present behaviour, not correct behaviour, and must keep passing.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [True]), (200, [True]))

    check_user_application(s.application.id)

    db.session.expire_all()
    assert db.session.get(UserRegistration, s.application.id).warning == '2 instances have banned this account.'


def test_a_failing_domain_does_not_stop_the_next_one(db_session, monkeypatch):
    """`:76-78`: an exception inside one domain's body is logged and the loop
    CONTINUES to the next domain.

    THE SECOND DOMAIN'S REQUESTS ARE THE OBSERVATION. A handler that logged
    and then broke out of the loop would leave `client.posts` holding only
    broken.example's single attempt, and this assertion separates the two
    behaviours. Nothing propagates out of `check_user_application` here --
    `:76` is `except Exception` and swallowing IS the behaviour under test --
    so there is no `pytest.raises` around the call.

    Note that broken.example makes ONE request and working.example makes TWO:
    the raise lands on the IP leg, so that domain's email leg never runs.
    """
    s = _seed()
    set_setting('ban_check_servers', 'broken.example\nworking.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(
        monkeypatch,
        (200, _RAISING_PAYLOAD),   # broken.example IP leg: json() raises
        (200, [False]),            # working.example IP leg
        (200, [False]),            # working.example email leg
    )

    check_user_application(s.application.id)

    assert [url for url, _data in client.posts] == [
        'https://broken.example/api/is_ip_banned',
        'https://working.example/api/is_ip_banned',
        'https://working.example/api/is_email_banned',
    ]


def test_the_warning_update_binds_its_parameters(db_session, monkeypatch):
    """D319. `:81-82` passed the params dict as a SECOND POSITIONAL ARGUMENT
    TO `text()` rather than as the second argument to `session.execute()`.
    `sqlalchemy.text` takes one positional parameter, so every run reaching
    `:80`'s true arm raised
    `TypeError: text() takes 1 positional argument but 2 were given`,
    `:84`'s `except Exception:` caught it, `:85` rolled back and `:86`
    re-raised, and the `warning` column was NEVER WRITTEN. Measured at
    SQLAlchemy 2.0.52.

    THE ASSERTION IS THE PERSISTED COLUMN, read back after the task. Asserting
    that `session.execute` was CALLED would be satisfied by the broken code
    the moment the line is reached, which is the unfailable shape fact 132
    exists to catch.

    THE ASSERTED STRING IS THE MODULE'S OWN MISCOUNT (D323), NOT A CORRECT
    COUNT: `num_banned` counts LEGS, not instances, so this single-domain,
    both-legs-banned scenario reads "2 instances" for ONE. This test pins
    present behaviour, not correct behaviour, and must keep passing.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [True]), (200, [True]))

    check_user_application(s.application.id)

    db.session.expire_all()
    persisted = db.session.get(UserRegistration, s.application.id)
    assert persisted.warning == '2 instances have banned this account.'


def test_both_responses_are_closed(db_session, monkeypatch):
    """D320. `ip_response.close()` at `:48` had no counterpart on the email
    leg, so every email response was left unclosed.

    THE DOUBLE'S `closed` LIST IS THE SUBJECT, and it exists because respx
    cannot observe this: the `Response` never leaves the function, and a
    respx-mocked response may already report `is_closed == True` before
    `close()` is called -- an `is_closed` assertion would pass identically
    before and after the fix. Before the fix this list holds ONE entry; after,
    TWO.
    """
    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(monkeypatch, (200, [False]), (200, [False]))

    check_user_application(s.application.id)

    assert len(client.closed) == 2


def test_a_response_whose_json_raises_is_still_closed(db_session, monkeypatch):
    """D322, fixed: a leg whose `.json()` raised went straight to the
    per-domain handler and skipped that leg's `close()`. Each response is now
    closed in a `finally`, so broken.example's IP response is closed too."""
    s = _seed()
    set_setting('ban_check_servers', 'broken.example\nworking.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    client = _recording_client(
        monkeypatch,
        (200, _RAISING_PAYLOAD),   # broken.example IP leg: json() raises
        (200, [False]),            # working.example IP leg
        (200, [False]),            # working.example email leg
    )

    check_user_application(s.application.id)

    assert len(client.closed) == 3


def test_a_database_failure_rolls_back_and_re_raises(db_session, monkeypatch):
    """`:84-86`'s except arm and `:87-88`'s finally, reached WITHOUT a faked
    exception anywhere in the task's own logic.

    `get_task_session` is replaced by one whose `execute` raises
    unconditionally. VERIFIED BY TRACEBACK (not assumed): under this
    project's pinned `sqlalchemy~=2.0.0`, `session.query(UserRegistration)
    .get(application_id)` at `:17` funnels through `Session.execute()` on
    the identity-map miss that a freshly constructed `Session` always has,
    so THE RAISE FIRES AT `:17`, before `:18`'s guard, before the domain
    loop, and before either HTTP leg would be reached. This is why there is
    no `set_setting('ban_check_servers', ...)`, no `_no_sleep`/
    `_lowest_randint`, and no `_recording_client` here: none of that
    machinery is ever consulted, and scripting it would misstate the
    mechanism to the next reader. What the test actually proves is
    unaffected by exactly which statement inside `:16`'s `try` raises --
    the except/finally structure and the recorded `['rollback', 'close']`
    order are the same regardless.

    THE PATCH TARGET IS THE USERS MODULE. `app/shared/tasks/users.py:10`
    imports `get_task_session` into the users namespace and `:15` resolves it
    there; patching `app.utils.get_task_session` would apply cleanly and
    observe nothing.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.users as users_module

    s = _seed()

    calls = []

    def _make():
        session = _Session(bind=_db.engine)
        real_rollback, real_close = session.rollback, session.close

        def rollback():
            calls.append('rollback')
            return real_rollback()

        def close():
            calls.append('close')
            return real_close()

        def execute(*_args, **_kwargs):
            raise RuntimeError('database refused the update')

        session.rollback = rollback
        session.close = close
        session.execute = execute
        return session

    monkeypatch.setattr(users_module, 'get_task_session', _make)

    with pytest.raises(RuntimeError):
        check_user_application(s.application.id)

    assert calls == ['rollback', 'close']


def test_the_session_is_closed_on_the_happy_path(db_session, monkeypatch):
    """`:87-88`'s finally on the SUCCESS path -- `close` with no `rollback`.

    The control for the test above: without it, `finally` running is only ever
    observed alongside an exception, and a handler that closed only in the
    `except` arm would pass every other test in this file.
    """
    from app import db as _db
    from sqlalchemy.orm import Session as _Session
    import app.shared.tasks.users as users_module

    s = _seed()
    set_setting('ban_check_servers', 'real.example')
    _no_sleep(monkeypatch)
    _lowest_randint(monkeypatch)
    _recording_client(monkeypatch, (200, [False]), (200, [False]))

    calls = []

    def _make():
        session = _Session(bind=_db.engine)
        real_rollback, real_close = session.rollback, session.close

        def rollback():
            calls.append('rollback')
            return real_rollback()

        def close():
            calls.append('close')
            return real_close()

        session.rollback = rollback
        session.close = close
        return session

    monkeypatch.setattr(users_module, 'get_task_session', _make)

    check_user_application(s.application.id)

    assert calls == ['close']
