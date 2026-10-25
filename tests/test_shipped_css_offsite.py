"""No stylesheet the app ships fetches from another host: the onion serves them all (project 4, Task 6)."""
import os
import re
from pathlib import Path

import pytest

from app.branding import offsite_references

APP = Path(__file__).resolve().parent.parent / 'app'
SHIPPED_CSS = sorted({path.resolve() for root in (APP / 'templates' / 'themes', APP / 'static')
                      for path in root.rglob('*.css')})


def test_shipped_css_is_found():
    assert any(path.name == 'base.css' and path.parent.name == 'hacker_bbs_shared' for path in SHIPPED_CSS)


@pytest.mark.parametrize('path', SHIPPED_CSS, ids=lambda path: str(path.relative_to(APP)))
def test_no_shipped_css_loads_offsite(path):
    assert offsite_references(css=path.read_text(encoding='utf-8', errors='replace')) == []


def test_hacker_bbs_fonts_resolve_and_carry_their_licence():
    base = APP / 'templates' / 'themes' / 'hacker_bbs_shared' / 'base.css'
    served_from = APP / 'static' / 'themes' / 'hacker_bbs_shared'  # the symlink the CSS is served through
    # Joined as a browser joins URLs (".." drops the last URL segment), not through the symlink's target.
    files = [Path(os.path.normpath(served_from / path)) for path in re.findall(r"url\('([^']+)'\)", base.read_text())
             if 'fonts/hacker_bbs/' in path]
    assert len(files) == 7
    for font in files:
        assert font.is_file(), font
        assert (font.parent / ('LICENSE.txt' if font.suffix == '.woff' else 'OFL.txt')).is_file(), font
