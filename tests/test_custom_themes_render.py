"""Custom themes render on their own domain (custom themes plan, Task 2).

A page in a custom theme renders its base theme's templates and scripts.js with the custom theme's own stylesheet,
served at /theme/<id>/styles.css. A custom theme counts only on its own domain (CT3); elsewhere the next theme in
order takes over. Pages with no custom theme anywhere are checked byte for byte by tests/test_branding_pages.py."""
import re

import pytest
from flask import g

from app import db
from app.custom_themes import custom_identifier, theme_version
from app.login_gate import LAPSED_OPEN
from app.models import Community, CustomTheme, CustomThemeFile, Site
from app.storefront import clear_domain_cache
from app.utils import current_theme, template_theme, theme_label, theme_stylesheet_url
from tests.test_branding_pages import onion_branding, page, visit
from tests.two_domains import default_request, onion_request

pytestmark = pytest.mark.usefixtures('site')

DILLO_BODY = '<body class="">'            # dillo's base.html; PieFed's has class="d-flex flex-column ..."
PIEFED_BODY = 'class="d-flex flex-column'


def make_theme(domain, name='Dusk', base_theme='forest', css='.dusk { color: navy; }', retired=False):
    theme = CustomTheme(site_domain_id=domain.id, name=name, base_theme=base_theme, css=css, retired=retired)
    db.session.add(theme)
    db.session.commit()
    return theme


def stylesheet_path(theme):
    return f'/theme/{theme.id}/styles.css?v={theme_version(theme)}'


def test_a_domain_default_custom_theme_links_its_stylesheet(app, branded):
    onion = branded.domains[1]
    theme = make_theme(onion)
    onion_branding(branded, default_theme=custom_identifier(theme.id))
    home = page(app, '/', onion=True)
    assert stylesheet_path(theme) in home
    assert 'themes/forest/styles.css' not in home       # the copy replaces its base theme's stylesheet
    assert 'themes/pyroclast/styles.css' in page(app, '/')   # the first domain is untouched


def test_a_custom_theme_renders_its_base_themes_templates(app, branded):
    onion = branded.domains[1]
    theme = make_theme(onion, base_theme='dillo')
    onion_branding(branded, default_theme=custom_identifier(theme.id))
    home = page(app, '/', onion=True)
    assert DILLO_BODY in home and PIEFED_BODY not in home
    assert stylesheet_path(theme) in home
    assert 'themes/dillo/styles.css' not in home
    with onion_request(app):
        g.site = db.session.get(Site, 1)
        assert current_theme() == custom_identifier(theme.id)
        assert template_theme() == 'dillo'


def test_a_custom_theme_runs_its_base_themes_script(app, branded):
    onion = branded.domains[1]
    theme = make_theme(onion, base_theme='high_contrast')
    onion_branding(branded, default_theme=custom_identifier(theme.id))
    home = page(app, '/', onion=True)
    assert 'themes/high_contrast/scripts.js' in home
    assert stylesheet_path(theme) in home


def test_a_users_custom_theme_shows_only_on_its_domain(app, branded):
    """Review Focus 3: the user's pick stays stored, and shows only where it counts."""
    onion = branded.domains[1]
    theme = make_theme(onion)
    fan = branded.local_user('themefan', onion)
    fan.theme = custom_identifier(theme.id)
    db.session.commit()
    assert stylesheet_path(theme) in page(app, '/', onion=True, user=fan)
    first = page(app, '/', user=fan)
    assert f'/theme/{theme.id}/' not in first
    assert 'themes/pyroclast/styles.css' in first      # the first domain's default
    assert db.session.get(type(fan), fan.id).theme == custom_identifier(theme.id)


def test_a_users_stale_shipped_theme_is_returned_as_before(app, branded):
    """Only custom themes are checked for the user's pick: a stale shipped name behaves as it always did."""
    fan = branded.local_user('oldfan', branded.domains[0])
    fan.theme = 'gone-theme'
    db.session.commit()
    home = page(app, '/', user=fan)
    assert 'gone-theme' not in home
    assert re.search(r'/static/themes/[^/"]+/styles\.css\?changed', home) is None


def test_the_site_default_custom_theme_does_not_reach_another_domain(app, branded):
    default, _onion = branded.domains
    theme = make_theme(default)
    site = db.session.get(Site, 1)
    site.default_theme = custom_identifier(theme.id)
    db.session.commit()
    clear_domain_cache()
    assert stylesheet_path(theme) in page(app, '/')
    onion_home = page(app, '/', onion=True)
    assert f'/theme/{theme.id}/' not in onion_home
    assert re.search(r'/static/themes/[^/"]+/styles\.css\?changed', onion_home) is None   # piefed


def test_a_communitys_custom_theme_applies_on_its_domain_only(app, branded):
    default, onion = branded.domains
    own = make_theme(default, name='Gallery')
    other = make_theme(onion, name='Elsewhere')
    goldart = db.session.query(Community).filter_by(name='goldart').one()
    goldart.theme = custom_identifier(own.id)
    db.session.commit()
    body = page(app, '/c/goldart')
    assert stylesheet_path(own) in body
    assert 'Gallery' in body                       # the side pane names the theme, not its identifier
    assert custom_identifier(own.id) not in body
    goldart.theme = custom_identifier(other.id)
    db.session.commit()
    body = page(app, '/c/goldart')
    assert f'/theme/{other.id}/' not in body
    assert 'themes/pyroclast/styles.css' in body   # the viewer's theme instead
    assert 'Community theme' not in body


def test_a_custom_theme_whose_base_no_longer_ships_renders_in_piefed_templates(app, branded):
    onion = branded.domains[1]
    theme = make_theme(onion, base_theme='gone-theme')
    onion_branding(branded, default_theme=custom_identifier(theme.id))
    home = page(app, '/', onion=True)
    assert stylesheet_path(theme) in home
    assert PIEFED_BODY in home
    assert 'gone-theme' not in home
    with onion_request(app):
        g.site = db.session.get(Site, 1)
        assert template_theme() == 'piefed'


def test_helpers(app, branded):
    default, onion = branded.domains
    theme = make_theme(onion)
    with onion_request(app):
        assert theme_stylesheet_url(custom_identifier(theme.id)) == stylesheet_path(theme)
        assert theme_stylesheet_url('forest').startswith('/static/themes/forest/styles.css?changed=')
        assert theme_stylesheet_url('piefed') is None
        assert theme_stylesheet_url('hacker_bbs_shared') is None
        assert theme_stylesheet_url('') is None
        assert theme_label(custom_identifier(theme.id)) == 'Dusk'
        assert theme_label('forest') == 'forest'
    with default_request(app):
        assert theme_stylesheet_url(custom_identifier(theme.id)) is None
        assert theme_label(custom_identifier(theme.id)) == custom_identifier(theme.id)


def test_the_stylesheet_route(app, branded):
    onion = branded.domains[1]
    theme = make_theme(onion, css='.dusk { color: navy; }')
    response = visit(app, stylesheet_path(theme), onion=True)
    assert response.status_code == 200
    assert response.mimetype == 'text/css'
    assert response.get_data(as_text=True) == '.dusk { color: navy; }'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    stale = visit(app, f'/theme/{theme.id}/styles.css?v=1', onion=True)
    assert stale.status_code == 200
    assert stale.headers['Cache-Control'] == 'public, max-age=60'
    assert visit(app, f'/theme/{theme.id}/styles.css').status_code == 404      # another domain's
    assert visit(app, '/theme/999999/styles.css', onion=True).status_code == 404


def test_the_onion_never_serves_an_off_site_reference(app, branded):
    onion = branded.domains[1]
    theme = make_theme(onion, css='body { background: url(https://elsewhere.example/x.png); } .a { color: red; }')
    body = visit(app, stylesheet_path(theme), onion=True).get_data(as_text=True)
    assert 'elsewhere.example' not in body
    assert '.a { color: red; }' in body
    default_theme = make_theme(branded.domains[0], css='body { background: url(https://elsewhere.example/x.png); }')
    assert 'elsewhere.example' in visit(app, stylesheet_path(default_theme)).get_data(as_text=True)


def test_a_private_domains_theme_reaches_its_login_page(app, branded):
    """The login gate keeps a logged-out visitor to a short list of pages on a private domain. The login page is on
    it, so its theme's stylesheet and files are too, as /static is; so are a lapsed member's."""
    default = branded.domains[0]
    theme = make_theme(default, css='.private { color: navy; }')
    db.session.add(CustomThemeFile(custom_theme_id=theme.id, filename='a.png', path='/static/media/themes/x.bin',
                                   mime_type='image/png', size=1))
    site = db.session.get(Site, 1)
    site.private_instance = True
    db.session.commit()
    clear_domain_cache()
    assert visit(app, '/c/goldart').status_code != 200            # the gate is up
    assert visit(app, stylesheet_path(theme)).status_code == 200
    assert visit(app, f'/theme/{theme.id}/files/a.png').status_code == 404   # past the gate; no such stored file
    assert {'custom_themes.stylesheet', 'custom_themes.theme_file'} <= LAPSED_OPEN
