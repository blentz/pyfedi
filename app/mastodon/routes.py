"""Mastodon's client API routes (plan W1; spec docs/superpowers/specs/2026-11-07-mastodon-client-api-design.md)."""
import re

from flask import g, jsonify, request
from sqlalchemy import and_, func
from sqlalchemy.orm.exc import NoResultFound
from werkzeug.exceptions import BadRequest

import app.api.alpha.views as alpha_views
from app import cache, db
from app.federation_urls import owner_base
from app.mastodon import bp, entities
from app.models import User, UserExtraField
from app.oauth_server.protect import require_oauth
from app.reads import users as reads_users
from app.user.profile_images import replace_avatar, replace_cover
from app.utils import markdown_to_html, piefed_markdown_to_lemmy_markdown, user_pronouns

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


MAX_DISPLAY_NAME = 255
MAX_NOTE = 5000
MAX_FIELDS = 4
MAX_FIELD = 1024
_NUMBERED_FIELD = re.compile(r'^fields_attributes\[(\d+)\]\[(name|value)\]$')


class _Invalid(ValueError):
    pass


def _refuse(reason: str):
    return jsonify({'error': f'Validation failed: {reason}'}), 422


def _text(fields, name: str, limit: int, label: str) -> str | None:
    """The string `name` holds, stripped, or None when it is left out."""
    if name not in fields:
        return None
    value = fields[name]
    if not isinstance(value, str):
        raise _Invalid(f'{label} is invalid')
    value = value.strip()
    if len(value) > limit:
        raise _Invalid(f'{label} is too long')
    return value


def _flag(fields, name: str) -> bool | None:
    if name not in fields:
        return None
    value = fields[name]
    return value if isinstance(value, bool) else str(value).strip().lower() in ('true', '1', 'on', 'yes')


def _profile_fields(fields) -> list[tuple[str, str]] | None:
    """`fields_attributes`, as a JSON list or numbered object, or a form's fields_attributes[n][name] and [value]:
    the (name, value) pairs with something in them, in order. None when left out."""
    if fields is not request.form:   # a JSON object (a form's MultiDict is a dict too)
        raw = fields.get('fields_attributes')
        if raw is None:
            return None
        if isinstance(raw, dict):
            raw = [raw[key] for key in sorted(raw, key=lambda key: int(key) if str(key).isdigit() else 0)]
        if not isinstance(raw, list):
            raise _Invalid('Fields are invalid')
    else:
        numbered = {}
        for key in fields:
            match = _NUMBERED_FIELD.match(key)
            if match:
                numbered.setdefault(int(match.group(1)), {})[match.group(2)] = fields[key]
        if not numbered:
            return None
        raw = [numbered[index] for index in sorted(numbered)]
    pairs = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise _Invalid('Fields are invalid')
        name, value = entry.get('name', ''), entry.get('value', '')
        if not isinstance(name, str) or not isinstance(value, str):
            raise _Invalid('Fields are invalid')
        name, value = name.strip(), value.strip()
        if len(name) > MAX_FIELD or len(value) > MAX_FIELD:
            raise _Invalid('Fields are too long')
        if name and value:
            pairs.append((name, value))
    if len(pairs) > MAX_FIELDS:
        raise _Invalid(f'Fields can be at most {MAX_FIELDS}')
    return pairs


def _upload(name: str):
    upload = request.files.get(name)
    return upload if upload is not None and upload.filename else None


@bp.route('/api/v1/accounts/update_credentials', methods=['PATCH'])
@require_oauth('write:accounts')
def update_credentials():
    """Display name, bio, avatar, banner, profile fields and the bot, discoverable and indexable flags. Everything is
    checked before anything changes, the images saved first so a refused one changes nothing."""
    fields = request.get_json(silent=True) if request.is_json else request.form
    if not isinstance(fields, dict) and fields is not request.form:
        return _refuse('the body is not an object')
    try:
        display_name = _text(fields, 'display_name', MAX_DISPLAY_NAME, 'Display name')
        note = _text(fields, 'note', MAX_NOTE, 'Note')
        profile_fields = _profile_fields(fields)
    except _Invalid as invalid:
        return _refuse(str(invalid))
    user = g.oauth_user
    try:
        for name, replace in (('avatar', replace_avatar), ('header', replace_cover)):
            if (upload := _upload(name)) is not None:
                replace(user, upload)
    except BadRequest:
        return _refuse('the image could not be read')
    if display_name is not None:
        user.title = display_name or None
    if note is not None:
        user.about = piefed_markdown_to_lemmy_markdown(note, owner_base(user))   # as the profile page (C3)
        user.about_html = markdown_to_html(note, link_base=owner_base(user))
    if profile_fields is not None:
        user.extra_fields = [UserExtraField(label=name, text=value) for name, value in profile_fields]
    for name, column in (('bot', 'bot'), ('discoverable', 'searchable'), ('indexable', 'indexable')):
        if (value := _flag(fields, name)) is not None:
            setattr(user, column, value)
    db.session.commit()
    cache.delete_memoized(user_pronouns)
    cache.delete_memoized(alpha_views.user_view)
    return jsonify(entities.account(user, user.id, with_source=True))


@bp.route('/api/v1/apps/verify_credentials')
@require_oauth(user_required=False)
def verify_app_credentials():
    """The app its token belongs to, without its secret, as Mastodon answers."""
    registered = g.oauth_token.app
    return jsonify({'id': str(registered.id), 'name': registered.name, 'website': registered.website,
                    'scopes': registered.scope.split(), 'redirect_uri': registered.redirect_uris,
                    'redirect_uris': registered.redirect_uri_list()})
