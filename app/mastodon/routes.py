"""Mastodon's client API routes (plan W1; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md)."""
from flask import g, jsonify, request
from sqlalchemy import and_, func
from sqlalchemy.orm.exc import NoResultFound

from app.mastodon import bp, entities
from app.models import User
from app.oauth_server.protect import require_oauth
from app.reads import users as reads_users

_PG_INT_MAX = 2 ** 31 - 1


def _not_found():
    return jsonify({'error': 'Record not found'}), 404


def _viewer_id() -> int | None:
    return g.oauth_user.id if g.oauth_user else None


@bp.route('/api/v1/accounts/verify_credentials')
@require_oauth('read:accounts')
def verify_credentials():
    return jsonify(entities.account(g.oauth_user, g.oauth_user.id, with_source=True))


@bp.route('/api/v1/accounts/lookup')
@require_oauth('read:accounts', optional=True)
def lookup_account():
    """An account by `acct`: `name` for this domain's, `name@host` for another's (a hosted domain's or a peer's)."""
    name, _sep, host = request.args.get('acct', '').strip().lstrip('@').partition('@')
    name, host = name.lower(), host.lower() or None
    if not name or (host is not None and '@' in host):
        return _not_found()
    remote_named = and_(func.lower(User.user_name) == name, func.lower(User.ap_domain) == host) if host else None
    try:
        person = reads_users.person_named(name, host, remote_named)
        reads_users.refuse_hidden_person(person, g.oauth_user is not None)
    except NoResultFound:
        return _not_found()
    return jsonify(entities.account(person, _viewer_id()))


@bp.route('/api/v1/accounts/<int:account_id>')
@require_oauth('read:accounts', optional=True)
def get_account(account_id):
    if account_id > _PG_INT_MAX:
        return _not_found()
    try:
        person = reads_users.person_by_id(account_id, g.oauth_user is not None, reads_users.BY_NAME)
    except NoResultFound:
        return _not_found()
    return jsonify(entities.account(person, _viewer_id()))
