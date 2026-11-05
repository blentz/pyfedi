"""The permission vocabulary, and the old `/admin/permissions` page's guard and masquerade.

The page itself is gone: RBAC step 7 replaced it with /admin/roles (tests/test_role_admin_routes.py), which edits one
role at a time, and its D937-D939 rows moved to tests/test_role_admin.py and tests/test_role_admin_routes.py. What
follows is the history of the page it replaced.

Sub-project 79, slice E. Three defects were found in seventeen lines, and all
three came from the same decision: the page derived the set of permissions it
offers from `SELECT DISTINCT permission FROM role_permission`, i.e. from the
rows it was about to delete.

* unchecking every box for a permission removed the last row naming it, so it
  vanished from the page and could never be granted again (D937);
* `DELETE FROM role_permission` cleared the whole table while only roles 3 and
  4 were written back, so any other role was silently stripped (D938);
* the memoize invalidation was passed the SELECT's `Row` rather than the
  permission string, so it never matched the key `user_access` is called with
  (D939).

The durable artefact is `test_the_permission_vocabulary_matches_the_codebase`:
a ratchet that fails if a `permission_required(...)` or `user_access(...)`
string is added without being added to the catalogue (`ROLE_PERMISSIONS`, app/permissions.py). Without it the
constant is just a list that goes stale, and a permission the code checks but
the page cannot grant is the same one-way door as D937 by another route.
"""
import ast
import pathlib
import re
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app import cache, db
from app.permissions import ROLE_PERMISSIONS
from app.roles import seed_default_roles
from app.models import Role, RolePermission, role_grant
from app.utils import role_access, user_access
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / 'app'


# --------------------------------------------------------------------------
# The ratchet
# --------------------------------------------------------------------------


def _permission_strings_in_source():
    """Every literal handed to `permission_required(...)` or `user_access(...)`.

    AST, not a regex over the text: a regex cannot tell a call from the same
    words in a docstring or a comment, and this file's own prose names most of
    these strings. Only literal arguments are collected -- a computed one
    cannot be checked here and would need its own row.
    """
    found = {}
    for path in sorted(APP_ROOT.rglob('*.py')):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
            if name not in ('permission_required', 'user_access'):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, []).append(
                    f'{path.relative_to(APP_ROOT.parent)}:{node.lineno}')
    return found


def test_the_permission_vocabulary_matches_the_codebase():
    """`ROLE_PERMISSIONS` must name every permission the code checks.

    A permission the code tests for but the page cannot offer is unreachable:
    nobody can ever be granted it, and the check silently always fails. A
    permission in the constant that nothing checks is dead UI. Both directions
    are asserted, and the failure names the call sites so the fix is obvious.
    """
    in_source = _permission_strings_in_source()

    unreachable = sorted(set(in_source) - set(ROLE_PERMISSIONS))
    assert unreachable == [], (
        'checked in the code but absent from ROLE_PERMISSIONS, so the '
        'permissions page cannot grant it and the check can never pass: '
        + '; '.join(f'{p} ({", ".join(in_source[p])})' for p in unreachable))

    unused = set(ROLE_PERMISSIONS) - set(in_source)
    assert unused == NOT_YET_CHECKED, (
        f'offered by the permissions page but checked nowhere: {sorted(unused - NOT_YET_CHECKED)}; '
        f'now checked, so take them off NOT_YET_CHECKED: {sorted(NOT_YET_CHECKED - unused)}')


# Every permission in the catalogue is checked somewhere now (RBAC step 10). A new one that is not goes here with why.
NOT_YET_CHECKED = set()


def test_the_vocabulary_has_no_duplicates():
    """A duplicate would render two checkboxes with the same `name`, and the
    second would silently win."""
    assert len(ROLE_PERMISSIONS) == len(set(ROLE_PERMISSIONS))


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _roles():
    """The roles `flask init-db` seeds (the built-in Admin, and Staff with its
    permissions) plus a Moderator role, so the page has two roles to edit: it
    edits every role but the built-in Admin, which holds every permission."""
    roles = seed_default_roles()
    roles['Moderator'] = Role(name='Moderator')
    db.session.add(roles['Moderator'])
    db.session.commit()
    return roles


def _grant(role, *permissions):
    for permission in permissions:
        db.session.add(RolePermission(role_id=role.id, permission=permission))
    db.session.commit()


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """Fact 355."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


@pytest.fixture(autouse=True)
def no_memoized_answers():
    """`user_access` is `@cache.memoize(timeout=50)` and this page's whole job
    is to change what it answers."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def permissions_admin(app, db_session):
    """An admin who is NOT user 1 (fact 347) and holds the built-in Admin role,
    which the page cannot edit: a save never takes away the caller's own access."""
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1
    admin = make_user(instance, 'roleadmin', local=True)
    admin.verified = True
    db.session.commit()
    assert admin.id != 1

    roles = _roles()
    db.session.execute(role_grant.insert().values(user_id=admin.id,
                                                 role_id=roles['Admin'].id))
    db.session.commit()

    client = app.test_client()
    login(client, admin)
    return client, csrf(app, client), roles, admin


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_the_permissions_page_is_refused_to_a_plain_user(app, db_session):
    """The page that decides what every role may do is Admin's alone (RB5); the
    permission it used to sit behind, 'change user roles', is gone."""
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    ordinary = make_user(instance, 'ordinary', local=True)
    ordinary.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, ordinary)

    response = client.get('/admin/permissions')

    assert response.status_code == 302
    assert '/permission_denied' in response.headers['Location']


# --------------------------------------------------------------------------
# masquerade
# --------------------------------------------------------------------------


def test_masquerade_needs_the_settings_permission(app, db_session):
    """`/admin/masquerade/<id>` logs the caller in AS another account. There is
    no confirmation, no audit record and no way back except logging in again,
    so the guard is the only thing standing between a staff account and every
    local account on the instance."""
    instance = make_instance('test.piefed.local', software='piefed')
    make_user(instance, 'founder', local=True)
    ordinary = make_user(instance, 'ordinary', local=True)
    ordinary.verified = True
    target = make_user(instance, 'target', local=True)
    db.session.commit()
    client = app.test_client()
    login(client, ordinary)

    response = client.get(f'/admin/masquerade/{target.id}')

    assert response.status_code == 302
    assert '/permission_denied' in response.headers['Location']
    with client.session_transaction() as session:
        assert session['_user_id'] == str(ordinary.id), 'the session changed anyway'


def test_masquerade_becomes_the_named_local_user(permissions_admin):
    """The feature itself. The assertion is on the SESSION, not on the
    redirect: a route that redirected to '/' without logging anyone in would
    pass a status-code assertion."""
    client, token, roles, admin = permissions_admin
    instance = make_instance('other.example', software='piefed')
    target = make_user(instance, 'target', local=True)
    db.session.commit()

    response = client.get(f'/admin/masquerade/{target.id}')

    assert response.status_code == 302
    assert response.headers['Location'] == '/'
    with client.session_transaction() as session:
        # str() on both sides: User.get_id() returns an int, not the string
        # Flask-Login's own UserMixin would, so the session holds whichever
        # type last wrote it. load_user() does int(id), so both work.
        assert str(session['_user_id']) == str(target.id)


def test_masquerade_does_not_set_a_remember_me_cookie(permissions_admin):
    """`login_user(user, False)` -- the second argument is `remember`.

    With it True, masquerading would write a `remember_token` cookie and the
    admin would still be logged in as the target after closing the browser,
    with nothing on screen to say so. For a debugging feature that has no
    confirmation and no audit record, "ends with the session" is the only
    containment there is. The mutation pass found this: the row above asserts
    who is logged in, which is identical either way.
    """
    client, token, roles, admin = permissions_admin
    instance = make_instance('other.example', software='piefed')
    target = make_user(instance, 'target', local=True)
    db.session.commit()

    response = client.get(f'/admin/masquerade/{target.id}')

    cookies = response.headers.getlist('Set-Cookie')
    assert not any('remember_token' in cookie for cookie in cookies), cookies


def test_masquerade_refuses_a_remote_account(permissions_admin):
    """`user.is_local()`. A remote account has no password and no session on
    this instance; logging in as one would produce a local session for an
    identity this instance does not own, and anything done in it would federate
    outward under that name."""
    client, token, roles, admin = permissions_admin
    remote_instance = make_instance('remote.example', software='mastodon')
    remote = make_user(remote_instance, 'remoteuser', local=False)
    db.session.commit()

    response = client.get(f'/admin/masquerade/{remote.id}')

    assert response.status_code == 200
    assert response.get_data() == b''
    with client.session_transaction() as session:
        assert session['_user_id'] == str(admin.id)


def test_masquerade_refuses_an_id_that_does_not_exist(permissions_admin):
    """`user is not None`. The id comes straight out of the URL, so a stale
    link or a typo must not reach `login_user(None, ...)`."""
    client, token, roles, admin = permissions_admin

    response = client.get('/admin/masquerade/999999')

    assert response.status_code == 200
    assert response.get_data() == b''
    with client.session_transaction() as session:
        assert session['_user_id'] == str(admin.id)
