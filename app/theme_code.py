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
import io
import json
import os
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
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


# --- Archives (spec §3.4, §3.5)
ARCHIVE_FORMAT = 1
ARCHIVE_SUFFIX = '.piefed-theme.zip'
_ARCHIVE_TIME = (1980, 1, 1, 0, 0, 0)   # every entry's time: the same theme exports to the same bytes
_SLUG_UNSAFE = re.compile(r'[^a-z0-9]+')


def archive_name(theme_name: str) -> str:
    """The download name of an export of the theme called `theme_name`."""
    slug = _SLUG_UNSAFE.sub('-', theme_name.lower()).strip('-')[:60].strip('-') or 'theme'
    return f'{slug}{ARCHIVE_SUFFIX}'


def write_archive(theme) -> tuple[bytes, list[str]]:
    """`theme` as one zip: manifest.json, styles.css, files/<name>, templates/<name> and scripts.js (spec §3.4).
    Returns the zip and the names of stored files that could not be read, which it leaves out."""
    from app.custom_themes import read_stored
    buffer, missing = io.BytesIO(), []

    with zipfile.ZipFile(buffer, 'w') as archive:
        def add(name: str, data):
            archive.writestr(zipfile.ZipInfo(name, date_time=_ARCHIVE_TIME), data, compress_type=zipfile.ZIP_DEFLATED)
        add('manifest.json', json.dumps({'format': ARCHIVE_FORMAT, 'name': theme.name, 'base_theme': theme.base_theme,
                                         'exported_at': utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                         'software': 'piefed'}, indent=2))
        add('styles.css', theme.css)
        for file in theme.files:
            try:
                add(f'files/{file.filename}', read_stored(file.path))
            except OSError:
                missing.append(file.filename)
        for template in theme.templates:
            add(f'templates/{template.name}', template.source)
        if theme.script:
            add('scripts.js', theme.script)
    return buffer.getvalue(), missing


MANIFEST_BYTES = 64 * 1024
# What zipfile raises for a damaged or crafted archive, beyond BadZipFile: found by the theme_archive fuzz target
# (ValueError: a negative seek; NotImplementedError: an unknown zip version or method).
_ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, OSError, ValueError,
               NotImplementedError, RuntimeError, struct.error, OverflowError)
_SYMLINK = 0o120000
_TOP_LEVEL = ('manifest.json', 'styles.css', 'scripts.js')


@dataclass
class ThemeArchive:
    """What an archive holds, every part checked (spec §3.5); nothing is stored yet."""
    name: str
    base_theme: str
    css: str
    files: dict = field(default_factory=dict)       # file name: bytes
    templates: dict = field(default_factory=dict)   # template name: source
    script: str | None = None

    @property
    def has_code(self) -> bool:
        return bool(self.templates) or self.script is not None


def _entry_limit(name: str, config) -> int:
    if name == 'manifest.json':
        return MANIFEST_BYTES
    if name == 'styles.css':
        return config['CUSTOM_THEME_MAX_CSS_BYTES']
    if name == 'scripts.js':
        return config['CUSTOM_THEME_MAX_SCRIPT_BYTES']
    if name.startswith('files/'):
        return config['CUSTOM_THEME_MAX_FILE_BYTES']
    return config['CUSTOM_THEME_MAX_TEMPLATE_BYTES']


def _check_entry_name(name: str) -> None:
    """Refuses an entry name an archive may not hold. Names are only ever keys here, never paths on disk, but an
    archive with a name like these was not made by an export, and is refused whole."""
    parts = name.split('/')
    if '\\' in name or '\x00' in name or name.startswith('/') or any(part in ('', '.', '..') for part in parts):
        raise ThemeCodeError(f'{name}: not a name an archive may hold.')
    if name in _TOP_LEVEL or (parts[0] == 'files' and len(parts) == 2) or (parts[0] == 'templates' and len(parts) > 1):
        return
    raise ThemeCodeError(f'{name}: not part of a theme (only manifest.json, styles.css, scripts.js, files/ and '
                         f'templates/ are).')


def _text(name: str, data: bytes) -> str:
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise ThemeCodeError(f'{name}: not UTF-8 text.') from None


def read_archive(data: bytes) -> ThemeArchive:
    """The theme an uploaded archive holds, every entry checked before anything is stored (spec §3.5); ThemeCodeError
    naming the entry and the reason otherwise. Sizes are counted while decompressing: zip headers are not trusted."""
    from app.custom_themes import clean_filename, sniff_type
    config = current_app.config
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except _ZIP_ERRORS:
        raise ThemeCodeError('The upload is not a zip archive.') from None
    entries = [info for info in infos if not info.is_dir()]
    most = config['CUSTOM_THEME_MAX_FILES'] + config['CUSTOM_THEME_MAX_TEMPLATES'] + len(_TOP_LEVEL)
    if len(entries) > most:
        raise ThemeCodeError(f'The archive has {len(entries)} entries; a theme has at most {most}.')
    total_limit = (config['CUSTOM_THEME_MAX_BYTES'] + config['CUSTOM_THEME_MAX_CSS_BYTES'] + MANIFEST_BYTES +
                   config['CUSTOM_THEME_MAX_SCRIPT_BYTES'] +
                   config['CUSTOM_THEME_MAX_TEMPLATES'] * config['CUSTOM_THEME_MAX_TEMPLATE_BYTES'])
    contents, total, templates = {}, 0, 0
    for info in entries:
        name = info.filename
        _check_entry_name(name)
        if name in contents:
            raise ThemeCodeError(f'{name}: in the archive twice.')
        if (info.external_attr >> 16) & 0o170000 == _SYMLINK:
            raise ThemeCodeError(f'{name}: a link, not a file.')
        if info.flag_bits & 0x1:
            raise ThemeCodeError(f'{name}: encrypted.')
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ThemeCodeError(f'{name}: a compression method other than stored or deflate.')
        if name.startswith('templates/'):
            templates += 1
            if templates > config['CUSTOM_THEME_MAX_TEMPLATES']:
                raise ThemeCodeError(f'{name}: more templates than a theme may override.')
        limit = _entry_limit(name, config)
        try:
            with archive.open(info) as entry:
                content = entry.read(limit + 1)
        except _ZIP_ERRORS:
            raise ThemeCodeError(f'{name}: damaged in the archive.') from None
        if len(content) > limit:
            raise ThemeCodeError(f'{name}: too big (at most {limit} bytes).')
        total += len(content)
        if total > total_limit:
            raise ThemeCodeError(f'{name}: the archive holds more than a theme may.')
        contents[name] = content

    if 'manifest.json' not in contents:
        raise ThemeCodeError('manifest.json: missing; this is not a theme export.')
    try:
        manifest = json.loads(contents['manifest.json'])
    except ValueError:
        raise ThemeCodeError('manifest.json: not valid JSON.') from None
    if not isinstance(manifest, dict):
        raise ThemeCodeError('manifest.json: not a JSON object.')
    version = manifest.get('format')
    if isinstance(version, int) and not isinstance(version, bool) and version > ARCHIVE_FORMAT:
        raise ThemeCodeError(f'manifest.json: format {version} was made by a newer version of this software.')
    if version != ARCHIVE_FORMAT or isinstance(version, bool):
        raise ThemeCodeError(f'manifest.json: format must be {ARCHIVE_FORMAT}.')
    for key in ('name', 'base_theme'):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ThemeCodeError(f'manifest.json: {key} must be text.')

    found = ThemeArchive(name=manifest['name'].strip(), base_theme=manifest['base_theme'].strip(),
                         css=_text('styles.css', contents.get('styles.css', b'')))
    if 'scripts.js' in contents:
        found.script = _text('scripts.js', contents['scripts.js'])
        if '\x00' in found.script:
            raise ThemeCodeError('scripts.js: contains a NUL character.')
    for name, content in contents.items():
        if name.startswith('templates/'):
            template = name[len('templates/'):]
            try:
                found.templates[check_template_name(template)] = check_template_source(_text(name, content))
            except ThemeCodeError as error:
                raise ThemeCodeError(f'{name}: {error}') from None
        elif name.startswith('files/'):
            filename = name[len('files/'):]
            kind = sniff_type(content)
            if kind is None:
                raise ThemeCodeError(f'{name}: not an image or font a theme may hold.')
            if clean_filename(filename, kind[1]) != filename:
                raise ThemeCodeError(f'{name}: the name must end in .{kind[1]} and use only letters, digits, '
                                     f'".", "_" and "-".')
            found.files[filename] = content
    return found
