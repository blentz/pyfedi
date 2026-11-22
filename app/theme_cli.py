"""`flask themes import`: install a theme export (`<name>.piefed-theme.zip`) as a custom theme of one or more domains,
so a site can keep its themes in files and install them from a script.

The archive is read on standard input (raw, or base64 with `--base64`) and checked as the admin import checks it. On
each domain the theme is found by its name, ignoring case: a domain without one gets a new theme, and a domain with
one has its stylesheet, base theme, script, template overrides and files set to the archive's, each only where it
differs. A theme already as the archive has it is left alone, so running the import twice changes nothing the second
time. Retired themes stay retired. With `--default` the theme is also made each domain's default theme, as the admin's
site page does. Every domain is checked before anything is stored."""
import base64
import binascii
import json
import sys

import click
from flask import current_app

from app import cache, db
from app.branding import offsite_references
from app.clock import utcnow
from app.custom_themes import (ThemeNameError, add_theme_file, check_name, custom_identifier, is_shipped, read_stored,
                               remove_theme_files)
from app.models import CustomTheme, CustomThemeTemplate, SiteDomain
from app.theme_code import ThemeArchive, ThemeCodeError, read_archive
from app.utils import get_site_as_dict


class ThemeImportError(ValueError):
    """An archive or domain the import refuses; the message says which and why."""


def file_limits() -> dict:
    config = current_app.config
    return {'file_bytes': config['CUSTOM_THEME_MAX_FILE_BYTES'], 'theme_bytes': config['CUSTOM_THEME_MAX_BYTES'],
            'files': config['CUSTOM_THEME_MAX_FILES']}


def read_input(data: bytes, encoded: bool) -> ThemeArchive:
    """The checked theme `data` holds (a zip, or its base64 when `encoded`); ThemeImportError otherwise."""
    if encoded:
        try:
            data = base64.b64decode(data, validate=False)
        except (binascii.Error, ValueError):
            raise ThemeImportError('The input is not base64.') from None
    limit = current_app.config['CUSTOM_THEME_MAX_ARCHIVE_BYTES']
    if len(data) > limit:
        raise ThemeImportError(f'The archive is too big (at most {limit} bytes).')
    try:
        found = read_archive(data)
    except ThemeCodeError as error:
        raise ThemeImportError(str(error)) from None
    if found.base_theme != 'piefed' and not is_shipped(found.base_theme):
        raise ThemeImportError(f'The theme needs base theme {found.base_theme}, which this site does not have.')
    limits = file_limits()
    if len(found.files) > limits['files'] or sum(len(data) for data in found.files.values()) > limits['theme_bytes']:
        raise ThemeImportError('files/: more than a theme may hold.')
    return found


def target_domains(names: tuple[str, ...]) -> list[SiteDomain]:
    """The domains named (every domain when none is), by host name; ThemeImportError for one the site lacks."""
    query = db.session.query(SiteDomain).order_by(SiteDomain.domain)
    if not names:
        return query.all()
    wanted = {name.strip().lower() for name in names}
    rows = query.filter(SiteDomain.domain.in_(wanted)).all()
    missing = sorted(wanted - {row.domain for row in rows})
    if missing:
        raise ThemeImportError(f'No such domain: {", ".join(missing)}.')
    return rows


def existing_theme(found: ThemeArchive, domain: SiteDomain) -> CustomTheme | None:
    return db.session.query(CustomTheme).filter(CustomTheme.site_domain_id == domain.id,
                                                db.func.lower(CustomTheme.name) == found.name.lower()).first()


def _stored_files(theme: CustomTheme) -> dict:
    """`theme`'s files as {name: bytes}; a stored copy that cannot be read counts as different from any upload."""
    files = {}
    for file in theme.files:
        try:
            files[file.filename] = read_stored(file.path)
        except OSError:
            files[file.filename] = None
    return files


def differences(found: ThemeArchive, theme: CustomTheme | None) -> list[str]:
    """What the import would change on a domain whose theme of that name is `theme` (None: it has none)."""
    if theme is None:
        return ['new theme']
    changes = [part for part, same in (('styles.css', theme.css == found.css),
                                       ('base theme', theme.base_theme == found.base_theme),
                                       ('scripts.js', theme.script == found.script),
                                       ('templates', {t.name: t.source for t in theme.templates} == found.templates))
               if not same]
    if _stored_files(theme) != found.files:
        changes.append('files')
    return changes


def is_default_on(theme: CustomTheme | None, domain: SiteDomain) -> bool:
    """Whether `theme` is what domain `domain` has set itself as its default theme."""
    config = domain.config() if domain.is_default else domain.own_config
    return theme is not None and config is not None and config.default_theme == custom_identifier(theme.id)


def check_domain(found: ThemeArchive, domain: SiteDomain, make_default: bool) -> None:
    """Refuses a domain the theme cannot go on: off-site references on an onion, a name it may not take, or (to be
    its default) a retired theme of that name, which no picker offers."""
    if domain.is_onion:
        offsite = offsite_references(css=found.css, own_host=domain.domain)
        if offsite:
            raise ThemeImportError(f'{domain.domain}: styles.css: off the onion: {", ".join(offsite)}')
    theme = existing_theme(found, domain)
    if make_default and theme is not None and theme.retired:
        raise ThemeImportError(f'{domain.domain}: its theme {theme.name} is retired, so it cannot be the default.')
    if theme is None:
        try:
            check_name(found.name, domain.id)
        except ThemeNameError as error:
            raise ThemeImportError(f'{domain.domain}: {error}') from None


def apply(found: ThemeArchive, domain: SiteDomain) -> CustomTheme:
    """Make domain `domain`'s theme of the archive's name what the archive holds (checked by `check_domain`)."""
    theme = existing_theme(found, domain)
    now = utcnow()
    if theme is None:
        theme = CustomTheme(site_domain_id=domain.id, name=found.name, created_at=now)
        db.session.add(theme)
    theme.base_theme, theme.css, theme.script = found.base_theme, found.css, found.script
    theme.updated_at, theme.updated_by = now, None   # a new version: no worker keeps serving the old one
    theme.code_failed_template = theme.code_failure = theme.code_failed_at = None
    db.session.flush()
    stored = {template.name: template for template in theme.templates}
    for name, template in stored.items():
        if name not in found.templates:
            db.session.delete(template)
    for name, source in found.templates.items():
        if name in stored:
            if stored[name].source != source:
                stored[name].source, stored[name].updated_at = source, now
        else:
            db.session.add(CustomThemeTemplate(custom_theme_id=theme.id, name=name, source=source, updated_at=now))
    gone = [file for file in theme.files if file.filename not in found.files]
    gone_paths = [file.path for file in gone]
    for file in gone:
        theme.files.remove(file)
    db.session.commit()
    remove_theme_files(gone_paths)
    current = _stored_files(theme)
    for filename, content in found.files.items():
        if current.get(filename) != content:
            add_theme_file(theme, filename, content, True, file_limits())
    return theme


def make_default_on(theme: CustomTheme, domain: SiteDomain) -> None:
    """Make `theme` domain `domain`'s default theme, as the admin's site page saves it."""
    domain.config().default_theme = custom_identifier(theme.id)
    db.session.commit()   # the commit clears each domain's memoised configuration (app.domain_config)
    if domain.is_default:
        cache.delete_memoized(get_site_as_dict)


def import_theme(found: ThemeArchive, domains: list[SiteDomain], check: bool, make_default: bool = False) -> dict:
    """Install `found` on each of `domains`, and with `make_default` make it each one's default theme; with `check`,
    only say what would change. Every domain is checked before any is changed.
    Returns {"theme", "changed": {domain: [what]}, "unchanged": [domain]}."""
    for domain in domains:
        check_domain(found, domain, make_default)
    changed, unchanged = {}, []
    for domain in domains:
        theme = existing_theme(found, domain)
        what = differences(found, theme)
        if make_default and not is_default_on(theme, domain):
            what.append('default theme')
        if not what:
            unchanged.append(domain.domain)
            continue
        changed[domain.domain] = what
        if not check:
            theme = apply(found, domain) if what != ['default theme'] else theme
            if 'default theme' in what:
                make_default_on(theme, domain)
    return {'theme': found.name, 'changed': changed, 'unchanged': unchanged}


def register_theme_commands(app) -> None:
    @app.cli.group('themes')
    def themes():
        """Custom themes from exported archives, for keeping them in files."""

    @themes.command('import')
    @click.option('--domain', 'domains', multiple=True, help='A domain to install it on (repeatable; default: every '
                                                              'domain)')
    @click.option('--base64', 'encoded', is_flag=True, help='Standard input is the archive in base64')
    @click.option('--default', 'make_default', is_flag=True, help="Also make it each domain's default theme")
    @click.option('--check', is_flag=True, help='Say what would change, and change nothing')
    def import_(domains, encoded, make_default, check):
        """Install the theme archive on standard input as a custom theme of each domain, creating it or bringing it
        up to date by name (and with --default, making it the domain's default theme). Prints JSON: the theme's name, what changed on each domain, and the domains already up to
        date."""
        try:
            found = read_input(sys.stdin.buffer.read(), encoded)
            result = import_theme(found, target_domains(domains), check, make_default)
        except ThemeImportError as error:
            click.echo(str(error))
            sys.exit(1)
        click.echo(json.dumps(result, indent=1, ensure_ascii=False))
