"""C3: the language variant of a post or comment a viewer sees (docs/superpowers/specs/2026-10-12-ap-interop-c3-
multi-language-design.md). Variants are stored at ingest in `language_variants`; nothing here queries per item."""
from collections import namedtuple
from urllib.parse import urlencode

from flask import g, has_request_context, request

from app import db, get_locale
from app.activitypub.util import primary_subtag
from app.models import Language

Shown = namedtuple('Shown', 'code title body_html cw others language_id')   # language_id: of `code`, None if unknown


def language_ids() -> dict:
    """{code: id} for every Language, kept per request like language_names."""
    if has_request_context() and 'language_ids' in g:
        return g.language_ids
    ids = dict(db.session.query(Language.code, Language.id).all())
    if has_request_context():
        g.language_ids = ids
    return ids


def chosen_variant(variants: dict, default: str, requested: str | None, viewer: str | None) -> tuple[str, dict | None]:
    """The code to show and its variant (None for the default): a valid `requested` code first, then the viewer's."""
    for code in (requested, viewer):
        if code == default:
            return default, None
        if code in variants:
            return code, variants[code]
    return default, None


def language_names() -> dict:
    """{code: name} for every Language, one query per request (cached on `g` only under a request context)."""
    if has_request_context() and 'language_names' in g:
        return g.language_names
    names = dict(db.session.query(Language.code, Language.name).all())
    if has_request_context():
        g.language_names = names
    return names


def shown(item) -> Shown:
    """What a template renders for `item`: the chosen variant's title, body and warning, each falling back to the
    default's, and the other languages a viewer can switch to."""
    title = getattr(item, 'title', None)
    default = item.language.code if item.language_id else 'und'
    variants = item.language_variants or {}
    if not variants:
        return Shown(default, title, item.body_html, item.content_warning, [], item.language_id)
    requested = request.args.get('lang') if has_request_context() else None
    viewer = primary_subtag((get_locale() or '').replace('_', '-')) if has_request_context() else None
    code, variant = chosen_variant(variants, default, requested, viewer)
    names = language_names()
    others = [(c, names.get(c, c)) for c in [default, *variants] if c != code]
    if variant is None:
        return Shown(default, title, item.body_html, item.content_warning, others, item.language_id)
    return Shown(code, variant.get('title') or title, variant['body_html'], variant.get('cw') or item.content_warning,
                 others, language_ids().get(code))


AJAX_ENDPOINTS = ('post.post_lazy_replies', 'post.continue_discussion_ajax')


def switch_url(code: str, anchor: str = '') -> str:
    """The link to the current page in language `code`: every other query parameter stays. A comment fetched on its
    own (lazy load, continue thread) links to the post page, because the fetched URL is not a page."""
    if not has_request_context():
        return f'?lang={code}{anchor}'
    args = {**request.args.to_dict(), 'lang': code}
    base = ''
    if request.endpoint in AJAX_ENDPOINTS:
        base = f"/post/{request.view_args['post_id']}"
        args = {k: v for k, v in args.items() if k in ('sort', 'lang')}
    return f'{base}?{urlencode(args)}{anchor}'
