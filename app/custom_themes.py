"""Custom themes per domain (docs/superpowers/specs/2026-11-18-custom-themes-design.md).

A theme column (`User.theme`, `Community.theme`, `Site.default_theme`, `DomainBranding.default_theme`) holds a shipped
theme's directory name, `piefed`, or `custom:<id>` for a `CustomTheme`. A custom theme counts only on the domain that
owns it (CT3); a shipped theme or `piefed` counts everywhere."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from app import db
import app.models as models

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
    """The `CustomTheme` `identifier` names when it counts on `domain`, else None."""
    theme_id = custom_theme_id(identifier)
    theme = db.session.get(models.CustomTheme, theme_id) if theme_id is not None else None
    return theme if theme is not None and theme.site_domain_id == domain.id else None


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
