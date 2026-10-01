"""`api_log_user_in` (app/shared/auth.py), the API's login.

D582, fixed (owner ruling): this was `log_user_in(input, src)`, sharing its
name with the real web login in app/auth/util.py, and its SRC_WEB arm had no
production caller. That arm and the tests that only drove it are gone; the
function is now API-only and named for it.

THREE TESTS HERE PINNED A DEFECT. The ban refusal used to be nested inside
the new-IP detection rather than hanging off the outer ban guard, so only
one of four ban states was refused. The three tests below are the inverted
pins: they assert that a banned, IP-banned, or cookie-banned user is
REFUSED, and each says REFUSES A DEFECT in its docstring.
"""
import contextlib
from types import SimpleNamespace

import pytest

import app.shared.auth as auth_module
from app import db
from app.models import Instance, IpBan, User
from app.shared.auth import api_log_user_in
from tests.factories import make_instance, make_user


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
    if not db.session.get(Instance, 1):
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
            result = api_log_user_in({'username': 'loginuser',
                                      'password': s.password})

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
            result = api_log_user_in({'username': 'LOGINUSER',
                                      'password': s.password})

    assert result['jwt']


def test_log_user_in_api_falls_back_to_the_email_address(app, db_session, monkeypatch):
    """`find_user`'s second lookup, reached only when the name found nothing.

    make_user sets email to f'{name}@example.com'. D584, fixed (owner
    ruling): the API and the web now share `find_user`, so both accept the
    name or the address, the address case-insensitively.
    """
    s = _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            result = api_log_user_in({'username': 'LoginUser@Example.com',
                                      'password': s.password})

    assert result['jwt']


def test_log_user_in_api_refuses_an_unknown_account(app, db_session, monkeypatch):
    """:35-37 -- both lookups missed."""
    _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            with pytest.raises(Exception, match='incorrect_login'):
                api_log_user_in({'username': 'nobody', 'password': 'whatever'})


def test_log_user_in_api_refuses_a_wrong_password(app, db_session, monkeypatch):
    """:46's true arm into :54-55's raise."""
    _seed_login_user()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch):
            with pytest.raises(Exception, match='incorrect_login'):
                api_log_user_in({'username': 'loginuser', 'password': 'wrong'})


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
                api_log_user_in({'username': 'loginuser',
                                 'password': s.password})

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
                api_log_user_in({'username': 'loginuser',
                                 'password': s.password})

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
                api_log_user_in({'username': 'loginuser',
                                 'password': s.password})

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
                api_log_user_in({'username': 'loginuser',
                                 'password': s.password})

    assert db.session.query(IpBan).count() == 0


def test_log_user_in_exempts_the_id_1_account_from_every_ban_check(app, db_session, monkeypatch):
    """D583, kept by owner ruling: the id-1 account logs in while banned AND
    ip-banned, because it is the account that set the instance up. The
    exemption is now named -- `User.is_ban_exempt()` -- rather than spelled
    as a bare `user.id != 1` at each ban check.
    """
    if not db.session.get(Instance, 1):
        make_instance('test.piefed.local', software='piefed')
    first = make_user(None, 'firstaccount', local=True)
    assert first.id == 1
    first.set_password('correct horse battery')
    first.banned = True
    db.session.commit()

    with app.test_request_context('/'):
        with _ban_state(monkeypatch, ip_banned=True):
            result = api_log_user_in({'username': 'firstaccount',
                                      'password': 'correct horse battery'})

    assert result['jwt']


def test_only_the_id_1_account_is_ban_exempt(app, db_session):
    """D583, fixed (owner ruling): one named helper states the exemption every
    ban check honours, so no ban check spells the literal `id != 1`."""
    if not db.session.get(Instance, 1):
        make_instance('test.piefed.local', software='piefed')
    first = make_user(None, 'firstaccount', local=True)
    second = make_user(None, 'secondaccount', local=True)
    db.session.commit()

    assert first.id == 1
    assert first.is_ban_exempt() is True
    assert second.is_ban_exempt() is False


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
            api_log_user_in({'username': 'loginuser', 'password': s.password})

    db.session.expire_all()
    stored = db.session.get(User, s.user.id)
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
            result = api_log_user_in({'username': 'loginuser',
                                      'password': s.password})

    assert result['jwt']
