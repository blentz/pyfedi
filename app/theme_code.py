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
from datetime import timedelta
from functools import lru_cache

from flask import current_app, has_request_context, request
from flask.templating import Environment
from jinja2 import BaseLoader, ChoiceLoader, TemplateNotFound, TemplateSyntaxError
from sqlalchemy import or_, update

from app import db
from app.clock import utcnow
from app.custom_themes import theme_version

PREFIX = 'custom_theme/'
SHIPPED_DIR = 'app/templates/themes'
TEMPLATE_SUFFIXES = ('.html', '.xml', '.txt')
_LOADER_NAME = re.compile(r'custom_theme/([1-9][0-9]{0,11})/([0-9]{1,20})/(.+)')


class ThemeCodeError(ValueError):
    """Theme code that cannot be stored; the message says why, for the person who sent it."""


@lru_cache(maxsize=1)
def app_template_names() -> frozenset:
    """Every template the app itself has, shipped theme directories left out. The app's templates do not change
    while it runs."""
    return frozenset(name for name in current_app.jinja_env.list_templates()
                     if not name.startswith(('themes/', PREFIX)) and name.endswith(TEMPLATE_SUFFIXES))


def check_template_name(name) -> str:
    """`name` if a custom theme may override it (TC3: any template the app has), else ThemeCodeError."""
    if not isinstance(name, str) or name not in app_template_names():
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


_ACTIVE = 'piefed.theme_code'          # request.environ key: the request's code theme, worked out once
_OFF = 'piefed.theme_code_off'         # request.environ key: render without theme code (the fallback)
FAILURE_SECONDS = 7 * 24 * 3600   # how long a failure is shown to the theme's holders
FAILURE_THROTTLE = 60             # at most one failure write a minute per theme


def active_code_theme():
    """(theme id, version, override names) of the code theme in effect for this request, or None: outside a
    request, when the theme in effect is not a custom theme of this domain, when it overrides nothing, or while a
    page renders again without theme code. Kept on the request, which a test's shared `g` would not be."""
    if not has_request_context() or request.environ.get(_OFF):
        return None
    if _ACTIVE not in request.environ:
        from app.custom_themes import custom_theme_here
        from app.storefront import current_storefront
        from app.utils import current_theme
        custom = custom_theme_here(current_theme(), current_storefront())
        active = None
        if custom is not None:
            version = str(theme_version(custom))
            names = override_names(custom.id, version)
            active = (custom.id, version, names) if names else None
        request.environ[_ACTIVE] = active
    return request.environ[_ACTIVE]


def override_for(template_name: str):
    """The loader name of the active code theme's override of `template_name`, or None."""
    active = active_code_theme()
    if active is None or template_name not in active[2]:
        return None
    return f'{PREFIX}{active[0]}/{active[1]}/{template_name}'


def render_with_fallback(render, template_name: str):
    """`render()` with theme code; if that raises in a code theme, `render()` again without it. The failure is kept
    for the theme's holders (`last_failure`) only when the page renders without the theme's code, so an app error is
    never blamed on a theme."""
    active = active_code_theme()
    if active is None:
        return render()
    try:
        return render()
    except Exception as error:   # noqa: BLE001 -- any error in theme code; the page must still render
        request.environ[_OFF] = True
        content = render()        # an app error raises here, as it would have anyway
        current_app.logger.warning('Custom theme %s failed rendering %s', active[0], template_name, exc_info=True)
        _record_failure(active[0], template_name, error)
        return content


def _record_failure(theme_id: int, template_name: str, error: Exception) -> None:
    """Keeps the failure on the theme's row, in a transaction of its own (the page's own session may hold anything),
    at most once a minute per theme so a broken theme on a busy domain does not write on every request."""
    from app.models import CustomTheme
    table, now = CustomTheme.__table__, utcnow()
    with db.engine.begin() as connection:
        connection.execute(update(table).where(
            table.c.id == theme_id,
            or_(table.c.code_failed_at.is_(None), table.c.code_failed_at < now - timedelta(seconds=FAILURE_THROTTLE))
        ).values(code_failed_template=template_name, code_failure=str(error)[:500], code_failed_at=now))


def last_failure(theme_id: int):
    """{'template', 'error', 'at'} of the theme's last failed render in the past week, or None."""
    from app.models import CustomTheme
    row = db.session.query(CustomTheme.code_failed_template, CustomTheme.code_failure, CustomTheme.code_failed_at) \
        .filter(CustomTheme.id == theme_id).one_or_none()
    if row is None or row.code_failed_at is None or row.code_failed_at < utcnow() - timedelta(seconds=FAILURE_SECONDS):
        return None
    return {'template': row.code_failed_template, 'error': row.code_failure, 'at': row.code_failed_at}


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
        if template.startswith(('themes/', PREFIX)):
            return template
        found = _LOADER_NAME.fullmatch(parent) if isinstance(parent, str) else None
        if found is None:   # in an app or shipped template: the request's code theme may override the name
            return override_for(template) or template
        code = _theme_code(int(found.group(1)), found.group(2))
        if code is not None and template in code[1]:
            return f'{PREFIX}{found.group(1)}/{found.group(2)}/{template}'
        base = code[0] if code is not None else None
        if base and os.path.exists(f'{SHIPPED_DIR}/{base}/{template}'):
            return f'themes/{base}/{template}'
        return template
