"""/user/settings/sidebar: a signed-in user's own order of the sidebars' boxes and which of them they hide
(app/sidebar.py; spec 2026-11-22-user-sidebar-order).

The page offers the boxes the request's domain shows, in the user's order, or the domain's until they change one.
Each button is its own POST, as on the admin page: it moves one box up or down, hides or shows one, or goes back to
the domain's order, and the page comes back showing the result. Nothing needs JavaScript.
"""
from flask import abort, redirect, request, url_for
from flask_babel import _
from flask_login import current_user

from app import db
from app.sidebar import BOXES, FIELDS, changed, domain_shown, own_value, personal_layout, stored
from app.user import bp
from app.utils import login_required, render_template


def apply_action(values: dict, offered: dict, action: str) -> dict | None:
    """The user's values (sidebar to stored layout, None for the domain's) after the posted `action`
    ('<sidebar>:<up|down|toggle>:<box>', or 'reset:<sidebar>'), or None when the action names no button the page
    has. `offered` is each sidebar's boxes the domain shows."""
    parts = action.split(':')
    if len(parts) == 2 and parts[0] == 'reset' and parts[1] in BOXES:
        return {**values, parts[1]: None}
    if len(parts) != 3 or parts[0] not in BOXES or parts[2] not in offered[parts[0]]:
        return None
    area, op, name = parts
    boxes = changed(personal_layout(values[area], area, offered[area]), op, name)
    if boxes is None:
        return None
    return {**values, area: stored(boxes)}


def sidebars(values: dict, offered: dict) -> list[dict]:
    """Each sidebar's offered boxes in the user's order with their names and whether each is shown, and whether the
    order is the user's own."""
    return [{'area': area, 'own': values[area] is not None,
             'boxes': [(name, BOXES[area][name], shown)
                       for name, shown in personal_layout(values[area], area, offered[area])]}
            for area in BOXES]


@bp.route('/user/settings/sidebar', methods=['GET', 'POST'])
@login_required
def user_settings_sidebar():
    offered = {area: domain_shown(area) for area in BOXES}
    values = {area: own_value(current_user, area) for area in BOXES}
    if request.method == 'POST':   # login_required has checked its CSRF token
        action = request.form.get('action', '')
        result = apply_action(values, offered, action)
        if result is None:
            abort(400)
        for area, field in FIELDS.items():
            setattr(current_user, field, result[area])
        db.session.commit()
        area = next(part for part in action.split(':') if part in BOXES)
        return redirect(url_for('user.user_settings_sidebar') + f'#sidebar-{area}')
    return render_template('user/sidebar.html', title=_('Sidebar'), user=current_user,
                           sidebars=sidebars(values, offered))
