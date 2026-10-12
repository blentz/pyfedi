"""C3: the language variant of a post or comment a viewer sees (docs/superpowers/specs/2026-10-12-ap-interop-c3-
multi-language-design.md). Variants are stored at ingest in `language_variants`; nothing here queries per item."""
from collections import namedtuple

from flask import g, has_request_context, request

from app import db, get_locale
from app.activitypub.util import primary_subtag
from app.models import Language

Shown = namedtuple('Shown', 'code title body_html cw others')


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
        return Shown(default, title, item.body_html, item.content_warning, [])
    requested = request.args.get('lang') if has_request_context() else None
    viewer = primary_subtag((get_locale() or '').replace('_', '-')) if has_request_context() else None
    code, variant = chosen_variant(variants, default, requested, viewer)
    names = language_names()
    others = [(c, names.get(c, c)) for c in [default, *variants] if c != code]
    if variant is None:
        return Shown(default, title, item.body_html, item.content_warning, others)
    return Shown(code, variant.get('title') or title, variant['body_html'], variant.get('cw') or item.content_warning,
                 others)
