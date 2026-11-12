"""gpodder sync's episode actions (spec §5, ruling G3). Only the newest of each action on each episode is kept, by the
time the app says it was taken (an older one sent late changes nothing), and at most MAX_KEPT per person, the oldest
received going first. A pull answers what was received at or after `since`. A play of an episode that is a post here
(its guid our feed's <guid>, the post's ActivityPub id, or its URL the post's media) marks the post read for the
person, if they may read it."""
import re
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlsplit

from flask import g, jsonify, request
from sqlalchemy import select

from app import db
from app.gpodder import bp
from app.gpodder.auth import signed_in
from app.gpodder.sync import MAX_URL, bad_request, device, json_body, server_time, since_time, valid_device_id
from app.mastodon.timelines import readable
from app.models import GpodderEpisodeAction, Post, User, utcnow
from app.shared.post import mark_post_read
from app.storefront import canonical_link, is_local_host

ACTIONS = frozenset(('download', 'delete', 'play', 'new'))
DROPPED = frozenset(('flattr',))   # gpodder.net's, which nothing here pays
MAX_ACTIONS = 1000   # in one upload
MAX_KEPT = 20000     # per person
TIME_FORMAT = '%Y-%m-%dT%H:%M:%S'
LOCAL_POST = re.compile(r'/post/(\d+)/?')


class Refused(ValueError):
    pass


def _url(value, name: str) -> tuple[str, str]:
    """`value` as given, and as kept: trimmed, http(s)."""
    if not isinstance(value, str) or len(value) > MAX_URL or not value.strip().startswith(('https://', 'http://')):
        raise Refused(f'{name} is an http(s) URL of up to {MAX_URL} characters')
    return value, value.strip()


def _count(item: dict, name: str) -> int | None:
    value = item.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Refused(f'{name} is a whole number of seconds')
    return value


def _time(value) -> datetime:
    if value is None:
        return utcnow()
    try:
        moment = datetime.fromisoformat(value.removesuffix('Z')) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None:
        raise Refused(f'timestamp is a time as {TIME_FORMAT}')
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def _parsed(item, update_urls: list) -> dict | None:
    """One action as kept, None for one dropped, or Refused."""
    if not isinstance(item, dict):
        raise Refused('Each action is an object')
    action = item.get('action')
    if isinstance(action, str) and action.lower() in DROPPED:
        return None
    if not isinstance(action, str) or action.lower() not in ACTIONS:
        raise Refused(f"action is one of {', '.join(sorted(ACTIONS))}")
    parsed = {'action': action.lower()}
    for name in ('podcast', 'episode'):
        given, kept = _url(item.get(name), name)
        if given != kept:
            update_urls.append([given, kept])
        parsed[name] = kept
    guid, device_id = item.get('guid'), item.get('device')
    if guid is not None and (not isinstance(guid, str) or len(guid) > MAX_URL):
        raise Refused(f'guid is text of up to {MAX_URL} characters')
    if device_id is not None and (not isinstance(device_id, str) or not valid_device_id(device_id)):
        raise Refused('device is a device id')
    parsed.update(guid=guid, device=device_id, timestamp=_time(item.get('timestamp')),
                  **{name: _count(item, name) for name in ('started', 'position', 'total')})
    return parsed


def _digest(url: str) -> str:
    return sha256(url.encode()).hexdigest()


def _keep(user: User, parsed: dict, now: datetime) -> None:
    """The action, unless one of its kind on its episode is newer."""
    digest = _digest(parsed['episode'])
    row = GpodderEpisodeAction.query.filter_by(user_id=user.id, episode_digest=digest,
                                               action=parsed['action']).first()
    if row is None:
        row = GpodderEpisodeAction(user_id=user.id, episode_digest=digest)
        db.session.add(row)
    elif row.timestamp > parsed['timestamp']:
        return
    for name, value in parsed.items():
        setattr(row, name, value)
    row.received_at = now


def _prune(user: User) -> None:
    stale = select(GpodderEpisodeAction.id).where(GpodderEpisodeAction.user_id == user.id) \
        .order_by(GpodderEpisodeAction.received_at.desc(), GpodderEpisodeAction.id.desc()).offset(MAX_KEPT)
    GpodderEpisodeAction.query.filter(GpodderEpisodeAction.id.in_(stale)).delete(synchronize_session=False)


def _post(parsed: dict) -> Post | None:
    """The post the episode is here: by its guid (our feed's <guid>, a post's ActivityPub id, or a local post's
    address), else by its media URL as stored."""
    guid = parsed['guid']
    if guid:
        found = Post.query.filter(Post.ap_id == guid).first()
        if found is not None:
            return found
        parts = urlsplit(guid)
        match = LOCAL_POST.fullmatch(parts.path) if is_local_host(parts.hostname) else None
        if match is not None:
            return db.session.get(Post, int(match.group(1)))
    return Post.query.filter(Post.url.in_({parsed['episode'], canonical_link(parsed['episode'])})).first()


def _mark_read(user: User, parsed: dict) -> None:
    post = _post(parsed) if parsed['action'] == 'play' else None
    if post is not None and readable(post, user.id):
        mark_post_read([post.id], True, user.id)


@bp.route('/api/2/episodes/<username>.json', methods=['POST'])
@signed_in
def upload_episode_actions(username):
    items, update_urls = json_body(None), []
    if not isinstance(items, list) or len(items) > MAX_ACTIONS:
        return bad_request(f'The body is a list of up to {MAX_ACTIONS} actions')
    try:
        actions = [parsed for parsed in (_parsed(item, update_urls) for item in items) if parsed is not None]
    except Refused as refusal:
        return bad_request(str(refusal))
    user, timestamp, now = g.gpodder_user, server_time(), utcnow()
    for device_id in {parsed['device'] for parsed in actions if parsed['device']}:
        device(user, device_id)
    for parsed in actions:
        _keep(user, parsed, now)
    db.session.flush()
    _prune(user)
    db.session.commit()
    for parsed in actions:
        _mark_read(user, parsed)
    return jsonify({'timestamp': timestamp, 'update_urls': update_urls})


def _answer(row: GpodderEpisodeAction) -> dict:
    answer = {'podcast': row.podcast, 'episode': row.episode, 'action': row.action,
              'timestamp': row.timestamp.strftime(TIME_FORMAT)}
    for name in ('guid', 'device', 'started', 'position', 'total'):
        if getattr(row, name) is not None:
            answer[name] = getattr(row, name)
    return answer


@bp.route('/api/2/episodes/<username>.json')
@signed_in
def pull_episode_actions(username):
    since = since_time(request.args.get('since', '0'))
    if since is None:
        return bad_request('since is a time in whole seconds')
    timestamp = server_time()
    rows = GpodderEpisodeAction.query.filter(GpodderEpisodeAction.user_id == g.gpodder_user.id,
                                             GpodderEpisodeAction.received_at >= since)
    if request.args.get('podcast'):
        rows = rows.filter(GpodderEpisodeAction.podcast == request.args['podcast'].strip())
    if request.args.get('device'):
        rows = rows.filter(GpodderEpisodeAction.device == request.args['device'])
    rows = rows.order_by(GpodderEpisodeAction.timestamp, GpodderEpisodeAction.id).all()
    if request.args.get('aggregated', '').lower() == 'true':
        newest = {}
        for row in rows:   # oldest first, so the newest of each episode is left
            newest[row.episode_digest] = row
        rows = sorted(newest.values(), key=lambda row: (row.timestamp, row.id))
    return jsonify({'actions': [_answer(row) for row in rows], 'timestamp': timestamp})
