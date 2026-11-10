"""The web links NewPipe shares for a PeerTube video (spec 2026-11-09-peertube-read-api-design §3.4): /videos/watch/<id>
and /w/<id>, each sent on to the post's own page when it is a video a signed-out reader may open here."""
from flask import jsonify, redirect

from app.peertube import bp
from app.peertube.entities import find_video, not_found


def _to_post(video_id: str):
    post = find_video(video_id)
    return redirect(f'/post/{post.id}') if post is not None else not_found()


@bp.route('/videos/watch/<video_id>')
def watch_link(video_id):
    return _to_post(video_id)


@bp.route('/w/<video_id>')
def short_link(video_id):
    return _to_post(video_id)


@bp.route('/client/locales/<locale>/server.json')
def server_translations(locale):
    """PeerTube's server-side translations, which its app reads before it shows a video: PieFed's category, licence and
    privacy labels are English, so there is nothing to translate in any locale."""
    return jsonify({})
