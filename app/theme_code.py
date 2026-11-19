"""Theme code: a custom theme's Jinja template overrides and its script (custom themes phase 6, spec
docs/superpowers/specs/2026-11-18-theme-code-import-export-design.md).

Overrides live in the database (TC2) and reach Jinja through `CustomThemeLoader`, under names
`custom_theme/<id>/<version>/<template name>`. The version is the theme's `theme_version`, so a changed theme has new
names and no worker can serve a stale compiled template. Inside an override, `extends`, `include` and `import`
resolve a plain template name to the same theme's override, else its base theme's shipped one, else the app's
(`ThemeCodeEnvironment.join_path`).

Templates run with the app's full context: only holders of `manage theme code` write them (spec §3.1). Nothing here
sandboxes them, by design (§4).
"""
import os
import re
from functools import lru_cache

from flask import current_app
from flask.templating import Environment
from jinja2 import BaseLoader, ChoiceLoader, TemplateNotFound, TemplateSyntaxError

from app import db
from app.custom_themes import theme_version

PREFIX = 'custom_theme/'
SHIPPED_DIR = 'app/templates/themes'
TEMPLATE_SUFFIXES = ('.html', '.xml', '.txt')
_LOADER_NAME = re.compile(r'custom_theme/([1-9][0-9]{0,11})/([0-9]{1,20})/(.+)')


class ThemeCodeError(ValueError):
    """Theme code that cannot be stored; the message says why, for the person who sent it."""


@lru_cache(maxsize=1)
def _app_template_names() -> frozenset:
    """Every template the app itself has, shipped theme directories left out. The app's templates do not change
    while it runs."""
    return frozenset(name for name in current_app.jinja_env.list_templates()
                     if not name.startswith(('themes/', PREFIX)) and name.endswith(TEMPLATE_SUFFIXES))


def check_template_name(name) -> str:
    """`name` if a custom theme may override it (TC3: any template the app has), else ThemeCodeError."""
    if not isinstance(name, str) or name not in _app_template_names():
        raise ThemeCodeError(f'{name!r} is not a template this site has.')
    return name


def check_template_source(source) -> str:
    """`source` if it is text within the size limit and parses as Jinja, else ThemeCodeError naming the line."""
    if not isinstance(source, str):
        raise ThemeCodeError('A template must be text.')
    limit = current_app.config['CUSTOM_THEME_MAX_TEMPLATE_BYTES']
    if len(source.encode('utf-8')) > limit:
        raise ThemeCodeError(f'A template may be at most {limit} bytes.')
    try:
        current_app.jinja_env.parse(source)
    except TemplateSyntaxError as error:
        raise ThemeCodeError(f'The template does not parse, line {error.lineno}: {error.message}') from None
    return source


def loader_name(theme, name: str) -> str:
    """The name Jinja loads `theme`'s override of `name` by, as of the theme's last save."""
    return f'{PREFIX}{theme.id}/{theme_version(theme)}/{name}'


@lru_cache(maxsize=1024)
def _theme_code(theme_id: int, version: str):
    """(base theme, the names it overrides) of theme `theme_id` as of `version`, or None when it has no such
    version. Keyed by version, so a save is never served from here. Sources are not kept: Jinja caches what it
    compiles."""
    from app.models import CustomTheme
    theme = db.session.get(CustomTheme, theme_id)
    if theme is None or str(theme_version(theme)) != version:
        return None
    return theme.base_theme, frozenset(template.name for template in theme.templates)


def override_names(theme_id: int, version: str):
    """The template names theme `theme_id` overrides as of `version`, or None when it has no such version."""
    code = _theme_code(theme_id, version)
    return None if code is None else code[1]


class CustomThemeLoader(BaseLoader):
    """Serves `custom_theme/<id>/<version>/<name>` from the database. Every other name is not found here."""

    def get_source(self, environment, template):
        from app.models import CustomThemeTemplate
        found = _LOADER_NAME.fullmatch(template)
        code = found and _theme_code(int(found.group(1)), found.group(2))
        if not code or found.group(3) not in code[1]:
            raise TemplateNotFound(template)
        source = db.session.query(CustomThemeTemplate.source).filter_by(
            custom_theme_id=int(found.group(1)), name=found.group(3)).scalar()
        if source is None:   # removed since the names were read, without a new version
            raise TemplateNotFound(template)
        return source, None, lambda: True   # a version never changes

    def list_templates(self):
        return []   # names are made per theme and version, never listed


class ThemeCodeEnvironment(Environment):
    """Flask's environment with the database loader in front, and override-relative name resolution."""

    def __init__(self, app, **options):
        super().__init__(app, **options)
        self.loader = ChoiceLoader([CustomThemeLoader(), self.loader])

    def join_path(self, template, parent):
        found = _LOADER_NAME.fullmatch(parent) if isinstance(parent, str) else None
        if found is None or template.startswith(('themes/', PREFIX)):
            return template
        code = _theme_code(int(found.group(1)), found.group(2))
        if code is not None and template in code[1]:
            return f'{PREFIX}{found.group(1)}/{found.group(2)}/{template}'
        base = code[0] if code is not None else None
        if base and os.path.exists(f'{SHIPPED_DIR}/{base}/{template}'):
            return f'themes/{base}/{template}'
        return template
