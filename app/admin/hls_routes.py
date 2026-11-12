"""/admin/video_hls: whether videos are offered as HLS too (owner rulings H4, HL1-HL3), and the two jobs for the
videos uploaded before or after a change: making their HLS copies, and deleting every HLS copy.

Off by default. Turning it on makes HLS copies for videos uploaded from then on; the backfill button makes them for the
rest. Turning it off stops offering them at once and keeps the files; the delete button removes them all."""
from flask import flash, redirect, request, url_for
from flask_babel import _
from sqlalchemy import text

from app import db
from app.admin import bp
from app.models import HLS_SETTING
from app.shared.tasks import task_selector
from app.utils import get_setting, login_required, permission_required, render_template, set_setting

ACTIONS = ('save', 'backfill', 'delete')


def hls_usage() -> tuple[int, int]:
    """How many videos have HLS copies recorded, and how many bytes their packaged MP4s take."""
    row = db.session.execute(text(
        "SELECT count(DISTINCT post.id), coalesce(sum((variant->>'bytes')::bigint), 0) FROM post "
        "CROSS JOIN LATERAL json_array_elements(CASE WHEN json_typeof(post.extensions->'media'->'hls') = 'array' "
        "THEN post.extensions->'media'->'hls' ELSE '[]'::json END) AS variant")).one()
    return int(row[0]), int(row[1])


@bp.route('/video_hls', methods=['GET', 'POST'])
@login_required
@permission_required('change instance settings')
def admin_video_hls():
    if request.method == 'POST':   # login_required has checked its CSRF token
        action = request.form.get('action', '')
        if action not in ACTIONS:
            return _('Unknown action'), 400
        if action == 'save':
            set_setting(HLS_SETTING, request.form.get('enabled') == 'on')
            flash(_('Saved.'))
        elif action == 'backfill':
            if not get_setting(HLS_SETTING, False):
                flash(_('Turn HLS on first: the copies are made only while it is on.'), 'error')
            else:
                task_selector('backfill_hls')
                flash(_('HLS copies are being made for the videos that have none, in the background.'))
        else:
            task_selector('delete_all_hls')
            flash(_('Every HLS copy is being deleted, in the background.'))
        return redirect(url_for('admin.admin_video_hls'))
    videos, size = hls_usage()
    return render_template('admin/video_hls.html', title=_('Video streaming (HLS)'),
                           enabled=bool(get_setting(HLS_SETTING, False)), videos=videos, size=size)
