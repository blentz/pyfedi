"""`log_user_in` (app/shared/auth.py:18-113).

The module was at 43.548% before this file: 40 statements and 30 arcs.

TWO FUNCTIONS SHARE THIS NAME. app/auth/util.py:474 is
`log_user_in(user, form, ip, country, ldap_sync=True)` and serves the real
web login flow. THIS one, app/shared/auth.py:18, is
`log_user_in(input, src)` and is reached from exactly one caller --
app/api/alpha/routes.py:1277, with SRC_API. Its own comment at :17 says so.
So every SRC_WEB test in this file drives a source value production never
passes to this function, and says so in its docstring. That is legitimate
and precedented (tests/test_redirect_targets.py:234-265 already does it,
and tests/test_shared_post_interactions.py:577 is the campaign's canonical
case), but it must never be left implicit.

THREE TESTS HERE PINNED A DEFECT. :57 used to admit four ban states while
only one was refused, because the refusal at :73/:75 was nested inside :59's
new-IP detection rather than hanging off :57. :66-75 were dedented one level
in this round, out of :59's block and into :57's, so the refusal now applies
to every state the outer guard admits; :59-64 kept its own body, since
banning the new IP is correct and stays there. The three tests below are the
inverted pins: they now assert that a banned, IP-banned, or cookie-banned
user is REFUSED, and each says REFUSES A DEFECT in its docstring.
"""
import contextlib
from types import SimpleNamespace

import pytest
from flask import session as flask_session, url_for

import app.shared.auth as auth_module
from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Instance, IpBan, User
from app.shared.auth import log_user_in
from tests.factories import (make_community, make_community_member, make_instance,
                              make_user, make_user_registration)


class Field:
    """One WTForms-ish field. :22-23 and :106 read `.data` off these."""

    def __init__(self, data):
        self.data = data


class Form:
    """The duck-typed form the SRC_WEB arm reads.

    Copied from tests/test_redirect_targets.py:240-248, which already drives
    this function's SRC_WEB arm -- the same three fields, read at :22, :23
    and :106.
    """

    def __init__(self, user_name, password, low_bandwidth_mode=False):
        self.user_name = Field(user_name)
        self.password = Field(password)
        self.low_bandwidth_mode = Field(low_bandwidth_mode)


def _seed_login_user(name='loginuser', password='correct horse battery'):
    """A local user with a real password hash, not id 1.

    NOT id 1 on purpose: :57 begins `if user.id != 1`, exempting the first
    account from every ban check, and tests/conftest.py:131-132 resets every
    sequence between tests so the first user minted is id 1 deterministically.
    Without the burn, every ban test here would pass :57 vacuously.

    make_user(None, ...) sets instance_id=1 unconditionally, so the local
    Instance row has to exist first -- the same arrangement as
    tests/test_redirect_targets.py:80-89.
    """
    if not Instance.query.get(1):
        make_instance('test.piefed.local', software='piefed')
    burn = make_user(None, 'burn-the-id-1-seat', local=True)
    assert burn.id == 1, 'the id-1 exemption at :57 moved; re-derive this'

    user = make_user(None, name, local=True)
    user.set_password(password)
    db.session.commit()
    return SimpleNamespace(user=user, password=password)


@contextlib.contextmanager
def _ban_state(monkeypatch, ip_banned=False, cookie_banned=False, ip='203.0.113.7'):
    """Drive :57 and :59's two predicates directly.

    app/shared/auth.py:14 binds ip_address, user_ip_banned and
    user_cookie_banned into THIS module's globals, so rebinding them on
    app.shared.auth is what intercepts -- patching app.utils would not, and
    app/utils.py:2308's `ip_address = get_ip_address` is an alias, so the same
    rule holds for it.

    Driving the predicates rather than seeding IpBan rows keeps each test's
    ban state a single explicit statement, and keeps :64's
    cache.delete_memoized(banned_ip_addresses) from depending on real rows.
    """
    monkeypatch.setattr('app.shared.auth.ip_address', lambda *a, **k: ip)
    monkeypatch.setattr('app.shared.auth.user_ip_banned', lambda *a, **k: ip_banned)
    monkeypatch.setattr('app.shared.auth.user_cookie_banned', lambda *a, **k: cookie_banned)
    yield


def test_log_user_in_api_returns_a_jwt(app, db_session, monkeypatch):
    """The success path: :26-29's username lookup, then :111-113's token."""
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert set(result) == {'jwt'}
    assert result['jwt']


def test_log_user_in_api_matches_the_username_case_insensitively(app, db_session, monkeypatch):
    """:29's `func.lower(User.user_name) == func.lower(username)`.

    The stored name is lower-case, so an upper-case input distinguishes a
    case-insensitive comparison from a plain equality.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            result = log_user_in({'username': 'LOGINUSER',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


def test_log_user_in_api_falls_back_to_the_email_address(app, db_session, monkeypatch):
    """:31-33's second lookup, reached only when :29 found nothing.

    make_user sets email to f'{name}@example.com'. The web arm has no such
    fallback -- :24 matches user_name exactly -- so the two arms accept
    different credentials. Registered, not fixed.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            result = log_user_in({'username': 'loginuser@example.com',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


def test_log_user_in_api_refuses_an_unknown_account(app, db_session, monkeypatch):
    """:35-37 -- both lookups missed."""
    _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            with pytest.raises(Exception, match='incorrect_login'):
                log_user_in({'username': 'nobody', 'password': 'whatever'}, SRC_API)


def test_log_user_in_api_refuses_a_wrong_password(app, db_session, monkeypatch):
    """:46's true arm into :54-55's raise."""
    _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            with pytest.raises(Exception, match='incorrect_login'):
                log_user_in({'username': 'loginuser', 'password': 'wrong'}, SRC_API)


def test_log_user_in_refuses_an_unknown_source(app, db_session):
    """:38-39's else. SRC_PUB is neither SRC_WEB nor SRC_API, so the function
    returns None before touching the database.

    SRC_PUB is used purely as a third source value to reach :39. It is not how
    this function is called in production -- app/api/alpha/routes.py:1277 is
    the only caller and passes SRC_API.
    """
    from app.constants import SRC_PUB
    _seed_login_user()

    with app.test_request_context('/'):
        assert log_user_in({'username': 'loginuser', 'password': 'x'}, SRC_PUB) is None


def test_log_user_in_api_bans_the_ip_of_a_banned_user_and_refuses(app, db_session, monkeypatch):
    """ROW ONE of the four ban states, and the only one that WAS refused
    before the dedent. As delivered, all four are refused: the three tests
    below this one cover rows two, three and four, and each of them now
    asserts a refusal.

    banned=True, ip_banned=False: :57 enters, :59 is true, :61-63 writes an
    IpBan for the current address, and :75 raises.

    THIS TEST WAS NOT INVERTED by the dedent -- it is the control that proves
    the dedent did not simply disable the branch. Assert both halves: the
    refusal AND the IpBan row, because a dedent that broke :59-64 would still
    raise.
    """
    s = _seed_login_user()
    s.user.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=False):
            with pytest.raises(Exception, match='incorrect_login'):
                log_user_in({'username': 'loginuser',
                             'password': s.password}, SRC_API)

    assert db.session.query(IpBan).filter_by(ip_address='203.0.113.7').count() == 1


def test_log_user_in_api_refuses_a_banned_user_whose_ip_is_already_banned(app, db_session, monkeypatch):
    """REFUSES A DEFECT -- and this was the serious one.

    ROW TWO: banned=True, ip_banned=True. :57 enters; :59 is
    `True and not True` = False, so :59's body (banning a new IP) is
    correctly skipped, but the refusal now hangs directly off :57 instead of
    being nested inside :59, so it still fires.

    This state is not hypothetical: it is what ROW ONE produces. The banned
    user's first attempt bans their IP and is refused; every attempt after
    that, from the same address, is this test -- and it must now be refused
    too.

    THIS ASSERTION WAS INVERTED by the task that dedented :66-75. The IpBan
    count assertion PINS, rather than observes, that :59's body was skipped
    (still 1, not 2) while the dedented refusal fired: with user_ip_banned
    patched True, :59's `and not user_ip_banned()` is False under the
    unmutated code, so the count cannot vary and the assertion can only fail
    against a mutant that re-reaches :61-63.
    """
    s = _seed_login_user()
    s.user.banned = True
    db.session.add(IpBan(ip_address='203.0.113.7', notes='pre-existing ban from row one'))
    db.session.commit()
    assert db.session.query(IpBan).filter_by(ip_address='203.0.113.7').count() == 1

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=True):
            with pytest.raises(Exception, match='incorrect_login'):
                log_user_in({'username': 'loginuser',
                             'password': s.password}, SRC_API)

    assert db.session.query(IpBan).filter_by(ip_address='203.0.113.7').count() == 1


def test_log_user_in_api_refuses_an_ip_banned_user_who_is_not_banned(app, db_session, monkeypatch):
    """REFUSES A DEFECT. ROW THREE: banned=False, ip_banned=True.

    :57 enters on the second disjunct, :59's first conjunct is false, so
    :59's body never runs, but the refusal now hangs directly off :57 and
    fires anyway.

    THIS ASSERTION WAS INVERTED by the task that dedented :66-75.

    Self-verifying: real user_ip_banned() consults banned_ip_addresses(),
    which queries the IpBan table and finds no rows here, so it returns False
    on its own -- the same shape as user_cookie_banned() returning False from
    a cookie-less request. If _ban_state's patch were ever retargeted at
    app.utils.user_ip_banned (which would not intercept, per app/shared/
    auth.py:14's `from ... import`), :57 would never be entered and the
    function would fall through to a successful login instead of raising --
    indistinguishable from a false pass by the pytest.raises alone. The
    explicit check below fails loudly instead, and the IpBan-count assertion
    after the call proves the refusal came from the dedented block rather
    than from :59-64 (which would have inserted a row).
    """
    s = _seed_login_user()
    assert s.user.banned is False

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=True):
            assert auth_module.user_ip_banned() is True, \
                '_ban_state did not intercept; the patch target is wrong'
            with pytest.raises(Exception, match='incorrect_login'):
                log_user_in({'username': 'loginuser',
                             'password': s.password}, SRC_API)

    assert db.session.query(IpBan).count() == 0


def test_log_user_in_api_refuses_a_cookie_banned_user(app, db_session, monkeypatch):
    """REFUSES A DEFECT. ROW FOUR: banned=False, cookie_banned=True.

    :57 enters on the third disjunct and :59 is false, so :59's body never
    runs, but the refusal now hangs directly off :57 and fires anyway.

    THIS ASSERTION WAS INVERTED by the task that dedented :66-75.

    Self-verifying: real user_cookie_banned() reads request.cookies.get
    ('sesion'), which is absent under app.test_request_context('/'), so it
    returns False on its own. If _ban_state's patch were ever retargeted at
    app.utils.user_cookie_banned (which would not intercept, per app/shared/
    auth.py:14's `from ... import`), all three real predicates would be
    falsy, :57 would never be entered, and the function would fall through
    to a successful login instead of raising -- indistinguishable from a
    false pass by the pytest.raises alone. The explicit check below fails
    loudly instead of passing for the wrong reason, and the IpBan-count
    assertion after the call proves the refusal came from the dedented block
    rather than from :59-64 (which would have inserted a row).
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, cookie_banned=True):
            assert auth_module.user_cookie_banned() is True, \
                '_ban_state did not intercept; the patch target is wrong'
            with pytest.raises(Exception, match='incorrect_login'):
                log_user_in({'username': 'loginuser',
                             'password': s.password}, SRC_API)

    assert db.session.query(IpBan).count() == 0


def test_log_user_in_exempts_the_id_1_account_from_every_ban_check(app, db_session, monkeypatch):
    """PINS A DEFECT, registered not fixed. :57 begins `user.id != 1`.

    The id-1 account logs in while banned AND ip-banned. This is the
    production face of the id-1 trap this campaign keeps meeting in fixtures
    (app/models.py:1259-1261 makes the same account an admin outright).

    NOT inverted by the dedent: the dedent moves the refusal, it does not
    touch :57's first conjunct. This test must keep passing unchanged, which
    is what makes it evidence about :57 rather than about :59.
    """
    if not Instance.query.get(1):
        make_instance('test.piefed.local', software='piefed')
    first = make_user(None, 'firstaccount', local=True)
    assert first.id == 1
    first.set_password('correct horse battery')
    first.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=True):
            result = log_user_in({'username': 'firstaccount',
                                  'password': 'correct horse battery'}, SRC_API)

    assert result['jwt']


def test_log_user_in_stamps_last_seen_and_ip(app, db_session, monkeypatch):
    """:83-86. `ip` comes from :19's ip_address() call, patched to a known
    value, so the `ip_address` assertion below distinguishes the stamp from
    a column default -- that half is load-bearing.

    The `last_seen` assertion is NOT load-bearing: `User.last_seen` is
    declared `db.Column(db.DateTime, default=utcnow, index=True)`
    (app/models.py:997), so the column fills itself on insert whether or not
    :83 runs, and `stored.last_seen is not None` holds even if :83 is
    deleted. Kept anyway -- it is not wrong, only insufficient on its own --
    because a stronger replacement (seeding a known old `last_seen` and
    asserting it advanced) is out of this test's scope.
    """
    s = _seed_login_user()
    assert s.user.ip_address is None

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip='198.51.100.9'):
            log_user_in({'username': 'loginuser', 'password': s.password}, SRC_API)

    db.session.expire_all()
    stored = db.session.query(User).get(s.user.id)
    assert stored.ip_address == '198.51.100.9'
    assert stored.last_seen is not None


def test_log_user_in_survives_a_failing_ldap_sync(app, db_session, monkeypatch):
    """:88-91's `except Exception: ...`.

    sync_user_to_ldap is bound into this module's globals by :12, so rebinding
    it here is what intercepts. The login must still succeed, which is the
    whole point of the handler.
    """
    s = _seed_login_user()

    def _boom(*args, **kwargs):
        raise RuntimeError('ldap is down')

    monkeypatch.setattr('app.shared.auth.sync_user_to_ldap', _boom)

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


# ---------------------------------------------------------------------------
# SRC_WEB arm. Every test below drives a source value production never
# passes to this function: app/auth/util.py:474 is a different log_user_in
# that serves the real web login flow, and app/shared/auth.py:18's only
# caller, app/api/alpha/routes.py:1277, passes SRC_API. The SRC_WEB arm is
# live code, so it is covered -- but no login page reaches it, and a reader
# should not mistake these for tests of one.
# ---------------------------------------------------------------------------


def test_log_user_in_web_looks_up_by_exact_user_name(app, db_session, monkeypatch):
    """:21-24. The web arm's own lookup, `filter_by(user_name=username,
    ap_id=None)` -- distinct from :26-33's API arm, which lower-cases the
    comparison in SQL and falls back to an email match. Asserts the returned
    response is a redirect, which is the thing that distinguishes SRC_WEB's
    branch from SRC_API's at this point: the `.status_code` dereference below
    already excludes the API arm's JWT dict, which has no such attribute and
    would raise AttributeError rather than compare unequal.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            response = log_user_in(Form('loginuser', s.password), SRC_WEB)

    assert response.status_code == 302


def test_log_user_in_web_refuses_an_unknown_user_name(app, db_session, monkeypatch):
    """:41-44's first operand, `user is None`. Two tests cover this `or`
    because a single test would leave one operand deletable -- see the
    sibling test below for `user.deleted`, the other operand.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            response = log_user_in(Form('nobody', 'whatever'), SRC_WEB)
            flashed = [str(message) for _category, message in
                       flask_session.get('_flashes', [])]
            expected_location = url_for('auth.login')

    assert response.status_code == 302
    assert 'No account exists with that user name.' in flashed
    assert response.headers['Location'] == expected_location


def test_log_user_in_web_refuses_a_deleted_user(app, db_session, monkeypatch):
    """:41-44's second operand, `user.deleted`. The user name is known and
    the password is correct -- only `deleted=True` should trigger the
    refusal, isolating this operand from the `user is None` operand covered
    above. :24's lookup does not filter on `deleted` (unlike :29's API-arm
    lookup), so a deleted user is still found here and must be rejected by
    this check instead.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    s = _seed_login_user()
    s.user.deleted = True
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            response = log_user_in(Form('loginuser', s.password), SRC_WEB)
            flashed = [str(message) for _category, message in
                       flask_session.get('_flashes', [])]
            expected_location = url_for('auth.login')

    assert response.status_code == 302
    assert 'No account exists with that user name.' in flashed
    assert response.headers['Location'] == expected_location


def test_log_user_in_web_refuses_a_wrong_password_with_a_reset_link(app, db_session, monkeypatch):
    """:47-51. A wrong password on the web arm when `user.password_hash` is
    None.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.

    The two wrong-password messages differ: :49-51 offers a reset link when
    there is no hash to check against, :52-53 says only 'Invalid password'.
    The exact text is asserted rather than merely that a flash happened,
    because asserting the latter would let the two branches swap undetected --
    false-witness mechanism (d), an input taking the same path under both
    arms.

    THE FLASH LIST IS ASSERTED EXACTLY, not with `any(...)`. Deleting :51's
    `return redirect(...)` lets flow fall into :52-53 too, which appends a
    second 'Invalid password' flash and returns the same login redirect --
    `any('reset_password_request' in m for m in flashed)` still holds against
    that two-item list, so the mutant would survive an `any` check. It does
    NOT reach login_user: :53 still returns before :57's ban check, so this
    is a milder gap than :53's own deletion (see the sibling test below) and
    is closed by pinning the exact list rather than by a login-state check.

    password_hash is set to None AFTER set_password, because _seed_login_user
    needs a real hash to exist first for the other tests in this file and
    check_password (app/models.py:1125) is total over a None hash rather than
    raising on it.
    """
    s = _seed_login_user()
    s.user.password_hash = None
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            response = log_user_in(Form('loginuser', 'wrong'), SRC_WEB)
            flashed = [str(message) for _category, message in
                       flask_session.get('_flashes', [])]

    assert response.status_code == 302
    assert flashed == [
        'Invalid password. Please <a href="/auth/reset_password_request">reset your password</a>.'
    ]


def test_log_user_in_web_refuses_a_wrong_password_without_a_reset_link(app, db_session, monkeypatch):
    """:52-53. A wrong password on the web arm when `user.password_hash` IS
    set -- the counterpart to the test above, which sets it to None. The two
    wrong-password messages differ: :49-51 offers a reset link, :52-53 says
    only 'Invalid password'. The exact text is asserted rather than merely
    that a flash happened, because asserting the latter would let the two
    branches swap undetected -- false-witness mechanism (d), an input taking
    the same path under both arms.

    THE LOGGED-IN CHECK BELOW IS THE LOAD-BEARING ASSERTION. Deleting :53's
    `return redirect(...)` makes a wrong-password web login fall through --
    out of :46's block, past the ban check, into :80's
    `login_user(user, remember=True)` -- and return the ordinary success
    redirect. Without this check nothing fails: the mutant still returns a
    302 and :52's flash still ran, so `status_code == 302` and
    `flashed == ['Invalid password']` both hold on the success path too.
    `test_log_user_in_web_logs_in_and_sets_ui_language` below establishes a
    login DID happen with `'_user_id' in flask_session`; this asserts the
    negation of that same probe, so the pair is symmetric.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            response = log_user_in(Form('loginuser', 'wrong'), SRC_WEB)
            flashed = [str(message) for _category, message in
                       flask_session.get('_flashes', [])]
            logged_in = '_user_id' in flask_session

    assert response.status_code == 302
    assert flashed == ['Invalid password']
    assert logged_in is False


def test_log_user_in_web_refuses_a_banned_user_row_one(app, db_session, monkeypatch):
    """:66-73. The web arm of the ban refusal, ROW ONE (banned=True,
    ip_banned=False) -- the one row of the four :57 admits that was already
    refused BEFORE the dedent. Assert the flash AND the `sesion` cookie set
    at :72, since that cookie is the mechanism `user_cookie_banned` later
    reads.

    A CONTROL, NOT A PIN: row one is the state the dedent of :66-75 did not
    change -- unlike this file's three REFUSES-A-DEFECT tests near the top
    (rows two through four), which were inverted when it landed. This test
    kept passing unchanged across the dedent, and that is what makes it
    evidence the dedent moved the refusal rather than disabling the branch.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    s = _seed_login_user()
    s.user.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=False):
            response = log_user_in(Form('loginuser', s.password), SRC_WEB)
            flashed = [str(message) for _category, message in
                       flask_session.get('_flashes', [])]

    assert response.status_code == 302
    assert 'You have been banned.' in flashed
    set_cookie_headers = response.headers.getlist('Set-Cookie')
    assert any(header.startswith('sesion=') for header in set_cookie_headers), set_cookie_headers


def test_log_user_in_web_redirects_a_user_awaiting_approval(app, db_session, monkeypatch):
    """:77-79. `user.waiting_for_approval()` (app/models.py:1254-1256) finds a
    pending UserRegistration row and redirects to auth.please_wait instead of
    reaching :80's login_user -- so the user is never actually logged in.
    make_user_registration(user, status=0) (tests/factories.py:1126) creates
    exactly the row waiting_for_approval() looks for.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    s = _seed_login_user()
    make_user_registration(s.user, status=0)

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            response = log_user_in(Form('loginuser', s.password), SRC_WEB)
            logged_in = '_user_id' in flask_session
            expected_location = url_for('auth.please_wait')

    assert response.status_code == 302
    assert response.headers['Location'] == expected_location
    assert logged_in is False


def test_log_user_in_web_logs_in_and_sets_ui_language(app, db_session, monkeypatch):
    """:80-81. The non-waiting path: `login_user(user, remember=True)` then
    `session['ui_language'] = user.interface_language`. interface_language is
    set to a distinctive, non-default value first, so the assertion is not
    indistinguishable from a session key defaulting to None on its own --
    false-witness mechanism (c), emptiness with no same-mechanism positive
    control.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    s = _seed_login_user()
    s.user.interface_language = 'fr'
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            log_user_in(Form('loginuser', s.password), SRC_WEB)
            logged_in = '_user_id' in flask_session
            ui_language = flask_session.get('ui_language')

    assert logged_in is True
    assert ui_language == 'fr'


def test_log_user_in_web_falls_back_to_main_index_for_a_member(app, db_session, monkeypatch):
    """:101-104's `else` arm. :99-100 finds no safe `next` (none was supplied,
    and `is_safe_redirect_target(None)` is False), so :101 checks
    `len(user.communities()) == 0`; a user who belongs to at least one
    non-banned community takes the `else` branch to `main.index` instead of
    :102's `auth.filter_selection`. This is distinct from the next-parameter
    branches tests/test_redirect_targets.py:250-265 already covers -- those
    tests use a community-less user, so :101 is always true there and :104
    is never reached; that gap is real, not a duplicate, which is why this
    test exists rather than being skipped as already covered.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    s = _seed_login_user()
    community = make_community('loginmembertest')
    make_community_member(s.user, community)

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            response = log_user_in(Form('loginuser', s.password), SRC_WEB)
            expected_location = url_for('main.index')

    assert response.status_code == 302
    assert response.headers['Location'] == expected_location


def test_log_user_in_web_sets_low_bandwidth_cookie_when_requested(app, db_session, monkeypatch):
    """:106-107. `input.low_bandwidth_mode.data` true sets the `low_bandwidth`
    cookie to '1'. tests/test_redirect_targets.py:234-265's
    TestSharedAuthNextPageIsChecked already drives :99-105's next-parameter
    branches through this same function, but its Form fixes
    low_bandwidth_mode to False (test_redirect_targets.py:248), so it never
    reaches this arm's true branch and never asserts on the cookie value --
    only that the redirect stays on-site. This test targets the cookie
    itself instead.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            response = log_user_in(Form('loginuser', s.password, low_bandwidth_mode=True), SRC_WEB)

    assert response.status_code == 302
    cookies = response.headers.getlist('Set-Cookie')
    assert any(cookie.startswith('low_bandwidth=1') for cookie in cookies), cookies


def test_log_user_in_web_clears_low_bandwidth_cookie_when_not_requested(app, db_session, monkeypatch):
    """:108-109. The `else` arm sets the `low_bandwidth` cookie to '0'.
    tests/test_redirect_targets.py's Form always passes low_bandwidth_mode as
    False, so its tests already execute this line -- but assert nothing about
    the cookie, only that the redirect stays on-site. This test makes the
    cookie value itself the assertion, as the sibling test above does for the
    true arm, so the two arms cannot swap undetected.

    DRIVES A SOURCE VALUE PRODUCTION NEVER PASSES to this function.
    app/auth/util.py:474 is a different log_user_in and serves the real web
    login flow; this one's only caller, app/api/alpha/routes.py:1277, passes
    SRC_API. The SRC_WEB arm is live code, so it is covered -- but no login
    page reaches it, and a reader should not mistake this for a test of one.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            response = log_user_in(Form('loginuser', s.password, low_bandwidth_mode=False), SRC_WEB)

    assert response.status_code == 302
    cookies = response.headers.getlist('Set-Cookie')
    assert any(cookie.startswith('low_bandwidth=0') for cookie in cookies), cookies
