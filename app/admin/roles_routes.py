"""/admin/roles: making, changing and deleting roles, and granting them (spec 2026-11-03-rbac-design.md, Admin UI).

Every page is Admin's alone (RB5) and every change is a POST with CSRF. The rules themselves are in app/roles.py;
these routes turn forms into its calls and its refusals into form errors.
"""
from datetime import datetime, time

from flask import abort, flash, redirect, render_template, request, url_for
from flask_babel import _
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy import func

from app import db
from app.admin import bp
from app.admin.forms import ExpiryForm, GrantForm, RoleForm
from app.local_names import local_clause
from app.models import Role, RoleGrant, User
from app.permissions import CATALOGUE, INSTANCE_ONLY_PERMISSIONS
from app.roles import (RoleError, change_expiry, create_role, delete_role, grant_role, holder_ids, revoke_grant,
                       scope_name, update_role)
from app.utils import admin_required, safe_redirect_target


def catalogue_groups():
    """The catalogue by area, in its own order: [(group, [Permission, ...]), ...]."""
    groups = {}
    for permission in CATALOGUE:
        groups.setdefault(permission.group, []).append(permission)
    return list(groups.items())


def permissions_of(role: Role) -> list[str]:
    return sorted(p.permission for p in role.permissions)


def role_rows():
    """The list page's rows: Admin first, then every role by name, with what the list shows of each."""
    holders = dict(db.session.execute(db.select(RoleGrant.role_id, func.count(func.distinct(RoleGrant.user_id)))
                                      .group_by(RoleGrant.role_id)).all())
    rows = []
    for role in Role.query.order_by(Role.built_in.desc(), Role.name).all():
        held = permissions_of(role)
        rows.append(dict(role=role, permission_count=len(held), holders=holders.get(role.id, 0),
                         scopable=not role.built_in and not any(p in INSTANCE_ONLY_PERMISSIONS for p in held)))
    return rows


def grant_rows(grants):
    """Grants as the panels show them, oldest first, each with the forms that change it."""
    users = {u.id: u for u in User.query.filter(User.id.in_({g.user_id for g in grants} |
                                                            {g.granted_by for g in grants if g.granted_by}))}
    roles = {r.id: r for r in Role.query.filter(Role.id.in_({g.role_id for g in grants}))}
    return [dict(grant=grant, user=users.get(grant.user_id), role=roles.get(grant.role_id),
                 granted_by=users.get(grant.granted_by), scope=scope_name(grant.site_domain_id),
                 expiry_form=ExpiryForm(prefix=f'grant{grant.id}',
                                        expires=grant.expires_at.date() if grant.expires_at else None))
            for grant in sorted(grants, key=lambda g: g.id)]


def user_grants_panel(user: User) -> dict:
    """What the Roles panel of /admin/user/<id>/edit needs. Empty for anyone but an admin, whose alone grants are
    (RB5), and for a remote account, which holds none."""
    if not current_user.is_admin() or not user.is_local():
        return dict(grants=None)
    return dict(grants=grant_rows(RoleGrant.query.filter_by(user_id=user.id).all()),
                grant_form=GrantForm(prefix='grant', formdata=None), revoke_form=FlaskForm(formdata=None))


def expiry_of(day) -> datetime | None:
    """A date picked in a form, as the naive UTC moment that day starts."""
    return datetime.combine(day, time()) if day else None


def role_or_404(role_id: int) -> Role:
    return db.session.get(Role, role_id) or abort(404)


def back_to(default: str) -> str:
    return safe_redirect_target(request.form.get('next') or request.args.get('next'), default)


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

@bp.route('/permissions', methods=['GET'])
@login_required
@admin_required
def admin_permissions():
    """The old single-table page; roles are edited one at a time now."""
    return redirect(url_for('admin.admin_roles'))


@bp.route('/roles', methods=['GET'])
@login_required
@admin_required
def admin_roles():
    return render_template('admin/roles.html', title=_('Roles'), rows=role_rows())


@bp.route('/roles/<int:role_id>', methods=['GET'])
@login_required
@admin_required
def admin_role(role_id):
    role = role_or_404(role_id)
    held = set(permissions_of(role))
    grant_form = GrantForm(prefix='grant')
    return render_template('admin/role.html', title=role.name, role=role,
                           permissions=[p for p in CATALOGUE if role.built_in or p.name in held],
                           grants=grant_rows(RoleGrant.query.filter_by(role_id=role.id).all()),
                           grant_form=grant_form, revoke_form=FlaskForm())


def _role_form_page(form, role, chosen, title, confirm=None):
    return render_template('admin/role_form.html', title=title, form=form, role=role, chosen=set(chosen),
                           groups=catalogue_groups(), instance_only=INSTANCE_ONLY_PERMISSIONS, confirm=confirm)


@bp.route('/roles/new', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_role_new():
    form = RoleForm()
    chosen = request.form.getlist('permissions')
    if request.method == 'GET':
        source = db.session.get(Role, request.args.get('from', type=int) or 0)
        if source is not None:   # Duplicate: Admin's copy holds every permission, as Admin does
            form.name.data = _('Copy of %(name)s', name=source.name)[:50]
            form.description.data = source.description
            form.listed_publicly.data = source.listed_publicly
            chosen = [p.name for p in CATALOGUE] if source.built_in else permissions_of(source)
    elif form.validate_on_submit():
        try:
            role = create_role(current_user, form.name.data, form.description.data, form.listed_publicly.data,
                               chosen)
        except RoleError as refused:
            form.name.errors.append(str(refused))
        else:
            flash(_('Role created'))
            return redirect(url_for('admin.admin_role', role_id=role.id))
    return _role_form_page(form, None, chosen, _('New role'))


@bp.route('/roles/<int:role_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_role_edit(role_id):
    role = role_or_404(role_id)
    if role.built_in:
        flash(_('The built-in Admin role holds every permission and cannot be edited.'), 'error')
        return redirect(url_for('admin.admin_role', role_id=role.id))
    form = RoleForm(obj=role) if request.method == 'GET' else RoleForm()
    chosen = permissions_of(role) if request.method == 'GET' else request.form.getlist('permissions')
    if form.validate_on_submit():
        holders = len(holder_ids(role))
        changes_access = set(chosen) != set(permissions_of(role))
        if holders and changes_access and not request.form.get('confirmed'):
            # A second look before access changes for people (spec, Admin UI: Edit)
            return _role_form_page(form, role, chosen, _('Edit role'), confirm=holders)
        try:
            update_role(current_user, role, form.name.data, form.description.data, form.listed_publicly.data, chosen)
        except RoleError as refused:
            db.session.rollback()
            form.name.errors.append(str(refused))
        else:
            flash(_('Role saved'))
            return redirect(url_for('admin.admin_role', role_id=role.id))
    return _role_form_page(form, role, chosen, _('Edit role'))


@bp.route('/roles/<int:role_id>/delete', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_role_delete(role_id):
    role = role_or_404(role_id)
    if role.built_in:
        flash(_('The built-in Admin role cannot be deleted.'), 'error')
        return redirect(url_for('admin.admin_role', role_id=role.id))
    form = FlaskForm()
    grants = RoleGrant.query.filter_by(role_id=role.id).all()
    if form.validate_on_submit():
        try:
            delete_role(current_user, role, revoke=bool(request.form.get('revoke_all')))
        except RoleError as refused:
            flash(str(refused), 'error')
        else:
            flash(_('Role deleted'))
            return redirect(url_for('admin.admin_roles'))
    return render_template('admin/role_delete.html', title=_('Delete role'), role=role, form=form,
                           grants=grant_rows(grants))


# --------------------------------------------------------------------------
# Grants
# --------------------------------------------------------------------------

def _add_grant(user, role, form, default):
    try:
        grant_role(current_user, user, role, site_domain_id=form.scope.data or None,
                   expires_at=expiry_of(form.expires.data))
    except RoleError as refused:
        flash(str(refused), 'error')
    else:
        flash(_('Granted'))
    return redirect(back_to(default))


@bp.route('/roles/<int:role_id>/grants', methods=['POST'])
@login_required
@admin_required
def admin_role_grant(role_id):
    role = role_or_404(role_id)
    form = GrantForm(prefix='grant')
    default = url_for('admin.admin_role', role_id=role.id)
    if not form.validate_on_submit():
        flash(_('The grant could not be read: %(errors)s', errors=form.errors), 'error')
        return redirect(default)
    name = (form.user.data or '').strip()
    user = User.query.filter(local_clause(User, name), User.deleted.is_(False)).first() if name else None
    if user is None:
        flash(_('There is no local account called "%(name)s".', name=name), 'error')
        return redirect(default)
    return _add_grant(user, role, form, default)


@bp.route('/user/<int:user_id>/grants', methods=['POST'])
@login_required
@admin_required
def admin_user_grant(user_id):
    user = db.session.get(User, user_id) or abort(404)
    form = GrantForm(prefix='grant')
    default = url_for('admin.admin_user_edit', user_id=user.id)
    role = db.session.get(Role, form.role.data or 0) if form.validate_on_submit() else None
    if role is None:
        flash(_('Choose a role to grant.'), 'error')
        return redirect(default)
    return _add_grant(user, role, form, default)


def grant_or_404(grant_id: int) -> RoleGrant:
    return db.session.get(RoleGrant, grant_id) or abort(404)


@bp.route('/grants/<int:grant_id>/expiry', methods=['POST'])
@login_required
@admin_required
def admin_grant_expiry(grant_id):
    grant = grant_or_404(grant_id)
    form = ExpiryForm(prefix=f'grant{grant.id}')
    default = url_for('admin.admin_role', role_id=grant.role_id)
    if form.validate_on_submit():
        try:
            change_expiry(current_user, grant, expiry_of(form.expires.data))
        except RoleError as refused:
            flash(str(refused), 'error')
        else:
            flash(_('Expiry saved'))
    else:
        flash(_('The expiry could not be read.'), 'error')
    return redirect(back_to(default))


@bp.route('/grants/<int:grant_id>/revoke', methods=['POST'])
@login_required
@admin_required
def admin_grant_revoke(grant_id):
    grant = grant_or_404(grant_id)
    default = url_for('admin.admin_role', role_id=grant.role_id)
    if FlaskForm().validate_on_submit():
        try:
            revoke_grant(current_user, grant)
        except RoleError as refused:
            flash(str(refused), 'error')
        else:
            flash(_('Revoked'))
    return redirect(back_to(default))
