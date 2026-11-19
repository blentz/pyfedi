"""Theme code: pages render a code theme's overrides (theme code plan, Task 3; spec §3.3).

An override replaces the app's template of the same name wherever the page renders it: as the page itself, or as a
partial another template includes, extends or imports. A page that raises inside a code theme renders again without
theme code, and the failure is kept for the theme's holders."""
from datetime import timedelta

import pytest
from flask import current_app

from app import db
from app.custom_themes import custom_identifier
from app.models import Community, CustomTheme, CustomThemeTemplate
from app.theme_code import last_failure
from tests.test_branding_pages import branded, onion_branding, page  # noqa: F401  (branded is a fixture)
from tests.test_custom_themes_render import PIEFED_BODY, make_theme
from tests.two_domains import two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('site')


def code_theme(domain, templates, **kwargs):
    theme = make_theme(domain, **kwargs)
    db.session.add_all([CustomThemeTemplate(custom_theme_id=theme.id, name=name, source=source)
                        for name, source in templates.items()])
    db.session.commit()
    return theme


def onion_default(branded, theme):
    onion_branding(branded, default_theme=custom_identifier(theme.id))


def test_a_page_override_renders_on_its_domain_alone(app, branded):
    theme = code_theme(branded.domains[1], {'index.html': 'CODE THEME HOME'})
    onion_default(branded, theme)
    assert 'CODE THEME HOME' in page(app, '/', onion=True)
    assert 'CODE THEME HOME' not in page(app, '/')


def test_a_partial_override_reaches_the_apps_own_templates(app, branded):
    theme = code_theme(branded.domains[1], {'_home_nav.html': '<nav>OWN HOME NAV</nav>'})
    onion_default(branded, theme)
    home = page(app, '/', onion=True)
    assert 'OWN HOME NAV' in home and PIEFED_BODY in home      # the app's base and index, the theme's nav
    assert 'OWN HOME NAV' not in page(app, '/')


def test_an_override_can_extend_the_apps_template(app, branded):
    theme = code_theme(branded.domains[1], {
        'index.html': "{% extends 'base.html' %}{% block app_content %}EXTENDED HOME{% endblock %}"})
    onion_default(branded, theme)
    home = page(app, '/', onion=True)
    assert 'EXTENDED HOME' in home and PIEFED_BODY in home


def test_a_theme_without_that_override_renders_as_before(app, branded):
    theme = code_theme(branded.domains[1], {'about.html': 'CODE ABOUT'})
    onion_default(branded, theme)
    home = page(app, '/', onion=True)
    assert 'CODE ABOUT' not in home and PIEFED_BODY in home


def test_a_broken_override_falls_back_and_is_recorded(app, branded):
    theme = code_theme(branded.domains[1], {'_home_nav.html': '{{ 1 // 0 }}'})
    onion_default(branded, theme)
    home = page(app, '/', onion=True)
    assert PIEFED_BODY in home   # rendered again, without theme code
    failure = last_failure(theme.id)
    assert failure['template'] == 'index.html' and 'division' in failure['error']


def test_an_app_error_is_not_blamed_on_the_theme(app, branded, monkeypatch):
    """When the page fails without theme code too, the theme's override was not the cause."""
    theme = code_theme(branded.domains[1], {'_side_pane.html': 'pane'})
    onion_default(branded, theme)
    monkeypatch.setitem(app.config, 'PROPAGATE_EXCEPTIONS', False)
    original = current_app.jinja_env.get_or_select_template

    def broken(name, *args, **kwargs):
        if name == 'index.html':
            raise RuntimeError('app bug')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(current_app.jinja_env, 'get_or_select_template', broken)
    from tests.test_branding_pages import visit
    assert visit(app, '/', onion=True).status_code == 500
    assert last_failure(theme.id) is None


def test_a_communitys_code_theme_brings_only_its_stylesheet(app, branded):
    default = branded.domains[0]
    theme = code_theme(default, {'community/_community_nav.html': 'COMMUNITY PANE'})
    goldart = db.session.query(Community).filter_by(name='goldart').one()
    goldart.theme = custom_identifier(theme.id)
    db.session.commit()
    body = page(app, '/c/goldart')
    assert f'/theme/{theme.id}/styles.css' in body and 'COMMUNITY PANE' not in body


def test_last_failure_is_none_for_a_theme_that_never_failed_or_is_gone(app, branded):
    assert last_failure(123456) is None
    assert last_failure(make_theme(branded.domains[0]).id) is None


def test_a_failure_is_written_at_most_once_a_minute_and_shown_for_a_week(app, branded):
    theme = code_theme(branded.domains[1], {'_home_nav.html': '{{ 1 // 0 }}'})
    onion_default(branded, theme)
    page(app, '/', onion=True)
    first = last_failure(theme.id)['at']
    page(app, '/', onion=True)
    db.session.expire_all()
    assert last_failure(theme.id)['at'] == first          # throttled
    theme = db.session.get(CustomTheme, theme.id)
    theme.code_failed_at = first - timedelta(days=8)
    db.session.commit()
    assert last_failure(theme.id) is None                 # too old to show
