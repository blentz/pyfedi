"""Custom themes per domain (docs/superpowers/specs/2026-11-18-custom-themes-design.md).

A theme column (`User.theme`, `Community.theme`, `Site.default_theme`, `DomainConfig.default_theme`) holds a shipped
theme's directory name, `piefed`, or `custom:<id>` for a `CustomTheme`. A custom theme counts only on the domain that
owns it (CT3); a shipped theme or `piefed` counts everywhere."""
from __future__ import annotations

import io
import json
import os
import posixpath
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote
from xml.etree import ElementTree

from flask import current_app
from PIL import Image

from app import db
from app.permissions import user_access
import app.domain_config as domain_config
import app.models as models
import app.storefront as storefront

CUSTOM_PREFIX = 'custom:'
THEME_PREVIEW = 'theme_preview'   # the session key of a theme holder's preview: {'domain_id', 'theme'}
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


def manages_themes(user_id, domain) -> bool:
    """Whether user `user_id` manages domain `domain`'s custom themes (CT4): `edit domain branding` on it, or for the
    first domain, whose branding is the site settings, `change instance settings`. `domain` is a `DomainInfo` or a
    `SiteDomain` row."""
    if domain.is_default:
        return user_access('change instance settings', user_id)
    return user_access('edit domain branding', user_id, on=domain)


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
    relative URLs made absolute), or a custom theme of the same domain (its CSS, a copy of each of its files, and its
    template overrides and script: theme code spec §3.7). The copy is not retired.
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
    db.session.flush()
    for file in custom.files if custom is not None else ():   # the copy's own stored files, not shared ones
        try:
            data = read_stored(file.path)
        except OSError:
            current_app.logger.warning('custom themes: %s of theme %s is missing: not copied', file.path, custom.id)
            continue
        db.session.add(models.CustomThemeFile(custom_theme_id=theme.id, filename=file.filename, path=_store(data),
                                              mime_type=file.mime_type, size=file.size))
    if custom is not None:   # theme code goes with a copy, whoever makes it (spec §3.6)
        theme.script = custom.script
        db.session.add_all([models.CustomThemeTemplate(custom_theme_id=theme.id, name=template.name,
                                                       source=template.source, updated_at=now)
                            for template in custom.templates])
    db.session.commit()
    return theme


def uses(theme) -> dict:
    """Where `theme` is chosen: as its domain's default (the site default for the first domain), and by how many
    users and communities. A theme with any use cannot be deleted (CT7)."""
    identifier = custom_identifier(theme.id)
    site = db.session.get(models.Site, 1)
    branding = domain_config.own_config(theme.site_domain_id)
    return {
        'domain_default': (branding is not None and branding.default_theme == identifier) or
                          (site is not None and site.default_theme == identifier),
        'users': db.session.query(models.User.id).filter(models.User.theme == identifier).count(),
        'communities': db.session.query(models.Community.id).filter(models.Community.theme == identifier).count(),
    }


def in_use(theme) -> bool:
    found = uses(theme)
    return found['domain_default'] or found['users'] > 0 or found['communities'] > 0


# --- A custom theme's files (spec §3.7) ---

# Stored with the other local media, each as a random name with `.bin`: fetched straight from /static it is
# application/octet-stream and renders as nothing. Only `/theme/<id>/files/<name>` serves it, with its checked type.
THEME_MEDIA = 'app/static/media/themes'
_STORED = re.compile(r'/static/media/themes/[0-9a-f]{32}\.bin')
_FILENAME_UNSAFE = re.compile(r'[^A-Za-z0-9._-]+')
SVG = 'image/svg+xml'
_SVG_ROOTS = ('svg', '{http://www.w3.org/2000/svg}svg')
# (MIME type, extension) by leading bytes; WebP and AVIF are checked further below
_MAGIC = [(b'\x89PNG\r\n\x1a\n', ('image/png', 'png')), (b'\xff\xd8\xff', ('image/jpeg', 'jpg')),
          (b'GIF87a', ('image/gif', 'gif')), (b'GIF89a', ('image/gif', 'gif')),
          (b'wOF2', ('font/woff2', 'woff2')), (b'wOFF', ('font/woff', 'woff')),
          (b'\x00\x01\x00\x00', ('font/ttf', 'ttf')), (b'true', ('font/ttf', 'ttf')), (b'OTTO', ('font/otf', 'otf'))]
_PILLOW_CHECKED = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}


class ThemeFileError(ValueError):
    """An upload a theme cannot take: the message says why."""


def sniff_type(data: bytes) -> tuple[str, str] | None:
    """The (MIME type, extension) `data` is, by its content alone, if it is one a theme may hold: PNG, JPEG, GIF,
    WebP, AVIF, SVG, WOFF2, WOFF, TTF or OTF. A raster image must also open in Pillow. Anything else is None."""
    found = next((kind for magic, kind in _MAGIC if data.startswith(magic)), None)
    if found is None and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        found = ('image/webp', 'webp')
    if found is None and data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'):
        found = ('image/avif', 'avif')
    if found is None:
        return (SVG, 'svg') if _is_svg(data) else None
    if found[0] in _PILLOW_CHECKED:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except Exception:
            return None
    return found


def _is_svg(data: bytes) -> bool:
    """Whether `data` is an XML document whose root is `<svg>`, with no DTD (no entities to expand)."""
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return False
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        return False
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return False
    return root.tag in _SVG_ROOTS


def clean_filename(name: str, extension: str) -> str:
    """`name` as a theme file's name: its last path part, reduced to letters, digits, `.`, `_` and `-`, no leading
    dot, ending in `extension` (the checked type's), at most `NAME_MAX` characters."""
    stem = (name or '').replace('\\', '/').rsplit('/', 1)[-1]
    stem = stem.rsplit('.', 1)[0] if '.' in stem else stem
    stem = _FILENAME_UNSAFE.sub('-', stem).lstrip('.-') or 'file'
    return f'{stem[:NAME_MAX - len(extension) - 1]}.{extension}'


def _store(data: bytes) -> str:
    """Write `data` to a new stored file; its path as a theme file row keeps it."""
    os.makedirs(THEME_MEDIA, exist_ok=True)
    name = f'{secrets.token_hex(16)}.bin'
    with open(os.path.join(THEME_MEDIA, name), 'xb') as file:
        file.write(data)
    return f'/static/media/themes/{name}'


def read_stored(path: str) -> bytes:
    with open(f'app{path}', 'rb') as file:
        return file.read()


def add_theme_file(theme, upload_name: str, data: bytes, replace: bool, limits: dict):
    """Store `data` as one of `theme`'s files, named from `upload_name`, and return its row (committed). Refused with
    ThemeFileError: a type a theme may not hold, a name already taken unless `replace`, or any of `limits`
    (`file_bytes`, `theme_bytes`, `files`). A replaced file's stored copy is removed once the row no longer names it."""
    if len(data) > limits['file_bytes']:
        raise ThemeFileError('That file is too big.')
    kind = sniff_type(data)
    if kind is None:
        raise ThemeFileError('That is not a PNG, JPEG, GIF, WebP, AVIF or SVG image, or a WOFF2, WOFF, TTF or OTF font.')
    filename = clean_filename(upload_name, kind[1])
    existing = next((file for file in theme.files if file.filename == filename), None)
    if existing is not None and not replace:
        raise ThemeFileError(f'This theme already has a file named {filename}.')
    others = [file for file in theme.files if file is not existing]
    if len(others) + 1 > limits['files']:
        raise ThemeFileError('This theme has as many files as a theme may hold.')
    if sum(file.size for file in others) + len(data) > limits['theme_bytes']:
        raise ThemeFileError('This theme\'s files would be over the size a theme may hold.')
    path = _store(data)
    replaced = existing.path if existing is not None else None
    if existing is not None:
        existing.path, existing.mime_type, existing.size = path, kind[0], len(data)
        row = existing
    else:
        row = models.CustomThemeFile(custom_theme_id=theme.id, filename=filename, path=path, mime_type=kind[0],
                                     size=len(data))
        db.session.add(row)
    theme.updated_at = models.utcnow()
    db.session.commit()
    if replaced:
        remove_theme_files([replaced])
    return row


def remove_theme_files(paths) -> None:
    """Unlink the stored files `paths` named, once no theme file row names them. A path goes only when it is one
    `_store` gives, is not a symlink, and with every symlink and `..` resolved is that same name directly inside
    `THEME_MEDIA`. Anything else is left where it is, and logged. `paths` are rows' stored values, never form input."""
    media = os.path.realpath(THEME_MEDIA)
    named = {path for (path,) in db.session.query(models.CustomThemeFile.path)}
    for path in paths:
        stored = f'app{path}'
        if not _STORED.fullmatch(path or '') or os.path.islink(stored) or path in named or \
                os.path.realpath(stored) != os.path.join(media, os.path.basename(path)):
            current_app.logger.info('custom themes: %s not removed: not a theme file no row names', path)
        elif os.path.isfile(stored):
            os.unlink(stored)


def theme_file_paths(themes) -> list[str]:
    """Every stored path the files of `themes` name: what to pass `remove_theme_files` once they are deleted."""
    return [file.path for theme in themes for file in theme.files]
