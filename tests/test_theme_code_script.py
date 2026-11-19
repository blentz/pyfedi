"""Theme code: a custom theme's own scripts.js (theme code plan, Task 4; spec §3.3).

A page in a custom theme with a script loads it from /theme/<id>/scripts.js, with the page's nonce, instead of its
base theme's scripts.js; never both. A theme without a script keeps its base theme's."""
import re

import pytest

from app import db
from app.custom_themes import custom_identifier, theme_version
from app.login_gate import LAPSED_OPEN, OPEN
from tests.test_branding_pages import onion_branding, page, visit
from tests.test_custom_themes_render import make_theme

pytestmark = pytest.mark.usefixtures('site')
SCRIPT = 'document.documentElement.dataset.own = "yes";'


def script_theme(branded, script=SCRIPT, base_theme='forest'):
    theme = make_theme(branded.domains[1], base_theme=base_theme)
    theme.script = script
    db.session.commit()
    onion_branding(branded, default_theme=custom_identifier(theme.id))
    return theme


def script_path(theme):
    return f'/theme/{theme.id}/scripts.js?v={theme_version(theme)}'


def test_the_script_is_served_on_its_domain(app, branded):
    theme = script_theme(branded)
    response = visit(app, script_path(theme), onion=True)
    assert response.status_code == 200 and response.get_data(as_text=True) == SCRIPT
    assert response.mimetype == 'application/javascript'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert 'immutable' in response.headers['Cache-Control']
    assert 'max-age=60' in visit(app, f'/theme/{theme.id}/scripts.js?v=1', onion=True).headers['Cache-Control']


def test_the_script_is_not_found_elsewhere_or_without_one(app, branded):
    theme = script_theme(branded)
    assert visit(app, script_path(theme)).status_code == 404               # the first domain does not own it
    theme.script = None
    db.session.commit()
    assert visit(app, f'/theme/{theme.id}/scripts.js', onion=True).status_code == 404
    assert visit(app, '/theme/999999/scripts.js', onion=True).status_code == 404


def test_a_page_loads_the_themes_script_with_the_nonce_instead_of_the_base_themes(app, branded):
    theme = script_theme(branded, base_theme='high_contrast')
    home = page(app, '/', onion=True)
    assert re.search(rf'<script src="{re.escape(script_path(theme))}" nonce="[^"]+"></script>', home)
    assert 'themes/high_contrast/scripts.js' not in home


def test_a_theme_without_a_script_keeps_its_base_themes(app, branded):
    theme = script_theme(branded, script=None, base_theme='high_contrast')
    home = page(app, '/', onion=True)
    assert f'/theme/{theme.id}/scripts.js' not in home
    assert 'themes/high_contrast/scripts.js' in home


def test_the_login_gate_lets_the_script_through():
    assert 'custom_themes.script' in OPEN and 'custom_themes.script' in LAPSED_OPEN
