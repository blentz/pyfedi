"""A person's relations to others from a Mastodon app (plan W1.3): follow, unfollow, block and unblock an account,
the Relationship between the viewer and others, and the requests to follow the viewer, accepted or rejected. Each runs
through the shared function the web and the alpha API use (app.shared.user), as the token's person.

A mute (owner ruling 2026-11-08) takes the account out of the viewer's timelines, and with `notifications` (Mastodon's
default) out of their notifications, for `duration` seconds (0: until unmuted). A follow of a local account that
approves its followers, or of a remote one until its server accepts, is `requested`."""
from flask import g, jsonify, request

from app import db
from app.constants import SRC_API
from app.local_only import found_here
from app.mastodon import bp
from app.mastodon.errors import failure
from app.mastodon.statuses import Page
from app.models import User, UserBlock, UserFollower, UserMute, utcnow
from app.oauth_server.protect import require_oauth
from app.shared import user as shared_user
from app.utils import Authorised

_PG_INT_MAX = 2 ** 31 - 1


def _not_found():
    return jsonify({'error': 'Record not found'}), 404


def _person(account_id: int) -> User | None:
    if account_id > _PG_INT_MAX:
        return None
    person = found_here(User, account_id)   # nobody's, or hidden here: one answer
    return person if person is not None and not person.deleted else None


def _outward(follower_id: int, followed_id: int):
    """The follow `follower_id` (a local account) made of `followed_id`, or None."""
    return UserFollower.query.filter(UserFollower.local_user_id == follower_id,
                                     UserFollower.remote_user_id == followed_id,
                                     UserFollower.is_inward == False).first()


def _inward(user_id: int, follower_id: int):
    """A remote account's follow of the local `user_id`, as the inbox recorded it, or None."""
    return UserFollower.query.filter(UserFollower.local_user_id == user_id,
                                     UserFollower.remote_user_id == follower_id,
                                     UserFollower.is_inward == True).first()


def _blocks(blocker_id: int, blocked_id: int) -> bool:
    return db.session.query(UserBlock.id).filter_by(blocker_id=blocker_id, blocked_id=blocked_id).first() is not None


def _mute(muter_id: int, muted_id: int):
    """The mute in force, or None (an expired one counts no more)."""
    return UserMute.query.filter(UserMute.muter_id == muter_id, UserMute.muted_id == muted_id).filter(
        (UserMute.expires_at == None) | (UserMute.expires_at > utcnow())).first()


def relationship(me: User, other_id: int) -> dict:
    mine = _outward(me.id, other_id)
    mute = _mute(me.id, other_id)
    theirs = _outward(other_id, me.id) or _inward(me.id, other_id)   # theirs is a row of its own when they are local
    return {'id': str(other_id), 'following': bool(mine and mine.is_accepted is True), 'showing_reblogs': True,
            'notifying': False, 'languages': None,
            'followed_by': bool(theirs and theirs.is_accepted is True),
            'blocking': _blocks(me.id, other_id), 'blocked_by': _blocks(other_id, me.id),
            'muting': mute is not None, 'muting_notifications': bool(mute and mute.notifications),
            'requested': bool(mine and mine.is_accepted is None),
            'requested_by': bool(theirs and theirs.is_accepted is None),
            'domain_blocking': False, 'endorsed': False, 'note': ''}


def _act(account_id: int, action):
    person = _person(account_id)
    if person is None:
        return _not_found()
    try:
        action(person.id, SRC_API, Authorised(g.oauth_user))
    except Exception as error:
        return failure(error)
    db.session.expire_all()
    return jsonify(relationship(g.oauth_user, person.id))


@bp.route('/api/v1/accounts/<int:account_id>/follow', methods=['POST'])
@require_oauth('write:follows')
def follow(account_id):
    return _act(account_id, shared_user.follow_user)


@bp.route('/api/v1/accounts/<int:account_id>/unfollow', methods=['POST'])
@require_oauth('write:follows')
def unfollow(account_id):
    return _act(account_id, shared_user.unfollow_user)


@bp.route('/api/v1/accounts/<int:account_id>/block', methods=['POST'])
@require_oauth('write:blocks')
def block(account_id):
    return _act(account_id, shared_user.block_another_user)


@bp.route('/api/v1/accounts/<int:account_id>/unblock', methods=['POST'])
@require_oauth('write:blocks')
def unblock(account_id):
    return _act(account_id, shared_user.unblock_another_user)


def _fields() -> dict:
    body = request.get_json(silent=True) if request.is_json else request.form
    return body if isinstance(body, dict) else {}


def _seconds(value) -> int:
    """`duration`: a whole number of seconds, 0 (or nothing usable) for a mute without end."""
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


@bp.route('/api/v1/accounts/<int:account_id>/mute', methods=['POST'])
@require_oauth('write:mutes')
def mute(account_id):
    fields = _fields()
    notifications = str(fields.get('notifications', True)).lower() not in ('false', '0')
    return _act(account_id, lambda person_id, src, auth: shared_user.mute_user(
        person_id, notifications, _seconds(fields.get('duration')), src, auth))


@bp.route('/api/v1/accounts/<int:account_id>/unmute', methods=['POST'])
@require_oauth('write:mutes')
def unmute(account_id):
    return _act(account_id, shared_user.unmute_user)


@bp.route('/api/v1/mutes')
@require_oauth('read:mutes')
def mutes():
    people = User.query.filter(User.id.in_(shared_user.muted_ids(g.oauth_user.id)), User.deleted == False).all()
    page = Page(g.oauth_user, [])
    page.authors.update({person.id: person for person in people})
    return jsonify([page.account(person) for person in people])


@bp.route('/api/v1/accounts/relationships')
@require_oauth('read:follows')
def relationships():
    wanted = request.args.getlist('id[]') or request.args.getlist('id')
    answer = []
    for raw in dict.fromkeys(wanted):
        if raw.isdigit() and (person := _person(int(raw))) is not None:
            answer.append(relationship(g.oauth_user, person.id))
    return jsonify(answer)


@bp.route('/api/v1/follow_requests')
@require_oauth('read:follows')
def follow_requests():
    people = shared_user.follow_requests_of(g.oauth_user)
    page = Page(g.oauth_user, [])
    page.authors.update({person.id: person for person in people})
    return jsonify([page.account(person) for person in people])


def _answer(account_id: int, accept: bool):
    if account_id > _PG_INT_MAX or not shared_user.answer_follow_request(g.oauth_user, account_id, accept):
        return _not_found()
    db.session.expire_all()
    return jsonify(relationship(g.oauth_user, account_id))


@bp.route('/api/v1/follow_requests/<int:account_id>/authorize', methods=['POST'])
@require_oauth('write:follows')
def authorize_follow_request(account_id):
    return _answer(account_id, True)


@bp.route('/api/v1/follow_requests/<int:account_id>/reject', methods=['POST'])
@require_oauth('write:follows')
def reject_follow_request(account_id):
    return _answer(account_id, False)
