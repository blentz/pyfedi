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

THREE TESTS HERE PIN A DEFECT. :57 admits four ban states and only one is
refused, because the refusal at :73/:75 is nested inside :59's new-IP
detection rather than hanging off :57. The three that fall through assert
that a banned or IP-banned user LOGS IN. They are inverted by the task that
dedents :66-75. Each says PINS A DEFECT in its docstring.
"""
import contextlib
from types import SimpleNamespace

import pytest

import app.shared.auth as auth_module
from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Instance, IpBan, User
from app.shared.auth import log_user_in
from tests.factories import make_instance, make_user


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
    """ROW ONE of the four ban states, and the ONLY one refused today.

    banned=True, ip_banned=False: :57 enters, :59 is true, :61-63 writes an
    IpBan for the current address, and :75 raises.

    THIS TEST IS NOT INVERTED by the dedent -- it is the control that proves
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


def test_log_user_in_api_admits_a_banned_user_whose_ip_is_already_banned(app, db_session, monkeypatch):
    """PINS A DEFECT -- and this is the serious one.

    ROW TWO: banned=True, ip_banned=True. :57 enters, but :59 is
    `True and not True` = False, so the refusal at :75 -- nested inside :59 --
    never runs, and control falls through to :83 and :111-113. A JWT is
    returned to a banned user.

    This state is not hypothetical: it is what ROW ONE produces. The banned
    user's first attempt bans their IP and is refused; every attempt after
    that, from the same address, is this test.

    THIS ASSERTION IS INVERTED by the task that dedents :66-75.
    """
    s = _seed_login_user()
    s.user.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=True):
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


def test_log_user_in_api_admits_an_ip_banned_user_who_is_not_banned(app, db_session, monkeypatch):
    """PINS A DEFECT. ROW THREE: banned=False, ip_banned=True.

    :57 enters on the second disjunct, :59's first conjunct is false, and the
    refusal never runs.

    THIS ASSERTION IS INVERTED by the task that dedents :66-75.

    Self-verifying: real user_ip_banned() consults banned_ip_addresses(),
    which queries the IpBan table and finds no rows here, so it returns False
    on its own -- the same shape as user_cookie_banned() returning False from
    a cookie-less request. If _ban_state's patch were ever retargeted at
    app.utils.user_ip_banned (which would not intercept, per app/shared/
    auth.py:14's `from ... import`), :57 would never be entered and the
    function would still fall through to a successful login -- indistinguish-
    able from today's correct-but-defective behaviour by this test's final
    assertion alone. The explicit check below fails loudly instead.
    """
    s = _seed_login_user()
    assert s.user.banned is False

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=True):
            assert auth_module.user_ip_banned() is True, \
                '_ban_state did not intercept; the patch target is wrong'
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


def test_log_user_in_api_admits_a_cookie_banned_user(app, db_session, monkeypatch):
    """PINS A DEFECT. ROW FOUR: banned=False, cookie_banned=True.

    :57 enters on the third disjunct and :59 is false.

    THIS ASSERTION IS INVERTED by the task that dedents :66-75.

    Self-verifying: real user_cookie_banned() reads request.cookies.get
    ('sesion'), which is absent under app.test_request_context('/'), so it
    returns False on its own. If _ban_state's patch were ever retargeted at
    app.utils.user_cookie_banned (which would not intercept, per app/shared/
    auth.py:14's `from ... import`), all three real predicates would be
    falsy, :57 would never be entered, and the function would still fall
    through to a successful login -- indistinguishable from today's correct-
    but-defective behaviour by this test's final assertion alone. The
    explicit check below fails loudly instead of passing for the wrong
    reason.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, cookie_banned=True):
            assert auth_module.user_cookie_banned() is True, \
                '_ban_state did not intercept; the patch target is wrong'
            result = log_user_in({'username': 'loginuser',
                                  'password': s.password}, SRC_API)

    assert result['jwt']


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
    value, so this distinguishes the stamp from a column default."""
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
