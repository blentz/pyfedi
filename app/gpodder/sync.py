"""gpodder sync's devices and subscriptions (spec §4, ruling G2). A device is made by its first call. The subscriptions
are the person's, one list every device of theirs reads and writes, as a gpodder.net sync group holding all of them. A
URL that is a community's own feed here (/community/<actor>/feed on a host this site answers) joins it, through the
shared join with its checks and federation, and its removal leaves it; a refusal of either is logged and the URL kept,
so syncing never fails for it.

Times are whole seconds. A pull answers what changed at or after `since`, and the time it began: a repeat is harmless,
a change missed is not."""
import calendar
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from flask import current_app, g, jsonify, request

from app import db
from app.community.util import actor_to_community
from app.constants import SRC_API
from app.gpodder import bp
from app.gpodder.auth import signed_in
from app.local_only import hidden_here
from app.models import Community, CommunityMember, GpodderDevice, GpodderSubscription, User, utcnow
from app.shared import community as shared_community
from app.storefront import is_local_host
from app.utils import Authorised

DEVICE_ID = re.compile(r'[\w.-]{1,64}', re.ASCII)
DEVICE_TYPES = frozenset(('desktop', 'laptop', 'mobile', 'server', 'other'))
MAX_CAPTION = 100
MAX_SUBSCRIPTIONS = 2000
MAX_URL = 2000
FEED_PATH = re.compile(r'/community/([^/]+)/feed/?')


def bad_request(message: str):
    return jsonify({'error': message}), 400


def server_time() -> int:
    return calendar.timegm(utcnow().timetuple())


def since_time(value) -> datetime | None:
    """`since` (whole seconds, gpodder's) as a time, or None if it is no such number."""
    try:
        return datetime.fromtimestamp(max(int(value), 0), timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def json_body(default):
    """The request's JSON body, `default` when it has none, or None when it is not JSON."""
    if not request.get_data():
        return default
    return request.get_json(silent=True, force=True)


def device(user: User, device_id: str) -> GpodderDevice:
    found = GpodderDevice.query.filter_by(user_id=user.id, device_id=device_id).first()
    if found is None:
        found = GpodderDevice(user_id=user.id, device_id=device_id)
        db.session.add(found)
        db.session.commit()
    return found


def valid_device_id(device_id: str) -> bool:
    return DEVICE_ID.fullmatch(device_id) is not None


@bp.route('/api/2/devices/<username>.json')
@signed_in
def list_devices(username):
    user = g.gpodder_user
    count = GpodderSubscription.query.filter_by(user_id=user.id, subscribed=True).count()
    devices = GpodderDevice.query.filter_by(user_id=user.id).order_by(GpodderDevice.id)
    return jsonify([{'id': found.device_id, 'caption': found.caption, 'type': found.type, 'subscriptions': count}
                    for found in devices])


@bp.route('/api/2/devices/<username>/<device_id>.json', methods=['POST'])
@signed_in
def configure_device(username, device_id):
    body = json_body({})
    if not valid_device_id(device_id) or not isinstance(body, dict):
        return bad_request('A device id is 1 to 64 letters, digits, dots, dashes or underscores, its body an object')
    caption, kind = body.get('caption'), body.get('type')
    if 'caption' in body and (not isinstance(caption, str) or len(caption) > MAX_CAPTION):
        return bad_request(f'A caption is text of up to {MAX_CAPTION} characters')
    if 'type' in body and kind not in DEVICE_TYPES:
        return bad_request(f"A device's type is one of {', '.join(sorted(DEVICE_TYPES))}")
    found = device(g.gpodder_user, device_id)
    if 'caption' in body:
        found.caption = caption
    if 'type' in body:
        found.type = kind
    db.session.commit()
    return jsonify({})


def _cleaned(url: str) -> str | None:
    """A URL as gpodder.net keeps it: trimmed, and only http(s)."""
    url = url.strip()
    return url if url.startswith(('https://', 'http://')) else None


def own_community(url: str) -> Community | None:
    """The community whose own feed `url` is here, if the person may open it."""
    parts = urlsplit(url)
    match = FEED_PATH.fullmatch(parts.path)
    if match is None or not is_local_host(parts.hostname):
        return None
    community = actor_to_community(unquote(match.group(1)))
    if community is None or community.private or hidden_here(community, True):
        return None
    return community


def _is_member(user: User, community: Community) -> bool:
    return CommunityMember.query.filter_by(user_id=user.id, community_id=community.id).first() is not None


def _follow(user: User, url: str, join: bool) -> None:
    """Ruling G2: join or leave the community `url` is the feed of, if it is one; a refusal is logged, not answered."""
    community = own_community(url)
    if community is None or _is_member(user, community) == join:
        return
    try:
        if join:
            shared_community.join_community(community.id, SRC_API, Authorised(user))
        else:
            shared_community.leave_community(community.id, SRC_API, Authorised(user))
    except Exception as refusal:   # a ban from it, or a moderator who must step down first: the list keeps the URL
        current_app.logger.info('gpodder: %s of community %s refused for user %s: %s',
                                'join' if join else 'leave', community.id, user.id, refusal)


def _urls(body: dict, key: str) -> list | None:
    value = body.get(key, [])
    if not isinstance(value, list) or not all(isinstance(url, str) and len(url) <= MAX_URL for url in value):
        return None
    return value


@bp.route('/api/2/subscriptions/<username>/<device_id>.json', methods=['POST'])
@signed_in
def upload_subscriptions(username, device_id):
    body = json_body(None)
    added = _urls(body, 'add') if isinstance(body, dict) else None
    removed = _urls(body, 'remove') if isinstance(body, dict) else None
    if not valid_device_id(device_id) or added is None or removed is None:
        return bad_request(f'add and remove are lists of URLs of up to {MAX_URL} characters')
    update_urls, add, remove = [], [], []
    for urls, kept in ((added, add), (removed, remove)):
        for url in urls:
            cleaned = _cleaned(url)
            if cleaned != url:
                update_urls.append([url, cleaned or ''])
            if cleaned:
                kept.append(cleaned)
    if set(add) & set(remove):
        return bad_request('A URL is both added and removed')
    user = g.gpodder_user
    rows = {row.url: row for row in GpodderSubscription.query.filter(GpodderSubscription.user_id == user.id,
                                                                     GpodderSubscription.url.in_(add + remove))}
    held = GpodderSubscription.query.filter_by(user_id=user.id, subscribed=True).count()
    new = {url for url in add if url not in rows or not rows[url].subscribed}
    if held + len(new) > MAX_SUBSCRIPTIONS:
        return bad_request(f'A person keeps at most {MAX_SUBSCRIPTIONS} subscriptions')
    device(user, device_id)
    timestamp, now = server_time(), utcnow()
    for url in new:
        if url in rows:
            rows[url].subscribed, rows[url].changed_at = True, now
        else:
            db.session.add(GpodderSubscription(user_id=user.id, url=url, subscribed=True, changed_at=now))
    for url in remove:
        if url in rows and rows[url].subscribed:
            rows[url].subscribed, rows[url].changed_at = False, now
    db.session.commit()
    for url in add:
        _follow(user, url, join=True)
    for url in remove:
        _follow(user, url, join=False)
    return jsonify({'timestamp': timestamp, 'update_urls': update_urls})


@bp.route('/api/2/subscriptions/<username>/<device_id>.json')
@signed_in
def pull_subscriptions(username, device_id):
    since = since_time(request.args.get('since', '0'))
    if not valid_device_id(device_id) or since is None:
        return bad_request('since is a time in whole seconds')
    user = g.gpodder_user
    device(user, device_id)
    timestamp = server_time()
    rows = GpodderSubscription.query.filter(GpodderSubscription.user_id == user.id,
                                            GpodderSubscription.changed_at >= since).order_by(GpodderSubscription.id)
    add, remove = [], []
    for row in rows:
        (add if row.subscribed else remove).append(row.url)
    return jsonify({'add': add, 'remove': remove, 'timestamp': timestamp})
