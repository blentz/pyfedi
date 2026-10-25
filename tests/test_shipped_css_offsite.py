"""No stylesheet the app ships fetches from another host: the onion serves them all (project 4, Task 6)."""
import hashlib
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


# SHA-256 of each font exactly as released: the six OFL families from google/fonts e7db6ca6 (ofl/<family>/), IBM VGA
# from int10h.org. They ship byte for byte; a conversion would be a modification (Reserved Font Names, CC BY-SA).
FONT_SHA256 = {
    'ShareTechMono-Regular.ttf': '9ceab1f87414829af259c0f537573ae03ef7dd3147c0b27a36a1a0beb6732677',
    'VT323-Regular.ttf': 'cf4de751ada78ceac033dbe16a687742939995b77bc2a052ae17a4957958594d',
    'PressStart2P-Regular.ttf': '034c77f1f05ec89421e4a63f0e3a4ca1ecf852cc6d2bf611f126f275728e017d',
    'Silkscreen-Regular.ttf': 'c845473330b94c2079ce9af01c51ac8ba2d99c24f4d14c039843bbb8e642ebd8',
    'MajorMonoDisplay-Regular.ttf': '3f2ee71c2b8464f065bf960a148be81d68fcc13385df87737928f948208dffc6',
    'CrimsonPro[wght].ttf': '16aa9fb7300a93637da51fac03a071b2ff08b6bbf65f99c794c25f040b58af6a',
    'web_ibm_vga_8x16.woff': '4823e093c7f2f69641eaafb6f3465185afa63877cda58d341b6b317fd53dfd1f',
}


def test_hacker_bbs_fonts_resolve_ship_unmodified_and_carry_their_licence():
    base = APP / 'templates' / 'themes' / 'hacker_bbs_shared' / 'base.css'
    served_from = APP / 'static' / 'themes' / 'hacker_bbs_shared'  # the symlink the CSS is served through
    # Joined as a browser joins URLs (".." drops the last URL segment), not through the symlink's target.
    files = [Path(os.path.normpath(served_from / path)) for path in re.findall(r"url\('([^']+)'\)", base.read_text())
             if 'fonts/hacker_bbs/' in path]
    assert sorted(font.name for font in files) == sorted(FONT_SHA256)
    for font in files:
        assert hashlib.sha256(font.read_bytes()).hexdigest() == FONT_SHA256[font.name], font
        assert (font.parent / ('LICENSE.txt' if font.suffix == '.woff' else 'OFL.txt')).is_file(), font
    assert not list((APP / 'static' / 'fonts' / 'hacker_bbs').rglob('*.woff2'))
