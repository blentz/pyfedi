"""Custom themes per domain (docs/superpowers/specs/2026-11-18-custom-themes-design.md).

A theme column (`User.theme`, `Community.theme`, `Site.default_theme`, `DomainBranding.default_theme`) holds a shipped
theme's directory name, `piefed`, or `custom:<id>` for a `CustomTheme`. A custom theme counts only on the domain that
owns it (CT3); a shipped theme or `piefed` counts everywhere."""
from __future__ import annotations

import os
import re

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
    if identifier == 'piefed' or is_shipped(identifier):
        return True
    theme_id = custom_theme_id(identifier)
    if theme_id is None:
        return False
    theme = db.session.get(models.CustomTheme, theme_id)
    return theme is not None and theme.site_domain_id == domain.id


def custom_themes_for(domain_id: int, include_retired: bool) -> list:
    """A domain's custom themes, by name ignoring case; retired ones only if `include_retired`."""
    query = db.session.query(models.CustomTheme).filter(models.CustomTheme.site_domain_id == domain_id)
    if not include_retired:
        query = query.filter(models.CustomTheme.retired.is_(False))
    return query.order_by(db.func.lower(models.CustomTheme.name), models.CustomTheme.id).all()
