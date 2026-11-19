"""Property check for theme archive import (app/theme_code.py read_archive; theme code spec §3.5).

An archive is an upload from anyone who may import a theme, possibly made elsewhere, so every byte of it is hostile.

* theme_archive: random bytes as an uploaded archive. No exception but ThemeCodeError; a theme that is read obeys
  every limit: each file a sniffed image or font under its clean name and size, each template an app template name
  that parses, sizes within their limits, and nothing outside manifest, stylesheet, script, files and templates.

App context (config and the Jinja environment); no database.
"""
from flask import current_app

from app.custom_themes import clean_filename, sniff_type
from app.theme_code import ThemeCodeError, app_template_names, read_archive
from tests.fuzz.targets._target import Target

MAX_INPUT = 256 * 1024


def check_theme_archive(data: bytes) -> None:
    try:
        found = read_archive(data[:MAX_INPUT])
    except ThemeCodeError:
        return
    config = current_app.config
    assert found.name.strip() == found.name and found.name and found.base_theme
    assert len(found.css.encode('utf-8')) <= config['CUSTOM_THEME_MAX_CSS_BYTES']
    assert found.script is None or ('\x00' not in found.script and
                                    len(found.script.encode('utf-8')) <= config['CUSTOM_THEME_MAX_SCRIPT_BYTES'])
    assert len(found.templates) <= config['CUSTOM_THEME_MAX_TEMPLATES']
    for name, source in found.templates.items():
        assert name in app_template_names(), name
        assert len(source.encode('utf-8')) <= config['CUSTOM_THEME_MAX_TEMPLATE_BYTES']
        current_app.jinja_env.parse(source)
    for name, content in found.files.items():
        kind = sniff_type(content)
        assert kind is not None and clean_filename(name, kind[1]) == name, name
        assert len(content) <= config['CUSTOM_THEME_MAX_FILE_BYTES']
    assert found.has_code == (bool(found.templates) or found.script is not None)


TARGETS = [
    Target(name='theme_archive', check=check_theme_archive, surface='theme_archives', budget_ms=None, needs_app=True),
]
