"""`admin_permissions` -- the page that decides what Staff and Admin may do.

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
string is added without being added to `ROLE_PERMISSIONS`. Without it the
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
from app.constants import EDITABLE_ROLE_IDS, ROLE_ADMIN_NAME, ROLE_PERMISSIONS, ROLE_STAFF_NAME
from app.models import Role, RolePermission, user_role
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

    unused = sorted(set(ROLE_PERMISSIONS) - set(in_source))
    assert unused == [], (
        f'offered by the permissions page but checked nowhere: {unused}')


def test_the_vocabulary_has_no_duplicates():
    """A duplicate would render two checkboxes with the same `name`, and the
    second would silently win."""
    assert len(ROLE_PERMISSIONS) == len(set(ROLE_PERMISSIONS))


def _role_name_literals_in_source():
    """Every `'Admin'`/`'Staff'` literal used as a role name in `app/`: compared
    against something (`role.name == 'Admin'`) or passed as `name=` (`Role(name=
    'Admin')`, `filter_by(name='Admin')`). Display strings like `_('Admin')` are
    neither, so they are not collected."""
    names = {'Admin', 'Staff'}
    found = []
    for path in sorted(APP_ROOT.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
            elif isinstance(node, ast.Call):
                operands = [k.value for k in node.keywords if k.arg == 'name']
            else:
                continue
            for operand in operands:
                if isinstance(operand, ast.Constant) and operand.value in names:
                    found.append(f'{path.relative_to(APP_ROOT.parent)}:{node.lineno}')
    return found


def test_role_names_are_spelled_only_through_their_constants():
    """D965, fixed: `is_admin()` and `is_staff()` matched the role NAMES 'Admin'
    and 'Staff' as bare literals, and the CLI seeded and looked them up the same
    way, with no constant behind them -- one rename away from every admin
    silently losing `is_admin()`. They now go through ROLE_ADMIN_NAME and
    ROLE_STAFF_NAME, and this ratchet keeps a new literal from creeping back."""
    assert (ROLE_ADMIN_NAME, ROLE_STAFF_NAME) == ('Admin', 'Staff')
    assert _role_name_literals_in_source() == []


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _roles():
    """The four seeded roles, in the order `flask init-db` creates them, so
    Staff is 3 and Admin is 4 -- which is what EDITABLE_ROLE_IDS hard-codes."""
    roles = {}
    for name, weight in (('Anonymous user', 0), ('Authenticated user', 1),
                         ('Staff', 2), ('Admin', 3)):
        role = Role(name=name, weight=weight)
        db.session.add(role)
        roles[name] = role
    db.session.commit()
    assert (roles['Staff'].id, roles['Admin'].id) == EDITABLE_ROLE_IDS
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
    """An admin who is NOT user 1 (fact 347) and holds the role the page
    itself requires, through the Admin role rather than a bespoke one -- the
    page rewrites that role's permissions, so the caller's own access is part
    of what is under test."""
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1
    admin = make_user(instance, 'roleadmin', local=True)
    admin.verified = True
    db.session.commit()
    assert admin.id != 1

    roles = _roles()
    _grant(roles['Admin'], *ROLE_PERMISSIONS)
    _grant(roles['Staff'], 'approve registrations', 'ban users')
    db.session.execute(user_role.insert().values(user_id=admin.id,
                                                 role_id=roles['Admin'].id))
    db.session.commit()

    client = app.test_client()
    login(client, admin)
    return client, csrf(app, client), roles, admin


def _save(client, token, **checked):
    """POST the page. `checked` maps a role id to the permissions ticked for
    it; everything else is unticked, which is how an HTML checkbox says no."""
    data = {'csrf_token': token}
    for role_id, permissions in checked.items():
        for permission in permissions:
            data[f'role_{role_id}_{permission}'] = 'y'
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.post('/admin/permissions', data=data)
    return response, render


def _rows():
    return sorted(
        (row[0], row[1]) for row in
        db.session.execute(text('SELECT role_id, permission FROM "role_permission"')))


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_the_permissions_page_needs_change_user_roles(app, db_session):
    """Not 'change instance settings': the page that decides what every role
    may do is behind the permission for changing roles, which is the narrower
    one and the one an instance is likely to hand out least."""
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
# D937: the vocabulary must not shrink
# --------------------------------------------------------------------------


def test_unchecking_every_box_for_a_permission_does_not_delete_it(
        permissions_admin):
    """D937's pin, inverted -- the sharpest of the three.

    The page read its list of permissions from `SELECT DISTINCT permission FROM
    role_permission`, then deleted those rows and re-added only the ticked
    ones. Unticking every box for a permission therefore removed the last row
    naming it, and the next render had nothing to draw a checkbox from: the
    permission was gone from the page and could never be granted to anyone
    again. Measured before the fix:

        PROBE p1 before: ['approve registrations', 'change user roles', 'manage users']
        PROBE p1 after unchecking "approve registrations": ['change user roles', 'manage users']
        PROBE p1 offered on the page now: ['change user roles', 'manage users']

    A one-way door on a security control, reachable by unticking two boxes.
    """
    client, token, roles, admin = permissions_admin

    kept = [p for p in ROLE_PERMISSIONS if p != 'approve registrations']
    _response, render = _save(client, token, **{str(roles['Admin'].id): kept})

    # Gone from the table, as it should be -- nobody holds it any more.
    assert ('approve registrations' not in
            [permission for _role, permission in _rows()])
    # Still offered by the page, which is the point.
    assert 'approve registrations' in render.call_args.kwargs['permissions']

    # And grantable again: the second save restores it.
    _response, _render = _save(client, token,
                               **{str(roles['Admin'].id): list(ROLE_PERMISSIONS)})
    assert role_access('approve registrations', roles['Admin'].id) is True


def test_the_page_offers_every_permission_even_on_a_fresh_instance(
        permissions_admin):
    """The GET arm. An instance whose `role_permission` table is empty must
    still be able to grant things -- under the old code the page would have
    rendered no rows at all."""
    client, token, roles, admin = permissions_admin
    db.session.execute(text('DELETE FROM "role_permission"'))
    db.session.commit()

    # As user 1, because with the table empty nobody else passes user_access --
    # fact 347, and it is also who actually sets an instance up.
    founder_client = client.application.test_client()
    login(founder_client, db.session.get(type(admin), 1))

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = founder_client.get('/admin/permissions')

    assert response.status_code == 200
    assert list(render.call_args.kwargs['permissions']) == list(ROLE_PERMISSIONS)


# --------------------------------------------------------------------------
# D938: other roles must survive a save
# --------------------------------------------------------------------------


def test_a_save_does_not_strip_a_role_the_page_does_not_edit(permissions_admin):
    """D938's pin, inverted.

    `DELETE FROM role_permission` cleared the whole table, and only roles 3 and
    4 were written back -- so a save that changed nothing about Staff or Admin
    silently removed every permission from every other role. Measured with a
    'Moderator' role holding 'manage users': `[('manage users',)]` before,
    `[]` after.
    """
    client, token, roles, admin = permissions_admin
    moderator = Role(name='Moderator', weight=5)
    db.session.add(moderator)
    db.session.commit()
    _grant(moderator, 'manage users', 'ban users')

    _save(client, token, **{str(roles['Admin'].id): list(ROLE_PERMISSIONS)})

    surviving = sorted(permission for role_id, permission in _rows()
                       if role_id == moderator.id)
    assert surviving == ['ban users', 'manage users']


def test_a_save_still_replaces_the_editable_roles_entirely(permissions_admin):
    """The other half of D938: scoping the delete must not turn into never
    deleting. A permission unticked for Staff has to stop being granted to
    Staff, which is the whole purpose of the page."""
    client, token, roles, admin = permissions_admin
    assert role_access('ban users', roles['Staff'].id) is True

    _save(client, token, **{str(roles['Staff'].id): ['approve registrations'],
                            str(roles['Admin'].id): list(ROLE_PERMISSIONS)})

    assert role_access('ban users', roles['Staff'].id) is False
    assert role_access('approve registrations', roles['Staff'].id) is True


# --------------------------------------------------------------------------
# D939: the cache invalidation
# --------------------------------------------------------------------------


def test_the_cache_invalidation_names_the_permission_and_every_affected_user(
        permissions_admin):
    """D939's pin, inverted -- asserted as a CALL, not as an effect.

    `user_access` is `@cache.memoize(timeout=50)`, and the page invalidated it
    with `cache.delete_memoized(user_access, permission, staff_user_id)` where
    `permission` was the SELECT's **Row** -- repr `('ban users',)` -- not the
    string `user_access` is called with. The key never matched, so a revoked
    permission kept working for up to fifty seconds after the admin was told
    'Settings saved'. The same loop only ever collected users holding role 3,
    so an ADMIN whose permissions changed was never invalidated at all.

    Neither can be observed through the cache here: `tests/conftest.py` sets
    `CACHE_TYPE = 'NullCache'`, so `@cache.memoize` stores nothing and a
    revoked permission reads correctly whether or not anything was
    invalidated. Fact 378. Asserting the arguments is what remains, and it is
    what fact 371 says to do anyway -- the arguments ARE the behaviour when the
    call itself cannot be watched.
    """
    client, token, roles, admin = permissions_admin
    instance = make_instance('other.example', software='piefed')
    staffer = make_user(instance, 'staffer', local=True)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=staffer.id,
                                                 role_id=roles['Staff'].id))
    db.session.commit()

    with patch('app.admin.routes.cache.delete_memoized') as delete_memoized:
        _save(client, token, **{str(roles['Admin'].id): list(ROLE_PERMISSIONS)})

    # `cache` is shared, so the patch also catches invalidations other code
    # makes during the request; only the user_access ones are this page's.
    calls = [call.args for call in delete_memoized.call_args_list
             if call.args and call.args[0] is user_access]
    assert calls, 'user_access was never invalidated at all'

    # Every argument is the string, never a Row or a 1-tuple.
    for _function, permission, _user_id in calls:
        assert isinstance(permission, str), (
            f'{permission!r} is not the key user_access is memoized under')

    # Every permission, for every user holding either editable role -- the
    # staffer AND the admin, which is the half the old code never attempted.
    assert set(calls) == {(user_access, permission, user_id)
                          for permission in ROLE_PERMISSIONS
                          for user_id in (staffer.id, admin.id)}


def test_a_revoked_permission_is_actually_revoked(permissions_admin):
    """The underlying behaviour the invalidation exists to expose, asserted
    through `user_access` itself so the row says something even though the
    cache is a NullCache."""
    client, token, roles, admin = permissions_admin
    instance = make_instance('other.example', software='piefed')
    staffer = make_user(instance, 'staffer', local=True)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=staffer.id,
                                                 role_id=roles['Staff'].id))
    db.session.commit()
    assert user_access('ban users', staffer.id) is True

    _save(client, token, **{str(roles['Staff'].id): ['approve registrations'],
                            str(roles['Admin'].id): list(ROLE_PERMISSIONS)})

    assert user_access('ban users', staffer.id) is False
    assert user_access('approve registrations', staffer.id) is True


# --------------------------------------------------------------------------
# The rest of the page
# --------------------------------------------------------------------------


def test_a_save_reports_that_it_saved(permissions_admin):
    client, token, roles, admin = permissions_admin

    with patch('app.admin.routes.flash') as flashed:
        with patch('app.admin.routes.render_template', return_value='rendered'):
            client.post('/admin/permissions', data={'csrf_token': token})

    assert [call.args[0] for call in flashed.call_args_list] == ['Settings saved']


def test_the_page_lists_the_roles_above_authenticated_user(permissions_admin):
    """`Role.id > 2`, ordered by weight. Anonymous and Authenticated hold no
    permissions and have no column on the page."""
    client, token, roles, admin = permissions_admin
    moderator = Role(name='Moderator', weight=5)
    db.session.add(moderator)
    db.session.commit()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/permissions')

    listed = [role.name for role in render.call_args.kwargs['roles']]
    assert listed == ['Staff', 'Admin', 'Moderator']


def test_a_get_changes_nothing(permissions_admin):
    """`if request.method == 'POST':` -- rendering the page must not rewrite
    the table, which is what makes a GET safe to link to."""
    client, token, roles, admin = permissions_admin
    before = _rows()

    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.get('/admin/permissions')

    assert _rows() == before


def test_a_permission_ticked_for_one_role_is_not_granted_to_the_other(
        permissions_admin):
    """The two checkbox columns are independent, and the form field name is
    the only thing that distinguishes them."""
    client, token, roles, admin = permissions_admin

    _save(client, token, **{str(roles['Staff'].id): ['ban users'],
                            str(roles['Admin'].id): ['approve registrations']})

    assert role_access('ban users', roles['Staff'].id) is True
    assert role_access('ban users', roles['Admin'].id) is False
    assert role_access('approve registrations', roles['Admin'].id) is True
    assert role_access('approve registrations', roles['Staff'].id) is False


def test_the_template_renders_a_checkbox_for_every_permission(permissions_admin):
    """The route and the template agree on the shape of `permissions`.

    Switching the constant from the SELECT's 1-tuples to plain strings meant
    changing `permission[0]` to `permission` in three places in
    `admin/permissions.html`; a route that renders and a template that indexes
    would silently produce a page of single characters. This is the one row
    here that renders the real template.
    """
    client, token, roles, admin = permissions_admin

    # CSRF on for this row only. tests/conftest.py turns it off, so FlaskForm
    # declares no csrf_token field and `{{ form.csrf_token() }}` in the
    # template raises UndefinedError -- fact 363. Every other row here patches
    # render_template away; this one needs the real thing.
    with patch.dict(client.application.config, {'WTF_CSRF_ENABLED': True}):
        response = client.get('/admin/permissions')

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    for permission in ROLE_PERMISSIONS:
        for role_id in EDITABLE_ROLE_IDS:
            assert f'name="role_{role_id}_{permission}"' in body, (
                f'no checkbox for {permission} on role {role_id}')
    # The old bug would have rendered the first character of each string.
    assert not re.search(r'name="role_3_[a-z]"', body)


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
