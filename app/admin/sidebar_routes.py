"""/admin/sidebar and /admin/domains/<id>/sidebar: the order of the sidebars' boxes and which are hidden (app/sidebar.py).

The site's order is Site row 1's, under `change instance settings`. A sister domain's is its `DomainBranding` row's,
under `edit domain branding` on that domain; until it sets one it shows the site's, and its first change starts from
the site's. Each button is its own POST: it moves one box up or down, hides or shows one, or (on a domain) goes back to
the site's order, and the page comes back showing the result. Nothing needs JavaScript.
"""
from flask import abort, redirect, request, url_for
from flask_babel import _
from flask_login import current_user

from app import db
from app.admin import bp
from app.models import DomainBranding, Site, SiteDomain
from app.sidebar import BOXES, FIELDS, layout, moved, stored, toggled
from app.storefront import clear_domain_cache
from app.utils import login_required, permission_required, render_template, user_access

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
        db.session.commit()
        clear_domain_cache()   # branding is memoised per domain (app/branding.py)
        area = next(part for part in request.form['action'].split(':') if part in BOXES)
        return redirect(f'{request.path}#sidebar-{area}')
    return render_template('admin/sidebar.html', title=_('Sidebar boxes'),
                           sidebars=sidebars(values, inherited), **context)


def site_values(site: Site) -> dict:
    return {area: getattr(site, field) or '' for area, field in FIELDS.items()}


@bp.route('/sidebar', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_sidebar():
    site = db.session.get(Site, 1) or abort(404)

    def save(result):
        for area, field in FIELDS.items():
            setattr(site, field, result[area] or '')
    values = site_values(site)
    return sidebar_page(values, values, save, row=None)


@bp.route('/domains/<int:domain_id>/sidebar', methods=['GET', 'POST'])
@login_required
def admin_domain_sidebar(domain_id):
    row = db.session.get(SiteDomain, domain_id) or abort(404)
    if not user_access('edit domain branding', current_user.get_id(), on=row):
        abort(403)
    if row.is_default:
        return redirect(url_for('admin.admin_sidebar'))
    branding = db.session.get(DomainBranding, row.id)
    values = {area: (getattr(branding, field) or None) if branding is not None else None
              for area, field in FIELDS.items()}

    def save(result):
        own = branding
        if own is None:
            own = DomainBranding(site_domain_id=row.id)
            db.session.add(own)
        for area, field in FIELDS.items():
            setattr(own, field, result[area])
    return sidebar_page(values, site_values(db.session.get(Site, 1)), save, row=row)
