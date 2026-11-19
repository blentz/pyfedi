"""Theme code: template rules and the database loader (theme code plan, Task 2; spec §3.2, §3.3)."""
import pytest
from flask import current_app
from jinja2 import TemplateNotFound

from app import db
from app.models import CustomThemeTemplate
from app.theme_code import (ThemeCodeError, check_template_name, check_template_source, loader_name,
                            override_names)
from tests.test_custom_themes import make_theme
from tests.two_domains import two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('site')


def give(theme, templates):
    for name, source in templates.items():
        db.session.add(CustomThemeTemplate(custom_theme_id=theme.id, name=name, source=source))
    db.session.commit()
    return theme


@pytest.mark.parametrize('name', ['index.html', 'base.html', 'post/_post_teaser.html', '_side_pane.html'])
def test_an_app_template_name_is_accepted(app, name):
    assert check_template_name(name) == name


@pytest.mark.parametrize('name', ['', 'nope.html', 'themes/dillo/base.html', 'custom_theme/1/1/index.html',
                                  '../index.html', '/index.html', 'post\\_post_teaser.html', './index.html',
                                  'index.html ', 'INDEX.html', None, 7])
def test_any_other_name_is_refused(app, name):
    with pytest.raises(ThemeCodeError):
        check_template_name(name)


def test_a_template_that_does_not_parse_is_refused_with_its_line(app):
    with pytest.raises(ThemeCodeError) as refused:
        check_template_source('ok\n{% if %}\n')
    assert 'line 2' in str(refused.value)


def test_a_template_over_the_limit_is_refused(app, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_TEMPLATE_BYTES', 10)
    check_template_source('x' * 10)
    with pytest.raises(ThemeCodeError):
        check_template_source('x' * 11)


def test_a_template_that_is_not_text_is_refused(app):
    with pytest.raises(ThemeCodeError):
        check_template_source(b'{{ 1 }}')


def test_the_loader_serves_an_override_under_its_versioned_name(two_domains):
    theme = give(make_theme(two_domains.domains[0]), {'index.html': 'hello {{ 40 + 2 }}'})
    assert current_app.jinja_env.get_template(loader_name(theme, 'index.html')).render() == 'hello 42'


@pytest.mark.parametrize('name', ['custom_theme/{id}/1/index.html', 'custom_theme/{id}/{v}/base.html',
                                  'custom_theme/999999/{v}/index.html', 'custom_theme/{id}/{v}',
                                  'custom_theme/x/{v}/index.html', 'custom_theme/{id}/x/index.html'])
def test_the_loader_finds_nothing_for_a_stale_version_another_theme_or_a_missing_override(two_domains, name):
    theme = give(make_theme(two_domains.domains[0]), {'index.html': 'hello'})
    version = loader_name(theme, 'x').split('/')[2]
    with pytest.raises(TemplateNotFound):
        current_app.jinja_env.get_template(name.format(id=theme.id, v=version))


def test_the_app_loader_still_lists_and_serves_app_templates(app):
    assert 'index.html' in app.jinja_env.list_templates()
    assert not any(name.startswith('custom_theme/') for name in app.jinja_env.list_templates())


def test_an_include_in_an_override_finds_the_themes_own_override_first(two_domains):
    theme = give(make_theme(two_domains.domains[0], base_theme='dillo'),
                 {'index.html': "[{% include '_home_nav.html' %}]", '_home_nav.html': 'own nav'})
    assert current_app.jinja_env.get_template(loader_name(theme, 'index.html')).render() == '[own nav]'


def test_names_resolve_to_the_theme_then_its_base_theme_then_the_app(two_domains):
    theme = give(make_theme(two_domains.domains[0], base_theme='dillo'), {'_home_nav.html': 'own'})
    parent = loader_name(theme, 'index.html')
    join = current_app.jinja_env.join_path
    assert join('_home_nav.html', parent) == loader_name(theme, '_home_nav.html')
    assert join('_side_pane.html', parent) == 'themes/dillo/_side_pane.html'    # dillo ships one
    assert join('_inoculation_links.html', parent) == '_inoculation_links.html'
    assert join('themes/forest/x.html', parent) == 'themes/forest/x.html'        # an explicit name stays
    assert join('_home_nav.html', 'index.html') == '_home_nav.html'              # not inside an override
    assert join('_home_nav.html', 'custom_theme/999999/1/index.html') == '_home_nav.html'   # a gone theme
    assert join('_home_nav.html', None) == '_home_nav.html'


def test_an_override_names_of_a_theme_are_cached_by_version(two_domains):
    theme = give(make_theme(two_domains.domains[0]), {'index.html': 'a'})
    assert override_names(theme.id, loader_name(theme, 'x').split('/')[2]) == frozenset({'index.html'})
    assert override_names(999999, '1') is None


def test_an_override_removed_without_a_new_version_is_not_found(two_domains):
    theme = give(make_theme(two_domains.domains[0]), {'index.html': 'a', 'base.html': 'b'})
    name = loader_name(theme, 'index.html')
    override_names(theme.id, name.split('/')[2])          # the names are read and kept
    db.session.query(CustomThemeTemplate).filter_by(name='index.html').delete()
    db.session.commit()
    with pytest.raises(TemplateNotFound):
        current_app.jinja_env.get_template(name)
