"""Writing a status from a Mastodon app (plan W1.3; spec 2026-11-07-mastodon-client-api-design §3.4, ruling M4).

Where a status goes (M4):
- `in_reply_to_id`: a comment on that post, or under that comment.
- otherwise, a status mentioning a community (`@name@host` or `!name@host`, the first one it names): a post there,
  titled as an inbound Mastodon note is (its first sentence, `microblog_content_to_title`).
- otherwise: a post in the person's microblogs (the local `microblogs` community), titled the same way.

Each runs through the shared create, edit and delete functions the alpha API and the web use, as the token's person
(app.utils.Authorised). Mastodon's `spoiler_text` is the content warning, `sensitive` NSFW, `private` visibility
followers-only (microblogs alone take it); `direct` has no PieFed form. A repeat with the same `Idempotency-Key` within
an hour answers the status the first one made, as Mastodon's does (Tusky sends one with every status)."""
import re

from flask import g, jsonify, request
from sqlalchemy import func
from werkzeug.exceptions import HTTPException

from app import cache, db
from app.activitypub.util import find_microblogging_community
from app.constants import POST_TYPE_ARTICLE, SRC_API, VISIBILITY_FOLLOWERS, VISIBILITY_PUBLIC
from app.local_names import local_named
from app.local_only import hidden_here
from app.mastodon import bp
from app.mastodon import ids
from app.mastodon.ids import find_status
from app.mastodon.statuses import Page
from app.mastodon.timelines import readable
from app.models import Community, Language, Post, PostReply
from app.oauth_server.protect import require_oauth
from app.shared import post as shared_post
from app.shared import reply as shared_reply
from app.storefront import domain_for_host
from app.utils import Authorised, html_to_text, is_local_microblogs, markdown_to_html, microblog_content_to_title, \
    site_language_id

MAX_CHARACTERS = 5000   # what /api/v2/instance tells apps (app.mastodon.instance)
TITLE_MAX = 255
IDEMPOTENCY_SECONDS = 3600
# a mention of a community, as a Mastodon user writes one: @name@host, or Lemmy's !name@host
COMMUNITY_MENTION = re.compile(r'(?<![\w@!])[@!]([A-Za-z0-9_]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b')
_VISIBILITY = {'public': VISIBILITY_PUBLIC, 'unlisted': VISIBILITY_PUBLIC, 'private': VISIBILITY_FOLLOWERS}


class _Refused(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _fields() -> dict:
    """The request's fields, from JSON (Tusky's) or a form; `media_ids[]` as a list either way."""
    if request.is_json:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise _Refused('Validation failed: the body is not an object')
        return body
    fields = request.form.to_dict()
    fields['media_ids'] = request.form.getlist('media_ids[]') or request.form.getlist('media_ids')
    return fields


def _text(fields: dict, name: str) -> str:
    value = fields.get(name) or ''
    if not isinstance(value, str):
        raise _Refused(f'Validation failed: {name} is not text')
    return value


def _truthy(value) -> bool:
    return value is True or str(value).lower() in ('1', 'true')


def _language_id(code) -> int | None:
    if isinstance(code, str) and code:
        language = Language.query.filter(func.lower(Language.code) == code.lower()).first()
        if language is not None:
            return language.id
    return site_language_id()


def _visibility(fields: dict) -> str:
    wanted = fields.get('visibility') or 'public'
    if wanted == 'direct':
        raise _Refused('Validation failed: direct messages are not statuses here')
    if wanted not in _VISIBILITY:
        raise _Refused(f'Validation failed: {wanted!r} is not a visibility')
    return _VISIBILITY[wanted]


def _mentioned_community(text: str) -> Community | None:
    """The first community the status names, here or on a peer this instance knows; None for none."""
    for match in COMMUNITY_MENTION.finditer(text):
        name, host = match.group(1).lower(), match.group(2).lower()
        hosted = domain_for_host(host)
        if hosted is not None:
            community = Community.query.filter(local_named(Community, name, hosted)).first()
        else:
            community = Community.query.filter(func.lower(Community.name) == name,
                                               func.lower(Community.ap_domain) == host).first()
        if community is not None and not community.banned and not hidden_here(community, True):
            return community
    return None


def _title(text: str) -> str:
    """As an inbound Mastodon note is titled: its first sentence, else its text. It can be empty (text that renders to
    nothing), as an image-only note's is."""
    title = microblog_content_to_title(markdown_to_html(text))[0] or html_to_text(markdown_to_html(text))
    return ' '.join(title.split())[:TITLE_MAX]


def _respond(row, status: int = 200):
    db.session.expire(row)
    entity = Page(g.oauth_user, [row]).statuses([row])[0]
    return jsonify(entity), status


def _guarded(write):
    """Runs `write`, answering a refusal as Mastodon does: 422 with its reason, a shared write's abort with its code."""
    try:
        return write()
    except _Refused as refused:
        return jsonify({'error': str(refused)}), refused.status
    except HTTPException as refused:
        return jsonify({'error': refused.description or refused.name}), refused.code
    except Exception as refused:   # the shared functions refuse with a bare Exception and its reason
        db.session.rollback()
        return jsonify({'error': str(refused) or 'Validation failed'}), 422


def _create(fields: dict):
    text = _text(fields, 'status')
    if fields.get('media_ids'):
        raise _Refused('Validation failed: media are not taken yet')
    if not text.strip():
        raise _Refused("Validation failed: Text can't be blank")
    if len(text) > MAX_CHARACTERS:
        raise _Refused(f'Validation failed: Text is longer than {MAX_CHARACTERS} characters')
    warning = _text(fields, 'spoiler_text').strip() or None
    language_id = _language_id(fields.get('language'))
    visibility = _visibility(fields)
    auth = Authorised(g.oauth_user)
    if reply_to := fields.get('in_reply_to_id'):
        parent = find_status(str(reply_to))
        if not readable(parent, g.oauth_user.id):
            raise _Refused('Record not found', 404)
        post = parent if isinstance(parent, Post) else parent.post
        _uid, reply = shared_reply.make_reply(
            {'body': text, 'notify_author': True, 'language_id': language_id, 'content_warning': warning},
            post, parent.id if isinstance(parent, PostReply) else None, SRC_API, auth)
        return reply
    community = _mentioned_community(text) or find_microblogging_community()
    _uid, post = shared_post.make_post(
        {'title': _title(text), 'body': text, 'url': None, 'nsfw': _truthy(fields.get('sensitive')),
         'language_id': language_id, 'notify_author': True, 'ai_generated': False, 'visibility': visibility,
         'content_warning': warning}, community, POST_TYPE_ARTICLE, SRC_API, auth)
    return post


@bp.route('/api/v1/statuses', methods=['POST'])
@require_oauth('write:statuses')
def create_status():
    key = request.headers.get('Idempotency-Key', '').strip()
    cache_key = f'mastodon:idempotency:{g.oauth_user.id}:{key}'[:250] if key else None
    if cache_key and (made := cache.get(cache_key)):
        row = find_status(made)
        if row is not None and not row.deleted:
            return _respond(row)

    def write():
        row = _create(_fields())
        if cache_key:
            cache.set(cache_key, ids.status_id(row), timeout=IDEMPOTENCY_SECONDS)
        return _respond(row)
    return _guarded(write)


def _own(status: str):
    """The viewer's own post or comment `status` names, or a refusal: another's is not theirs to change."""
    row = find_status(status)
    if not readable(row, g.oauth_user.id):
        raise _Refused('Record not found', 404)
    if row.user_id != g.oauth_user.id:
        raise _Refused('This action is not allowed', 403)
    return row


@bp.route('/api/v1/statuses/<status_id>', methods=['DELETE'])
@require_oauth('write:statuses')
def delete_status(status_id):
    def write():
        row = _own(status_id)
        entity = Page(g.oauth_user, [row]).statuses([row])[0]
        entity['text'] = row.body or ''
        auth = Authorised(g.oauth_user)
        if isinstance(row, Post):
            shared_post.delete_post(row.id, True, SRC_API, auth)
        else:
            shared_reply.delete_reply(row.id, SRC_API, auth)
        return jsonify(entity)
    return _guarded(write)


@bp.route('/api/v1/statuses/<status_id>/source')
@require_oauth('read:statuses')
def status_source(status_id):
    def read():
        row = _own(status_id)
        return jsonify({'id': ids.status_id(row), 'text': row.body or '', 'spoiler_text': row.content_warning or ''})
    return _guarded(read)


@bp.route('/api/v1/statuses/<status_id>', methods=['PUT'])
@require_oauth('write:statuses')
def edit_status(status_id):
    def write():
        row = _own(status_id)
        fields = _fields()
        text = _text(fields, 'status')
        if not text.strip():
            raise _Refused("Validation failed: Text can't be blank")
        if len(text) > MAX_CHARACTERS:
            raise _Refused(f'Validation failed: Text is longer than {MAX_CHARACTERS} characters')
        changes = {'body': text, 'notify_author': row.notify_author,
                   'language_id': _language_id(fields.get('language')) if fields.get('language') else row.language_id,
                   'content_warning': _text(fields, 'spoiler_text').strip() or None}
        auth = Authorised(g.oauth_user)
        if isinstance(row, PostReply):
            shared_reply.edit_reply({**changes, 'distinguished': row.distinguished}, row, row.post, SRC_API, auth)
        else:
            # a microblog post's title is its text's; a community post keeps the title it was given
            title = _title(text) if is_local_microblogs(row.community) else row.title
            shared_post.edit_post({**changes, 'title': title, 'url': row.url, 'nsfw': _truthy(fields.get('sensitive')),
                                   'ai_generated': row.ai_generated}, row, row.type, SRC_API, auth=auth)
        return _respond(row)
    return _guarded(write)
