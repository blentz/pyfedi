"""User administration: `admin_users`, `admin_users_add`, `admin_user_edit`,
the delete pair and the resend-email endpoint.

Sub-project 79, slice F -- the last of `app/admin/routes.py`'s security
surface. This is where accounts are created, banned, promoted, demoted and
destroyed.

Seven defects were found:

* the "Banned" and "Email address is verified" checkboxes on the add-user form
  did nothing at all (D944);
* a refused edit silently replaced the admin's typed note and Banned tick with
  the stored values (D945);
* a demoted administrator kept every permission for fifty seconds, which the
  page apologised for in a flash message rather than fixing (D946);
* deleting a LOCAL user was never written to the modlog, while deleting a
  remote one was (D947);
* `unsubscribe_from_everything_then_delete_task` dereferenced a user outside
  its own `if user:` (D948);
* the verified filter was dropped from the pagination links (D949);
* the resend-email endpoint returned the mail exception's text to the browser
  (D950).
"""
import io
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app import cache, db
from app.constants import ROLE_PERMISSIONS
from app.models import Role, RolePermission, User, user_role
from app.utils import user_access
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def _roles():
    roles = {}
    for name, weight in (('Anonymous user', 0), ('Authenticated user', 1),
                         ('Staff', 2), ('Admin', 3)):
        role = Role(name=name, weight=weight)
        db.session.add(role)
        roles[name] = role
    db.session.commit()
    return roles


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
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def users_admin(app, db_session):
    """An admin who is NOT user 1 (fact 347) and holds exactly the two
    permissions these routes check -- 'administer all users' for the pages and
    'change user roles' for the role selector, which is separately gated."""
    instance = _instance()
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1
    admin = make_user(instance, 'useradmin', local=True)
    admin.verified = True
    db.session.commit()
    assert admin.id != 1

    roles = _roles()
    for permission in ('administer all users', 'change user roles'):
        db.session.add(RolePermission(role_id=roles['Admin'].id,
                                      permission=permission))
    db.session.execute(user_role.insert().values(user_id=admin.id,
                                                 role_id=roles['Admin'].id))
    db.session.commit()

    client = app.test_client()
    login(client, admin)
    return client, csrf(app, client), roles, admin


def _instance(domain='test.piefed.local', software='piefed'):
    """Get-or-create. `tests.factories.make_instance` always INSERTs, and
    `instance.domain` is unique, so calling it twice for the local instance --
    which the fixture has already made -- is a UniqueViolation rather than a
    second row."""
    from app.models import Instance

    existing = Instance.query.filter_by(domain=domain).first()
    if existing is not None:
        return existing
    return make_instance(domain, software=software)


def _target(name='target', local=True, domain='test.piefed.local'):
    user = make_user(_instance(domain), name, local=local)
    db.session.commit()
    return user


def _add_payload(token, **overrides):
    data = {'user_name': 'newbie', 'email': 'newbie@example.com',
            'password': 'password123', 'password2': 'password123',
            'about': '', 'matrix_user_id': '', 'ignore_bots': '0',
            'hide_nsfw': '1', 'hide_nsfl': '1', 'role': '2',
            'submit': 'Save', 'csrf_token': token,
            'profile_file': (io.BytesIO(b''), ''),
            'banner_file': (io.BytesIO(b''), '')}
    data.update(overrides)
    return data


def _add(client, token, **overrides):
    """Fact 362: multipart, because the route reads request.files
    unconditionally. Fact 363: render_template patched."""
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        with patch('app.admin.routes.finalize_user_setup') as finalize:
            response = client.post('/admin/users/add',
                                   data=_add_payload(token, **overrides),
                                   content_type='multipart/form-data')
    return response, render, finalize


def _edit_payload(token, **overrides):
    data = {'admin_note': '', 'hide_nsfw': '1', 'hide_nsfl': '1', 'role': '2',
            'submit': 'Save', 'csrf_token': token}
    data.update(overrides)
    return data


def _edit(client, token, user_id, **overrides):
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.post(f'/admin/user/{user_id}/edit',
                               data=_edit_payload(token, **overrides))
    return response, render


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


@pytest.mark.parametrize('method, path', [
    ('get', '/admin/users'),
    ('get', '/admin/users/add'),
    ('get', '/admin/user/2/edit'),
    ('post', '/admin/user/2/delete'),
    ('post', '/admin/user/2/resend_email'),
])
def test_the_user_pages_need_administer_all_users(app, db_session, method, path):
    instance = _instance()
    make_user(instance, 'founder', local=True)
    ordinary = make_user(instance, 'ordinary', local=True)
    ordinary.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, ordinary)

    # A POST carries a token: login_required, now the outer decorator (D943),
    # checks it before permission_required can refuse.
    data = {'csrf_token': csrf(app, client)} if method == 'post' else None
    response = getattr(client, method)(path, data=data)

    assert response.status_code == 302
    assert '/permission_denied' in response.headers['Location']


def test_the_role_selector_needs_change_user_roles(app, db_session):
    """The role change inside `admin_user_edit` is gated SEPARATELY from the
    page. An admin who may edit users but not change roles must be able to
    save everything else and change nothing about the target's role."""
    instance = _instance()
    make_user(instance, 'founder', local=True)
    admin = make_user(instance, 'editor', local=True)
    admin.verified = True
    db.session.commit()
    roles = _roles()
    db.session.add(RolePermission(role_id=roles['Admin'].id,
                                  permission='administer all users'))
    db.session.execute(user_role.insert().values(user_id=admin.id,
                                                 role_id=roles['Admin'].id))
    db.session.commit()

    target = _target()
    db.session.execute(user_role.insert().values(user_id=target.id,
                                                 role_id=roles['Staff'].id))
    db.session.commit()

    client = app.test_client()
    login(client, admin)
    token = csrf(app, client)

    _edit(client, token, target.id, role=str(roles['Admin'].id),
          admin_note='a note that should be saved')

    db.session.expire_all()
    target = db.session.get(User, target.id)
    assert target.admin_note == 'a note that should be saved'
    assert [role.name for role in target.roles] == ['Staff'], (
        'the role changed without the change user roles permission')


# --------------------------------------------------------------------------
# D944: the checkboxes that did nothing
# --------------------------------------------------------------------------


def test_adding_a_user_honours_the_banned_and_verified_checkboxes(users_admin):
    """D944's pin, inverted.

    `AddUserForm` declares `banned` and `verified` and `admin_users_add` set
    neither, so an admin creating a pre-banned account got an active one and an
    admin marking an address verified got an unverified user -- with no error
    either time. Measured before the fix:

        PROBE u1 ticked Banned and Verified -> banned: False verified: False
    """
    client, token, roles, admin = users_admin

    response, _render, _finalize = _add(client, token, banned='y', verified='y')

    assert response.status_code == 302
    created = User.query.filter_by(user_name='newbie').one()
    assert created.banned is True
    assert created.verified is True


def test_adding_a_user_without_those_boxes_leaves_them_off(users_admin):
    """The other half: the fix must read the checkbox, not hard-code True."""
    client, token, roles, admin = users_admin

    _add(client, token)

    created = User.query.filter_by(user_name='newbie').one()
    assert created.banned is False
    assert created.verified is False


def test_adding_a_user_stores_the_rest_of_the_form(users_admin):
    """The sixteen attributes that were already copied. Asserted together
    because a row per field would say nothing more, and asserted at all because
    D944 was two fields missing from exactly this list."""
    client, token, roles, admin = users_admin

    _add(client, token, user_name='newbie', email='newbie@example.com',
         about='# hello', matrix_user_id='@n:matrix.org', bot='y',
         newsletter='y', ignore_bots='2', hide_nsfw='3', hide_nsfl='0',
         role=str(roles['Staff'].id))

    created = User.query.filter_by(user_name='newbie').one()
    assert created.title == 'newbie'
    assert created.email == 'newbie@example.com'
    assert created.about == '# hello'
    assert '<h1>hello</h1>' in created.about_html
    assert created.matrix_user_id == '@n:matrix.org'
    assert (created.bot, created.newsletter) == (True, True)
    assert (created.ignore_bots, created.hide_nsfw, created.hide_nsfl) == (2, 3, 0)
    assert created.instance_id == 1
    assert created.verification_token
    assert created.check_password('password123')
    assert [role.name for role in created.roles] == ['Staff']


def test_adding_a_user_finalizes_the_account(users_admin):
    """`finalize_user_setup` generates the keypair and the local actor; without
    it the account cannot federate and cannot be deleted by the normal path --
    `admin_user_delete_task` branches on `private_key is not None`."""
    client, token, roles, admin = users_admin

    _response, _render, finalize = _add(client, token)

    created = User.query.filter_by(user_name='newbie').one()
    assert finalize.call_args.args == (created,)


def test_adding_a_user_saves_an_avatar_and_a_banner(users_admin):
    """`request.files` for both fields. The route no longer tries to remove an
    existing avatar first -- `user` is a fresh `User()`, so there never is one
    (D951)."""
    from app.models import File

    client, token, roles, admin = users_admin
    avatar = File(source_url='https://example.com/avatar.png', file_path='a.png')
    banner = File(source_url='https://example.com/banner.png', file_path='b.png')
    db.session.add_all([avatar, banner])
    db.session.commit()

    with patch('app.admin.routes.save_icon_file', return_value=avatar) as save_icon:
        with patch('app.admin.routes.save_banner_file', return_value=banner) as save_banner:
            _add(client, token,
                 profile_file=(io.BytesIO(b'avatar bytes'), 'avatar.png'),
                 banner_file=(io.BytesIO(b'banner bytes'), 'banner.png'))

    created = User.query.filter_by(user_name='newbie').one()
    assert save_icon.call_args.args[1] == 'users'
    assert save_banner.call_args.args[1] == 'users'
    assert created.avatar_id == avatar.id
    assert created.cover_id == banner.id


def test_an_upload_the_saver_rejects_leaves_no_avatar(users_admin):
    """`if file:` -- `save_icon_file` returns None for something it will not
    accept, and assigning that to `user.avatar` would break the profile page."""
    client, token, roles, admin = users_admin

    with patch('app.admin.routes.save_icon_file', return_value=None):
        _add(client, token,
             profile_file=(io.BytesIO(b'not an image'), 'avatar.png'))

    created = User.query.filter_by(user_name='newbie').one()
    assert created.avatar is None


def test_a_duplicate_email_is_refused(users_admin):
    """`AddUserForm.validate_email`. The form's own validator, pinned here
    because the refusal arm is what renders the page again."""
    client, token, roles, admin = users_admin
    existing = _target('existing')
    existing.email = 'taken@example.com'
    db.session.commit()

    response, render, _finalize = _add(client, token, email='taken@example.com')

    assert response.status_code == 200
    assert 'email' in render.call_args.kwargs['form'].errors
    assert User.query.filter_by(user_name='newbie').count() == 0


def test_the_add_user_page_renders_a_blank_form(users_admin):
    """The GET arm, and the `user = User()` that the template is handed."""
    client, token, roles, admin = users_admin

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.get('/admin/users/add')

    assert response.status_code == 200
    assert render.call_args.args == ('admin/add_user.html',)
    assert render.call_args.kwargs['user'].id is None


# --------------------------------------------------------------------------
# D945: a refused edit must keep what was typed
# --------------------------------------------------------------------------


def test_a_refused_edit_keeps_what_the_admin_typed(users_admin):
    """D945's pin, inverted -- D907's shape for the third time in this module.

    The POST branch ended with `else:`, so a submission the form REFUSED fell
    into the pre-fill arm and was overwritten from the database. The admin's
    typed note and their Banned tick were both replaced by the stored values,
    on a page that looked as though they had entered nothing. Measured:

        PROBE u2 errors: {'role': ['Not a valid choice.']}
        PROBE u2 admin_note redisplayed as: 'the stored note'
        PROBE u2 banned redisplayed as: False

    Here it destroys moderation input rather than a settings field, which is
    why it is a P and not another registration.
    """
    client, token, roles, admin = users_admin
    target = _target()
    target.admin_note = 'the stored note'
    db.session.commit()

    _response, render = _edit(client, token, target.id, role='99',
                              admin_note='what the admin just typed', banned='y')

    form = render.call_args.kwargs['form']
    assert 'role' in form.errors
    assert form.admin_note.data == 'what the admin just typed'
    assert form.banned.data is True
    # And nothing was saved, which is the point of refusing.
    db.session.expire_all()
    assert db.session.get(User, target.id).admin_note == 'the stored note'


def test_the_edit_form_is_prefilled_on_a_get(users_admin):
    """The pre-fill arm doing its actual job."""
    client, token, roles, admin = users_admin
    target = _target()
    target.bot = True
    target.bot_override = True
    target.suppress_crossposts = True
    target.verified = True
    target.banned = True
    target.ban_posts = True
    target.ban_comments = True
    target.hide_nsfw = 3
    target.hide_nsfl = 0
    target.can_send_pm = False
    target.admin_note = 'stored note'
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=target.id,
                                                 role_id=roles['Staff'].id))
    db.session.commit()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get(f'/admin/user/{target.id}/edit')

    form = render.call_args.kwargs['form']
    assert (form.bot.data, form.bot_override.data, form.suppress_crossposts.data) == \
        (True, True, True)
    assert (form.verified.data, form.banned.data) == (True, True)
    assert (form.ban_posts.data, form.ban_comments.data) == (True, True)
    assert (form.hide_nsfw.data, form.hide_nsfl.data) == (3, 0)
    assert form.can_send_pm.data is False
    assert form.admin_note.data == 'stored note'
    assert form.role.data == roles['Staff'].id


def test_a_user_with_no_role_leaves_the_selector_at_its_default(users_admin):
    """`if user.roles and user.roles.count() > 0`. `User.roles` is an
    AppenderQuery, so `.count()` and not `len()` -- and a user with none must
    not raise on `roles[0]`."""
    client, token, roles, admin = users_admin
    target = _target()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get(f'/admin/user/{target.id}/edit')

    assert render.call_args.kwargs['form'].role.data == 2


def test_editing_a_remote_user_warns_that_it_will_be_overwritten(users_admin):
    """Most of this form's fields are replaced on the next refresh of a remote
    actor, so the warning is the only thing telling the admin their edit is
    temporary."""
    client, token, roles, admin = users_admin
    remote = _target('remoteuser', local=False, domain='remote.example')

    with patch('app.admin.routes.flash') as flashed:
        with patch('app.admin.routes.render_template', return_value='rendered'):
            client.get(f'/admin/user/{remote.id}/edit')

    assert flashed.call_args.args[0].startswith('This is a remote user')
    assert flashed.call_args.args[1] == 'warning'


def test_editing_a_local_user_does_not_warn(users_admin):
    client, token, roles, admin = users_admin
    target = _target()

    with patch('app.admin.routes.flash') as flashed:
        with patch('app.admin.routes.render_template', return_value='rendered'):
            client.get(f'/admin/user/{target.id}/edit')

    assert flashed.call_args_list == []


def test_an_edit_saves_every_field(users_admin):
    client, token, roles, admin = users_admin
    target = _target()

    _edit(client, token, target.id, bot='y', bot_override='y',
          suppress_crossposts='y', banned='y', ban_posts='y', ban_comments='y',
          can_send_pm='y', hide_nsfw='2', hide_nsfl='3',
          admin_note='a note', role=str(roles['Staff'].id))

    db.session.expire_all()
    target = db.session.get(User, target.id)
    assert (target.bot, target.bot_override, target.suppress_crossposts) == \
        (True, True, True)
    assert (target.banned, target.ban_posts, target.ban_comments) == (True, True, True)
    assert target.can_send_pm is True
    assert (target.hide_nsfw, target.hide_nsfl) == (2, 3)
    assert target.admin_note == 'a note'
    assert [role.name for role in target.roles] == ['Staff']


def test_marking_a_user_verified_finalizes_their_account(users_admin):
    """`if form.verified.data and not user.verified: finalize_user_setup(user)`.
    An account that was never verified has no keypair, so verifying it by hand
    has to do the setup the verification email would have."""
    client, token, roles, admin = users_admin
    target = _target()
    target.verified = False
    db.session.commit()

    with patch('app.admin.routes.finalize_user_setup') as finalize:
        _edit(client, token, target.id, verified='y')

    assert finalize.call_args.args[0].id == target.id
    db.session.expire_all()
    assert db.session.get(User, target.id).verified is True


def test_an_already_verified_user_is_not_finalized_again(users_admin):
    """The `and not user.verified` half. Running the setup twice would replace
    a working keypair on an account that is already federating."""
    client, token, roles, admin = users_admin
    target = _target()
    target.verified = True
    db.session.commit()

    with patch('app.admin.routes.finalize_user_setup') as finalize:
        _edit(client, token, target.id, verified='y')

    assert finalize.call_args_list == []


@pytest.mark.parametrize('field, column', [('remove_avatar', 'avatar_id'),
                                           ('remove_banner', 'cover_id')])
def test_removing_an_image_deletes_the_file_from_disk_and_the_row(
        users_admin, field, column):
    """Both `remove_*` arms. The file is unlinked AND the row deleted AND the
    column cleared -- leaving any one of the three behind is a different kind
    of broken."""
    from app.models import File

    client, token, roles, admin = users_admin
    target = _target()
    image = File(source_url='https://example.com/a.png', file_path='a.png')
    db.session.add(image)
    db.session.commit()
    setattr(target, column, image.id)
    db.session.commit()

    with patch.object(File, 'delete_from_disk') as delete_from_disk:
        _edit(client, token, target.id, **{field: 'y'})

    assert delete_from_disk.call_count == 1
    db.session.expire_all()
    assert getattr(db.session.get(User, target.id), column) is None
    assert db.session.get(File, image.id) is None


@pytest.mark.parametrize('field', ['remove_avatar', 'remove_banner'])
def test_removing_an_image_that_is_not_there_does_nothing(users_admin, field):
    """The `and user.avatar_id` half of each guard -- without it the route
    would call `delete_from_disk` on None."""
    from app.models import File

    client, token, roles, admin = users_admin
    target = _target()

    with patch.object(File, 'delete_from_disk') as delete_from_disk:
        response, _render = _edit(client, token, target.id, **{field: 'y'})

    assert response.status_code == 302
    assert delete_from_disk.call_args_list == []


# --------------------------------------------------------------------------
# D946: a demoted admin must lose their permissions now
# --------------------------------------------------------------------------


def test_a_role_change_invalidates_the_permission_cache(users_admin):
    """D946's pin, inverted.

    `user_access` is `@cache.memoize(timeout=50)` and this route rewrote
    `user_role` while invalidating nothing, so an administrator stripped of
    their role went on passing every permission check for up to fifty seconds
    after the change was saved. The page knew: it flashed "Permissions are
    cached for 50 seconds so new admin roles won't take effect immediately."

    Fact 378 -- `CACHE_TYPE` is `NullCache` here, so the staleness itself
    cannot be observed and the assertion is on the call, as in slice E.
    """
    client, token, roles, admin = users_admin
    target = _target()
    db.session.execute(user_role.insert().values(user_id=target.id,
                                                 role_id=roles['Admin'].id))
    db.session.commit()

    with patch('app.admin.routes.cache.delete_memoized') as delete_memoized:
        _edit(client, token, target.id, role='2')

    invalidated = {call.args for call in delete_memoized.call_args_list
                   if call.args and call.args[0] is user_access}
    assert invalidated == {(user_access, permission, target.id)
                           for permission in ROLE_PERMISSIONS}


def test_a_demotion_actually_removes_the_role(users_admin):
    """The behaviour the invalidation exists to expose, asserted through
    `user_access` itself so the row says something under a NullCache."""
    client, token, roles, admin = users_admin
    db.session.add(RolePermission(role_id=roles['Admin'].id,
                                  permission='ban users'))
    db.session.commit()
    target = _target()
    db.session.execute(user_role.insert().values(user_id=target.id,
                                                 role_id=roles['Admin'].id))
    db.session.commit()
    assert user_access('ban users', target.id) is True

    _edit(client, token, target.id, role='2')

    assert user_access('ban users', target.id) is False


def test_a_role_change_refreshes_the_cached_admin_ids(users_admin):
    """`g.admin_ids` and the `admin_ids` setting are recomputed from the
    `user_role` table, and user 1 is in the union unconditionally -- the
    instance owner is an admin whether or not they hold the role."""
    from app.utils import get_setting

    client, token, roles, admin = users_admin
    target = _target()

    _edit(client, token, target.id, role=str(roles['Admin'].id))

    admin_ids = get_setting('admin_ids', None)
    assert 1 in admin_ids
    assert target.id in admin_ids


def test_a_banned_user_is_not_counted_as_an_admin(users_admin):
    """The `u.deleted = false AND u.banned = false` half of that query: an
    admin who is banned in the same request must not remain in `admin_ids`."""
    from app.utils import get_setting

    client, token, roles, admin = users_admin
    target = _target()
    db.session.execute(user_role.insert().values(user_id=target.id,
                                                 role_id=roles['Admin'].id))
    db.session.commit()

    _edit(client, token, target.id, role=str(roles['Admin'].id), banned='y')

    assert target.id not in get_setting('admin_ids', [])


def test_editing_a_user_that_does_not_exist_is_a_404(users_admin):
    client, token, roles, admin = users_admin

    response = client.get('/admin/user/999999/edit')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D947 and D948: deletion
# --------------------------------------------------------------------------


def test_user_one_cannot_be_deleted(users_admin):
    """The instance owner passes every `user_access` check (fact 347), so
    deleting them would leave an instance nobody can administer."""
    client, token, roles, admin = users_admin

    with patch('app.admin.routes.admin_user_delete_task') as task:
        with patch('app.admin.routes.flash') as flashed:
            response = client.post('/admin/user/1/delete',
                                   data={'csrf_token': token})

    assert response.status_code == 302
    assert task.delay.call_args_list == []
    assert flashed.call_args.args[0] == 'This user cannot be deleted.'
    assert db.session.get(User, 1).deleted is False


def test_deleting_a_user_bans_them_immediately_and_queues_the_task(users_admin):
    """The ban is not the deletion -- it hides the account from the UI while
    the unsubscribe-from-everything task runs, which can take a long time. Both
    halves are asserted because either alone leaves the account half-deleted."""
    client, token, roles, admin = users_admin
    target = _target()

    with patch('app.admin.routes.admin_user_delete_task') as task:
        response = client.post(f'/admin/user/{target.id}/delete',
                               data={'csrf_token': token})

    assert response.status_code == 302
    db.session.expire_all()
    target = db.session.get(User, target.id)
    assert target.banned is True
    assert target.deleted_by == admin.id
    assert task.delay.call_args.args == (target.id, admin.id)


def test_deleting_a_user_runs_inline_in_debug(app, users_admin):
    client, token, roles, admin = users_admin
    target = _target()

    with patch('app.admin.routes.admin_user_delete_task') as task:
        with patch.dict(app.config, {'DEBUG': True}):
            client.post(f'/admin/user/{target.id}/delete',
                        data={'csrf_token': token})

    assert task.call_args.args == (target.id, admin.id)
    assert task.delay.call_args_list == []


def test_deleting_a_user_that_does_not_exist_is_a_404(users_admin):
    client, token, roles, admin = users_admin

    response = client.post('/admin/user/999999/delete', data={'csrf_token': token})

    assert response.status_code == 404


@pytest.mark.parametrize('local, finalized, expect_unsubscribe', [
    (True, True, True),     # a finalized local user federates its deletion
    (True, False, False),   # a non-finalized one was never federated anywhere
    (False, False, False),  # a remote user is only dropped locally
])
def test_the_delete_task_writes_a_modlog_entry_for_every_kind_of_user(
        app, db_session, local, finalized, expect_unsubscribe):
    """D947's pin, inverted.

    `add_to_modlog('delete_user', ...)` was in the REMOTE branch alone, so
    deleting one of this instance's own accounts -- the case an audit trail
    exists for -- left no trace anywhere. `unsubscribe_from_everything_then_delete_task`
    does not log either (`grep -c add_to_modlog app/admin/util.py` gives 0), so
    neither local path recorded anything.

    All three shapes are parameterised, because the defect was precisely that
    one of three branches had the call.
    """
    from app.admin.routes import admin_user_delete_task

    instance = _instance()
    actor = make_user(instance, 'founder', local=True)
    target = make_user(_instance('remote.example', 'mastodon') if not local
                       else instance, 'victim', local=local)
    target.private_key = 'a key' if finalized else None
    db.session.commit()

    target_id, actor_id = target.id, actor.id

    # The ids are read INSIDE the call. The task holds its own session
    # (get_task_session) and commits, so by the time the patch's call_args are
    # inspected the objects it passed are detached and touching .id triggers a
    # refresh that raises DetachedInstanceError.
    logged = []

    def record(action, **kwargs):
        logged.append((action, kwargs['actor'].id, kwargs['target_user'].id))

    with patch('app.admin.routes.add_to_modlog', side_effect=record):
        with patch('app.admin.routes.unsubscribe_from_everything_then_delete') as unsub:
            admin_user_delete_task(target_id, actor_id)

    assert logged == [('delete_user', actor_id, target_id)]
    assert (unsub.call_args_list != []) is expect_unsubscribe


@pytest.mark.parametrize('local', [True, False])
def test_a_non_federated_user_is_marked_deleted_directly(app, db_session, local):
    """The two branches that do the deletion inline rather than handing it to
    the unsubscribe task."""
    from app.admin.routes import admin_user_delete_task

    instance = _instance()
    actor = make_user(instance, 'founder', local=True)
    target = make_user(_instance('remote.example', 'mastodon') if not local
                       else instance, 'victim', local=local)
    target.private_key = None
    db.session.commit()

    with patch('app.admin.routes.add_to_modlog'):
        admin_user_delete_task(target.id, actor.id)

    db.session.expire_all()
    target = db.session.get(User, target.id)
    assert (target.deleted, target.banned) == (True, True)


def test_the_delete_task_does_nothing_for_a_user_that_is_already_gone(
        app, db_session):
    """`if user:`. The task is queued from a route that has already committed,
    so by the time a worker picks it up the row may be gone -- two admins
    pressing Delete, or a retry."""
    from app.admin.routes import admin_user_delete_task

    instance = _instance()
    actor = make_user(instance, 'founder', local=True)
    db.session.commit()

    with patch('app.admin.routes.add_to_modlog') as modlog:
        admin_user_delete_task(999999, actor.id)

    assert modlog.call_args_list == []


def test_the_delete_task_rolls_back_and_re_raises(app, db_session):
    """`except Exception: session.rollback(); raise`."""
    from app.admin.routes import admin_user_delete_task

    instance = _instance()
    actor = make_user(instance, 'founder', local=True)
    target = make_user(instance, 'victim', local=True)
    target.private_key = None
    db.session.commit()

    with patch('app.admin.routes.add_to_modlog', side_effect=ValueError('boom')):
        with pytest.raises(ValueError):
            admin_user_delete_task(target.id, actor.id)

    db.session.expire_all()
    assert db.session.get(User, target.id).deleted is False


def test_the_unsubscribe_task_does_nothing_for_a_user_that_is_already_gone(
        app, db_session):
    """D948's pin, inverted.

    `user.delete_dependencies()` and the UPDATE sat OUTSIDE the `if user:`
    that guards everything above them, so a user already gone when the task ran
    raised `AttributeError: 'NoneType' object has no attribute
    'delete_dependencies'`. The task is queued after the route has committed,
    and `admin_user_delete` is a plain POST with no idempotency, so two clicks
    are enough.
    """
    from app.admin.util import unsubscribe_from_everything_then_delete_task

    _instance()
    db.session.commit()

    unsubscribe_from_everything_then_delete_task(999999)  # must not raise


# --------------------------------------------------------------------------
# D949: the user list
# --------------------------------------------------------------------------


def test_the_user_list_carries_every_filter_into_its_pagination_links(
        users_admin):
    """D949's pin, inverted.

    `next_url` and `prev_url` were built without `verified`, so paging past
    the first page silently dropped the filter the admin had selected and
    showed the unfiltered list. Every filter is asserted, not just the missing
    one, because the next one added will be missed the same way.
    """
    client, token, roles, admin = users_admin
    instance = _instance()
    for index in range(501):
        make_user(instance, f'bulk{index}', local=True)
    db.session.commit()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/users?verified=verified&search=bulk&local_remote=local'
                   '&last_seen=7&sort_by=user_name')

    next_url = render.call_args.kwargs['next_url']
    assert next_url is not None
    for fragment in ('verified=verified', 'search=bulk', 'local_remote=local',
                     'last_seen=7', 'sort_by=user_name'):
        assert fragment in next_url, f'{fragment} was dropped from next_url'

    # And the same for prev_url, from page 2 -- it is built separately, so a
    # row that only checks next_url leaves half the defect in place.
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/users?page=2&verified=verified&search=bulk'
                   '&local_remote=local&last_seen=7&sort_by=user_name')

    prev_url = render.call_args.kwargs['prev_url']
    assert prev_url is not None
    for fragment in ('verified=verified', 'search=bulk', 'local_remote=local',
                     'last_seen=7', 'sort_by=user_name'):
        assert fragment in prev_url, f'{fragment} was dropped from prev_url'


def test_the_previous_link_is_absent_on_the_first_page(users_admin):
    """`users.has_prev and page != 1`. `has_prev` alone is true on page 1 of a
    paginate() whose page argument came back as 1, so the extra test is what
    stops a Previous link pointing at page 0."""
    client, token, roles, admin = users_admin

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/users')

    assert render.call_args.kwargs['prev_url'] is None


@pytest.mark.parametrize('query, expected', [
    ('local_remote=local', {'localuser'}),
    ('local_remote=remote', {'remoteuser'}),
    ('search=remote', {'remoteuser'}),
    # The search is an OR over email AND user_name. These two rows separate
    # them: the address belongs to localuser and the NAME to remoteuser, so
    # dropping either half of the or_() fails one of them.
    ('search=onlyintheaddress', {'localuser'}),
    ('search=remoteus', {'remoteuser'}),
    ('verified=verified', {'localuser'}),
    ('verified=unverified', {'remoteuser'}),
    ('', {'localuser', 'remoteuser'}),
])
def test_each_user_list_filter_selects_its_own_rows(users_admin, query, expected):
    """One row per filter, asserted on the names returned rather than on a
    count -- a count passes when the filter selects the wrong row."""
    client, token, roles, admin = users_admin
    local = make_user(_instance(), 'localuser', local=True)
    local.verified = True
    local.email = 'onlyintheaddress@example.com'
    remote = make_user(_instance('remote.example', 'mastodon'), 'remoteuser',
                       local=False)
    remote.verified = False
    remote.email = 'nothing-in-common@example.com'
    db.session.commit()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get(f'/admin/users?{query}')

    listed = {user.user_name for user in render.call_args.kwargs['users']}
    assert expected <= listed
    assert not (({'localuser', 'remoteuser'} - expected) & listed)


def test_a_deleted_user_is_never_listed(users_admin):
    """`User.query.filter_by(deleted=False)` -- a deleted account has had its
    dependencies removed, so listing it offers actions that cannot work."""
    client, token, roles, admin = users_admin
    gone = _target('gone')
    gone.deleted = True
    db.session.commit()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/users')

    assert 'gone' not in {user.user_name for user in render.call_args.kwargs['users']}


def test_sorting_by_attitude_excludes_users_who_have_none(users_admin):
    """`if 'attitude' in sort_by: users.filter(User.attitude != None)`. A NULL
    attitude sorts to one end and fills the first page with users the admin
    cannot judge."""
    client, token, roles, admin = users_admin
    rated = _target('rated')
    rated.attitude = 0.5
    _target('unrated')
    db.session.commit()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/users?sort_by=attitude DESC')

    listed = {user.user_name for user in render.call_args.kwargs['users']}
    assert 'rated' in listed
    assert 'unrated' not in listed


def test_the_last_seen_filter_excludes_older_accounts(users_admin):
    """`if last_seen > 0` -- zero means no filter, which is the default."""
    from datetime import timedelta

    from app.models import utcnow

    client, token, roles, admin = users_admin
    recent = _target('recent')
    recent.last_seen = utcnow()
    stale = _target('stale')
    stale.last_seen = utcnow() - timedelta(days=90)
    db.session.commit()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/users?last_seen=7')

    listed = {user.user_name for user in render.call_args.kwargs['users']}
    assert 'recent' in listed
    assert 'stale' not in listed


def test_the_sort_button_redirects_and_carries_the_filters(users_admin):
    """`sort_by_btn` is a separate parameter so the sort control can be a
    submit button; the redirect is what turns it back into a URL the admin can
    bookmark."""
    client, token, roles, admin = users_admin

    response = client.get('/admin/users?sort_by_btn=user_name&search=bob'
                          '&local_remote=local&last_seen=7')

    assert response.status_code == 302
    location = response.headers['Location']
    assert 'sort_by=user_name' in location
    for fragment in ('search=bob', 'local_remote=local', 'last_seen=7'):
        assert fragment in location


def test_an_unknown_sort_column_is_refused(users_admin):
    """`safe_order_by(sort_by, User, {...})` -- `sort_by` comes straight from
    the query string and is interpolated into an ORDER BY."""
    client, token, roles, admin = users_admin
    _target('someone')

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.get('/admin/users?sort_by=password_hash DESC')

    assert response.status_code == 200
    assert 'someone' in {user.user_name for user in render.call_args.kwargs['users']}


# --------------------------------------------------------------------------
# D950: the resend-email endpoint
# --------------------------------------------------------------------------


def test_the_resend_endpoint_refuses_a_non_htmx_request(users_admin):
    """`if not is_htmx: abort(400)`. The response is a bare string rather than
    a page, so a browser reaching it directly would render it as the whole
    document."""
    client, token, roles, admin = users_admin
    target = _target()

    response = client.post(f'/admin/user/{target.id}/resend_email',
                           data={'csrf_token': token})

    assert response.status_code == 400


def test_the_resend_endpoint_sends_the_email(users_admin):
    client, token, roles, admin = users_admin
    target = _target()
    target.verification_token = 'existingtoken12'
    db.session.commit()

    with patch('app.admin.routes.send_email_verification') as send:
        response = client.post(f'/admin/user/{target.id}/resend_email',
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true'})

    assert send.call_args.args[0].id == target.id
    assert response.get_data(as_text=True) == 'Verification email sent!'


def test_the_resend_endpoint_creates_a_missing_verification_token(users_admin):
    """Without a token the verification link cannot be built, so an account
    created before tokens existed could never be verified by this button."""
    client, token, roles, admin = users_admin
    target = _target()
    target.verification_token = None
    db.session.commit()

    with patch('app.admin.routes.send_email_verification'):
        client.post(f'/admin/user/{target.id}/resend_email',
                    data={'csrf_token': token}, headers={'HX-Request': 'true'})

    db.session.expire_all()
    assert db.session.get(User, target.id).verification_token


def test_an_existing_verification_token_is_not_replaced(users_admin):
    """The false arm. Replacing it would invalidate a link the user may already
    have received."""
    client, token, roles, admin = users_admin
    target = _target()
    target.verification_token = 'theirtoken12345'
    db.session.commit()

    with patch('app.admin.routes.send_email_verification'):
        client.post(f'/admin/user/{target.id}/resend_email',
                    data={'csrf_token': token}, headers={'HX-Request': 'true'})

    db.session.expire_all()
    assert db.session.get(User, target.id).verification_token == 'theirtoken12345'


def test_a_mail_failure_does_not_reach_the_browser(users_admin, caplog):
    """D950's pin, inverted.

    The handler returned `_("Problem sending email: ") + str(e)`, so the mail
    exception's text went straight into the page -- and a mail failure names
    the relay, the credentials in use or the recipient's provider. It is also
    D815's shape: the catalogue was asked for a string ending in a stack of
    server detail. The text is logged instead.
    """
    client, token, roles, admin = users_admin
    target = _target()
    secret = 'SMTP AUTH failed for smtp-relay.internal as postmaster@example.com'

    # caplog, not patch('...current_app'): patching the LocalProxy replaces
    # every config lookup the route makes, and mock auto-creates
    # `logger.exception` as an AsyncMock, which pytest then reports as
    # `RuntimeWarning: coroutine ... was never awaited` -- a warning this
    # campaign counts.
    with patch('app.admin.routes.send_email_verification',
               side_effect=RuntimeError(secret)):
        response = client.post(f'/admin/user/{target.id}/resend_email',
                               data={'csrf_token': token},
                               headers={'HX-Request': 'true'})

    body = response.get_data(as_text=True)
    assert secret not in body
    assert 'server log' in body
    # The detail is not lost -- it goes to the log, where the operator can
    # read it and the browser cannot.
    assert secret in caplog.text


def test_the_resend_endpoint_is_a_404_for_an_unknown_user(users_admin):
    client, token, roles, admin = users_admin

    response = client.post('/admin/user/999999/resend_email',
                           data={'csrf_token': token},
                           headers={'HX-Request': 'true'})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# delete_user_files_in_background
# --------------------------------------------------------------------------


def test_the_file_deletion_task_removes_every_file_a_user_owns(app, db_session):
    """It reads through the `user_file` association rather than from the User,
    so a file shared with another user is still found by the join."""
    from app.admin.routes import delete_user_files_in_background
    from app.models import File, user_file

    instance = _instance()
    owner = make_user(instance, 'owner', local=True)
    db.session.commit()
    files = []
    for index in range(2):
        image = File(source_url=f'https://example.com/{index}.png',
                     file_path=f'{index}.png')
        db.session.add(image)
        files.append(image)
    db.session.commit()
    for image in files:
        db.session.execute(user_file.insert().values(user_id=owner.id,
                                                     file_id=image.id))
    db.session.commit()

    with patch('app.admin.routes.process_file_delete') as process:
        delete_user_files_in_background(owner.id)

    assert sorted(call.args[0] for call in process.call_args_list) == [
        'https://example.com/0.png', 'https://example.com/1.png']
    assert {call.args[1] for call in process.call_args_list} == {owner.id}


def test_the_file_deletion_task_leaves_another_users_files_alone(app, db_session):
    """`filter(user_file.c.user_id == user_id)` -- without it the join would
    match every file that has any owner at all."""
    from app.admin.routes import delete_user_files_in_background
    from app.models import File, user_file

    instance = _instance()
    owner = make_user(instance, 'owner', local=True)
    other = make_user(instance, 'other', local=True)
    db.session.commit()
    image = File(source_url='https://example.com/theirs.png', file_path='t.png')
    db.session.add(image)
    db.session.commit()
    db.session.execute(user_file.insert().values(user_id=other.id,
                                                 file_id=image.id))
    db.session.commit()

    with patch('app.admin.routes.process_file_delete') as process:
        delete_user_files_in_background(owner.id)

    assert process.call_args_list == []


def test_the_file_deletion_task_rolls_back_and_re_raises(app, db_session):
    from app.admin.routes import delete_user_files_in_background
    from app.models import File, user_file

    instance = _instance()
    owner = make_user(instance, 'owner', local=True)
    db.session.commit()
    image = File(source_url='https://example.com/a.png', file_path='a.png')
    db.session.add(image)
    db.session.commit()
    db.session.execute(user_file.insert().values(user_id=owner.id,
                                                 file_id=image.id))
    db.session.commit()

    with patch('app.admin.routes.process_file_delete',
               side_effect=OSError('disk gone')):
        with pytest.raises(OSError):
            delete_user_files_in_background(owner.id)


# --------------------------------------------------------------------------
# unsubscribe_from_everything_then_delete_task (app/admin/util.py)
# --------------------------------------------------------------------------


def test_deleting_a_local_user_unsubscribes_and_federates_the_deletion(
        app, db_session):
    """The whole task, for the case it exists to handle: a finalized local
    account.

    Three things have to happen and each would be invisible without the
    others -- an Undo Follow to every community the user belonged to, a Delete
    of the actor to every live instance, and only then the local row marked
    deleted. Skipping the Undo leaves the remote community sending content to
    a user that no longer exists; skipping the Delete leaves every peer holding
    a live copy of an account this instance destroyed.
    """
    from app.admin.util import unsubscribe_from_everything_then_delete_task
    from app.models import CommunityMember, Instance
    from tests.factories import make_community

    instance = _instance()
    owner = make_user(instance, 'owner', local=True)
    user = make_user(instance, 'victim', local=True)
    user.private_key = 'a private key'
    community = make_community('somewhere', host='remote.example')
    db.session.commit()
    remote = _instance('remote.example', 'lemmy')
    remote.inbox = 'https://remote.example/inbox'
    remote.dormant = False
    remote.gone_forever = False
    community.instance_id = remote.id
    community.ap_inbox_url = 'https://remote.example/c/somewhere/inbox'
    db.session.add(CommunityMember(user_id=user.id, community_id=community.id))
    db.session.commit()
    user_id = user.id

    with patch('app.admin.util.send_post_request') as send:
        with patch.object(Instance, 'online', return_value=True):
            unsubscribe_from_everything_then_delete_task(user_id)

    sent = [call.args[1]['type'] for call in send.call_args_list]
    assert 'Undo' in sent, 'the community was never told the user left'
    assert 'Delete' in sent, 'peers were never told the actor was deleted'

    db.session.expire_all()
    deleted = db.session.get(User, user_id)
    assert (deleted.deleted, deleted.banned) == (True, True)


def test_deleting_a_remote_user_does_not_federate_a_delete(app, db_session):
    """`if user.is_local():` -- this instance does not get to announce the
    deletion of somebody else's account. The Undo still goes out, because the
    membership was ours."""
    from app.admin.util import unsubscribe_from_everything_then_delete_task
    from app.models import CommunityMember, Instance
    from tests.factories import make_community

    instance = _instance()
    make_user(instance, 'owner', local=True)
    remote = _instance('remote.example', 'lemmy')
    remote.inbox = 'https://remote.example/inbox'
    user = make_user(remote, 'theirs', local=False)
    community = make_community('local_community')
    community.ap_inbox_url = 'https://test.piefed.local/c/local_community/inbox'
    db.session.commit()
    db.session.add(CommunityMember(user_id=user.id, community_id=community.id))
    db.session.commit()
    user_id = user.id

    with patch('app.admin.util.send_post_request') as send:
        with patch.object(Instance, 'online', return_value=True):
            unsubscribe_from_everything_then_delete_task(user_id)

    sent = [call.args[1]['type'] for call in send.call_args_list]
    assert 'Delete' not in sent
    assert 'Undo' in sent


def test_a_community_on_a_dead_instance_is_not_contacted(app, db_session):
    """`unsubscribe_from_community` returns early for `gone_forever`. Posting
    to an instance known to be gone costs a timeout per community, and this
    task runs one per membership."""
    from app.admin.util import unsubscribe_from_everything_then_delete_task
    from app.models import CommunityMember, Instance
    from tests.factories import make_community

    instance = _instance()
    make_user(instance, 'owner', local=True)
    user = make_user(instance, 'victim', local=True)
    user.private_key = 'a private key'
    community = make_community('gone', host='dead.example')
    db.session.commit()
    dead = _instance('dead.example', 'lemmy')
    dead.gone_forever = True
    community.instance_id = dead.id
    db.session.add(CommunityMember(user_id=user.id, community_id=community.id))
    db.session.commit()
    user_id = user.id

    with patch('app.admin.util.send_post_request') as send:
        with patch.object(Instance, 'online', return_value=False):
            unsubscribe_from_everything_then_delete_task(user_id)

    assert send.call_args_list == []


def test_the_deletion_is_not_sent_to_this_instance_or_to_dormant_peers(
        app, db_session):
    """`instance.inbox and instance.online() and instance.id != 1`. Instance 1
    is always this instance, and delivering our own Delete to ourselves would
    process it as an inbound activity."""
    from app.admin.util import unsubscribe_from_everything_then_delete_task
    from app.models import Instance

    instance = _instance()
    instance.inbox = 'https://test.piefed.local/inbox'
    make_user(instance, 'owner', local=True)
    user = make_user(instance, 'victim', local=True)
    user.private_key = 'a private key'
    offline = _instance('offline.example', 'lemmy')
    offline.inbox = 'https://offline.example/inbox'
    inboxless = _instance('inboxless.example', 'lemmy')
    inboxless.inbox = None
    db.session.commit()
    user_id = user.id

    with patch('app.admin.util.send_post_request') as send:
        with patch.object(Instance, 'online', return_value=False):
            unsubscribe_from_everything_then_delete_task(user_id)

    assert send.call_args_list == []


def test_this_instance_is_never_sent_its_own_delete(app, db_session):
    """`instance.id != 1`. Instance 1 is always this instance, so delivering
    our own Delete to our own inbox would have the activity processed as though
    a peer had sent it. The other two instances here are live and DO receive
    it, which is what makes the exclusion visible rather than vacuous."""
    from app.admin.util import unsubscribe_from_everything_then_delete_task
    from app.models import Instance

    local = _instance()
    local.inbox = 'https://test.piefed.local/inbox'
    local.dormant = False
    local.gone_forever = False
    make_user(local, 'owner', local=True)
    user = make_user(local, 'victim', local=True)
    user.private_key = 'a private key'
    peer = _instance('peer.example', 'lemmy')
    peer.inbox = 'https://peer.example/inbox'
    peer.dormant = False
    peer.gone_forever = False
    db.session.commit()
    assert local.id == 1
    user_id = user.id

    with patch('app.admin.util.send_post_request') as send:
        with patch.object(Instance, 'online', return_value=True):
            unsubscribe_from_everything_then_delete_task(user_id)

    inboxes = [call.args[0] for call in send.call_args_list]
    assert inboxes == ['https://peer.example/inbox']


def test_the_unsubscribe_task_rolls_back_and_re_raises(app, db_session):
    from app.admin.util import unsubscribe_from_everything_then_delete_task

    instance = _instance()
    make_user(instance, 'owner', local=True)
    user = make_user(instance, 'victim', local=True)
    user.private_key = 'a private key'
    db.session.commit()
    user_id = user.id

    with patch.object(User, 'delete_dependencies', side_effect=ValueError('boom')):
        with pytest.raises(ValueError):
            unsubscribe_from_everything_then_delete_task(user_id)

    db.session.expire_all()
    assert db.session.get(User, user_id).deleted is False
