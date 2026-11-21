"""/admin/sidebar: the order of the picked domain's sidebar boxes and which are hidden (app/sidebar.py; spec
2026-11-20-multitenant-domain-config MT4). /admin/domains/<id>/sidebar opens it with that domain picked.

The first domain's order is its configuration row's, under `change instance settings`. A sister domain's is its own
row's, under `edit domain branding` on that domain; until it sets one it shows the first domain's, and its first change
starts from the first domain's. Each button is its own POST: it moves one box up or down, hides or shows one, or (on a domain) goes back to
the site's order, and the page comes back showing the result. Nothing needs JavaScript.
"""
from flask import abort, redirect, request, url_for
from flask_babel import _

from app import db
from app.admin import bp
from app.admin.tenant import may_edit_branding, tenant_for
from app.domain_config import own_config
from app.models import SiteDomain, first_config
from app.sidebar import BOXES, FIELDS, layout, moved, stored, toggled
from app.utils import login_required, render_template

STEPS = {'up': -1, 'down': 1}


def changed(boxes, op: str, name: str):
    """`boxes` after the button `op` on box `name`, or None for a button no box has."""
    if op in STEPS:
        return moved(boxes, name, STEPS[op])
    if op == 'toggle':
        return toggled(boxes, name)
    return None


def apply_action(values: dict, inherited: dict, action: str) -> dict | None:
    """The stored values (sidebar to stored layout, None for the inherited one) after the posted `action`
    ('<sidebar>:<up|down|toggle>:<box>', or 'reset:<sidebar>'), or None when the action names no button there is.
    `inherited` is what a sidebar with no value of its own shows."""
    parts = action.split(':')
    if len(parts) == 2 and parts[0] == 'reset' and parts[1] in BOXES:
        return {**values, parts[1]: None}
    if len(parts) != 3 or parts[0] not in BOXES or parts[2] not in BOXES[parts[0]]:
        return None
    area, op, name = parts
    current = values[area] if values[area] is not None else inherited[area]
    boxes = changed(layout(current, area), op, name)
    if boxes is None:
        return None
    return {**values, area: stored(boxes)}


def sidebars(values: dict, inherited: dict) -> list[dict]:
    """What the page lists for each sidebar: its boxes in order with their names and whether each is shown, and
    whether the value is its own."""
    return [{'area': area, 'own': values[area] is not None,
             'boxes': [(name, BOXES[area][name], shown)
                       for name, shown in layout(values[area] if values[area] is not None else inherited[area], area)]}
            for area in BOXES]


def sidebar_page(values: dict, inherited: dict, save, **context):
    """The page for one domain's sidebars, and the POST of one of its buttons."""
    if request.method == 'POST':   # login_required has checked its CSRF token
        result = apply_action(values, inherited, request.form.get('action', ''))
        if result is None:
            abort(400)
        save(result)
        db.session.commit()   # the commit clears each domain's memoised configuration (app.domain_config)
        area = next(part for part in request.form['action'].split(':') if part in BOXES)
        return redirect(url_for('admin.admin_sidebar', domain=context['tenant'].id) + f'#sidebar-{area}')
    return render_template('admin/sidebar.html', title=_('Sidebar boxes'),
                           sidebars=sidebars(values, inherited), **context)


def site_values(config) -> dict:
    return {area: getattr(config, field) or '' for area, field in FIELDS.items()}


@bp.route('/sidebar', methods=['GET', 'POST'])
@login_required
def admin_sidebar():
    """The picked domain's sidebars. A sister's sidebar with no order of its own shows the first domain's."""
    tenant = tenant_for(may_edit_branding)
    if tenant is None:
        return redirect(url_for('auth.permission_denied'))
    first = site_values(first_config())
    if tenant.is_first:
        def save(result):
            for area, field in FIELDS.items():
                setattr(first_config(), field, result[area] or '')
        return sidebar_page(first, first, save, tenant=tenant, row=None)
    row = db.session.get(SiteDomain, tenant.domain.id)
    branding = own_config(row.id)
    values = {area: (getattr(branding, field) or None) if branding is not None else None
              for area, field in FIELDS.items()}

    def save(result):
        own = branding if branding is not None else row.config()
        for area, field in FIELDS.items():
            setattr(own, field, result[area])
    return sidebar_page(values, first, save, tenant=tenant, row=row)


@bp.route('/domains/<int:domain_id>/sidebar', methods=['GET', 'POST'])
@login_required
def admin_domain_sidebar(domain_id):
    """A domain's sidebars are on the Sidebar page with the domain picked (MT4)."""
    row = db.session.get(SiteDomain, domain_id) or abort(404)
    return redirect(url_for('admin.admin_sidebar', domain=row.id))
