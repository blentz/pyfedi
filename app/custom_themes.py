"""Custom themes per domain (docs/superpowers/specs/2026-11-18-custom-themes-design.md).

A theme column (`User.theme`, `Community.theme`, `Site.default_theme`, `DomainBranding.default_theme`) holds a shipped
theme's directory name, `piefed`, or `custom:<id>` for a `CustomTheme`. A custom theme counts only on the domain that
owns it (CT3); a shipped theme or `piefed` counts everywhere."""
from __future__ import annotations

import json
import os
import posixpath
import re
from datetime import datetime, timedelta
from urllib.parse import quote

from app import db
import app.models as models
import app.storefront as storefront

CUSTOM_PREFIX = 'custom:'
THEMES_DIR = 'app/templates/themes'
_CUSTOM = re.compile(r'custom:([1-9][0-9]{0,11})')       # at most 19 characters: a theme column is String(20)
_SHIPPED_NAME = re.compile(r'[A-Za-z0-9_-]+')


def custom_theme_id(identifier: str | None) -> int | None:
    """The `CustomTheme` id `identifier` names, or None when it names no custom theme."""
    match = _CUSTOM.fullmatch(identifier or '')
    return int(match.group(1)) if match else None


def custom_identifier(theme_id: int) -> str:
    """What a theme column stores for the custom theme `theme_id`."""
    return f'{CUSTOM_PREFIX}{theme_id}'


def is_shipped(name: str | None) -> bool:
    """Whether `name` is a shipped theme: a directory under `THEMES_DIR` with its `<name>.json`. `piefed` is not."""
    return bool(name) and _SHIPPED_NAME.fullmatch(name) is not None and \
        os.path.isfile(f'{THEMES_DIR}/{name}/{name}.json')


def theme_counts(identifier: str | None, domain) -> bool:
    """Whether `identifier` is a theme a page on `domain` (a `DomainInfo`) can render in: `piefed`, a shipped theme,
    or a custom theme `domain` owns, retired or not (CT3, CT9)."""
    return identifier == 'piefed' or is_shipped(identifier) or custom_theme_here(identifier, domain) is not None


def custom_themes_for(domain_id: int, include_retired: bool) -> list:
    """A domain's custom themes, by name ignoring case; retired ones only if `include_retired`."""
    query = db.session.query(models.CustomTheme).filter(models.CustomTheme.site_domain_id == domain_id)
    if not include_retired:
        query = query.filter(models.CustomTheme.retired.is_(False))
    return query.order_by(db.func.lower(models.CustomTheme.name), models.CustomTheme.id).all()


def theme_version(theme) -> int:
    """The stylesheet URL's `v`: the theme's last save in microseconds since 1970, so every save changes every page's
    link (the stylesheet is cached as immutable)."""
    return (theme.updated_at - datetime(1970, 1, 1)) // timedelta(microseconds=1)


def custom_theme_here(identifier: str | None, domain):
    """The `CustomTheme` `identifier` names when it counts on `domain` (a `DomainInfo`), else None."""
    return owned_theme(identifier, domain.id)


def owned_theme(identifier: str | None, domain_id: int):
    """The `CustomTheme` `identifier` names when domain `domain_id` owns it, else None."""
    theme_id = custom_theme_id(identifier)
    theme = db.session.get(models.CustomTheme, theme_id) if theme_id is not None else None
    return theme if theme is not None and theme.site_domain_id == domain_id else None


# Theme values a peer's Group may carry that mean "no theme of its own": PieFed's own, and the community picker's off
_PLAIN_THEMES = ('piefed', 'disabled')


def federated_theme(identifier: str | None) -> str | None:
    """What a local community's `theme` federates as (spec §3.9): a custom theme as its base theme, or nothing when
    that is `piefed` or no longer ships; any other value as stored. Never `custom:<id>`."""
    theme_id = custom_theme_id(identifier)
    if theme_id is None:
        return identifier or None
    theme = db.session.get(models.CustomTheme, theme_id)
    return theme.base_theme if theme is not None and is_shipped(theme.base_theme) else None


def received_theme(value) -> str | None:
    """A remote Group's `theme`, if it is one to store: a shipped theme's name, `piefed` or `disabled`. Anything else
    (a custom theme's identifier, an unknown name, a non-string) is None and is not stored (spec §3.9)."""
    if isinstance(value, str) and (value in _PLAIN_THEMES or is_shipped(value)):
        return value
    return None


# --- Managing a domain's themes (spec §3.2) ---

NAME_MAX = 100
_SCHEME = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:')
# Characters that would end or break a URL written back into CSS, bare or quoted: percent-encoded instead
_URL_SAFE = "/:?#[]@!$&*+,;=%-._~"


class ThemeNameError(ValueError):
    """A theme name that cannot be used: the message says why."""


def shipped_name(name: str) -> str:
    """A shipped theme's display name, from its `<name>.json`; `PieFed` for piefed."""
    if name == 'piefed':
        return 'PieFed'
    with open(f'{THEMES_DIR}/{name}/{name}.json', encoding='utf-8') as file:
        return json.load(file)['name']


def shipped_css(name: str) -> str:
    """A shipped theme's styles.css as text; '' for piefed or a theme without one."""
    path = f'{THEMES_DIR}/{name}/styles.css'
    if name == 'piefed' or not os.path.isfile(path):
        return ''
    with open(path, encoding='utf-8') as file:
        return file.read()


def _is_url_target(css: str, start: int, quoted: bool) -> bool:
    """Whether the token at `start` is a URL to fetch: a bare `url(...)`, or a string inside `url(` or after
    `@import`. Other strings (`font-family: "Name"`, `content: "x"`) are left alone."""
    if not quoted:
        return True
    before = css[:start - 1].rstrip().lower()
    return before.endswith('url(') or before.endswith('@import')


def rewrite_relative_urls(css: str, base: str) -> str:
    """`css` from shipped theme `base`'s styles.css, with each relative `url()` and `@import` target made the absolute
    `/static/themes/...` path it resolved to there, so a copy served from elsewhere loads the same files. Absolute
    paths, other hosts, `data:` and `#` fragments are kept. Newlines are normalised as the scanner reads them."""
    text, spans = storefront._css_url_spans(css)
    out, last = [], 0
    for start, end, url in spans:
        quoted = start > 0 and text[start - 1] in '"\''
        if not url or url.startswith(('/', '#')) or _SCHEME.match(url) or not _is_url_target(text, start, quoted):
            continue
        path, mark, rest = url.partition('?') if '?' in url else url.partition('#')
        resolved = posixpath.normpath(posixpath.join(f'/static/themes/{base}', path))
        out.append(text[last:start])
        out.append(quote(resolved + mark + rest, safe=_URL_SAFE))
        last = end
    return ''.join(out) + text[last:]


def check_name(name: str, domain_id: int, theme_id: int | None = None) -> str:
    """`name` stripped, if a theme of domain `domain_id` (other than `theme_id`) may take it: 1 to `NAME_MAX`
    characters, no other of the domain's themes by that name ignoring case, and no shipped theme's name."""
    name = (name or '').strip()
    if not name:
        raise ThemeNameError('A theme needs a name.')
    if len(name) > NAME_MAX:
        raise ThemeNameError(f'A theme name is at most {NAME_MAX} characters.')
    if name.lower() in {shipped_name(shipped).lower() for shipped in shipped_themes()}:
        raise ThemeNameError('A shipped theme has that name.')
    taken = db.session.query(models.CustomTheme.id).filter(
        models.CustomTheme.site_domain_id == domain_id, db.func.lower(models.CustomTheme.name) == name.lower())
    if theme_id is not None:
        taken = taken.filter(models.CustomTheme.id != theme_id)
    if taken.first() is not None:
        raise ThemeNameError('Another of this domain\'s themes has that name.')
    return name


def shipped_themes() -> list[str]:
    """Every shipped theme's directory name, and piefed, sorted."""
    return ['piefed'] + sorted(name for name in os.listdir(THEMES_DIR) if is_shipped(name))


def unique_copy_name(name: str, domain_id: int) -> str:
    """'Copy of <name>', or 'Copy of <name> (2)', (3)... : the first a theme of `domain_id` may take."""
    stem = f'Copy of {name}'[:NAME_MAX - 6]
    candidate, number = stem, 1
    while True:
        try:
            return check_name(candidate, domain_id)
        except ThemeNameError:
            number += 1
            candidate = f'{stem} ({number})'


def copy_theme(source: str, domain_id: int, user_id: int | None):
    """A new custom theme of domain `domain_id`, copied from `source`: a shipped theme or piefed (its styles.css with
    relative URLs made absolute), or a custom theme of the same domain (its CSS). The copy is not retired.
    `source` names nothing the domain may copy: ValueError."""
    custom = owned_theme(source, domain_id)
    if custom is not None:
        name, base, css = custom.name, custom.base_theme, custom.css
    elif source == 'piefed' or is_shipped(source):
        name, base, css = shipped_name(source), source, rewrite_relative_urls(shipped_css(source), source)
    else:
        raise ValueError(f'nothing to copy: {source!r}')
    now = models.utcnow()
    theme = models.CustomTheme(site_domain_id=domain_id, name=unique_copy_name(name, domain_id), base_theme=base,
                               css=css, retired=False, created_at=now, updated_at=now, updated_by=user_id)
    db.session.add(theme)
    db.session.commit()
    return theme


def uses(theme) -> dict:
    """Where `theme` is chosen: as its domain's default (the site default for the first domain), and by how many
    users and communities. A theme with any use cannot be deleted (CT7)."""
    identifier = custom_identifier(theme.id)
    site = db.session.get(models.Site, 1)
    branding = db.session.get(models.DomainBranding, theme.site_domain_id)
    return {
        'domain_default': (branding is not None and branding.default_theme == identifier) or
                          (site is not None and site.default_theme == identifier),
        'users': db.session.query(models.User.id).filter(models.User.theme == identifier).count(),
        'communities': db.session.query(models.Community.id).filter(models.Community.theme == identifier).count(),
    }


def in_use(theme) -> bool:
    found = uses(theme)
    return found['domain_default'] or found['users'] > 0 or found['communities'] > 0
