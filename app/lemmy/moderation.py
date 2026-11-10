"""A moderator's and an admin's counts in Lemmy's API (spec 2026-11-10-lemmy-v3-design §6, ruling L2): moderating
stays on the web or in a PieFed app for now (W3b), and every moderation endpoint is not_found. Jerboa asks these two
counts on every start for a person who moderates or administers, and shows an error for one that fails, so they
answer zero."""
from flask import jsonify, request

from app.lemmy import bp
from app.utils import authorise_api_user


@bp.route('/api/v3/user/report_count')   # where Lemmy 0.19 serves it
def report_count():
    authorise_api_user(request.headers.get('Authorization'))
    return jsonify(community_id=request.args.get('community_id', type=int), comment_reports=0, post_reports=0,
                   private_message_reports=0)


@bp.route('/api/v3/admin/registration_application/count')
def registration_application_count():
    authorise_api_user(request.headers.get('Authorization'))
    return jsonify(registration_applications=0)
