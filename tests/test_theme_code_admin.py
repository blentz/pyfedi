"""Theme code in the admin: overrides and the script (theme code plan, Task 5; spec §3.6).

Changing theme code needs `manage theme code` on the theme's domain as well as being one of its theme holders. Other
theme holders see what code a theme has, read-only. Every change is mod-logged, bumps the theme's version and clears
a recorded failure."""
from datetime import timedelta

import pytest

from app import db
from app.models import CustomTheme, CustomThemeTemplate, ModLog, Role, RolePermission
from app.clock import utcnow
from tests.factories import grant_permission, grant_role, limit_role
from tests.test_custom_themes_admin import Actor, themes_url

pytestmark = pytest.mark.usefixtures('site')
NEEDS = 'Changing theme code needs the manage theme code permission.'


@pytest.fixture
def coder(custom_themes_admin_world):
    grant_permission(custom_themes_admin_world['admin'].user, 'manage theme code')
    return custom_themes_admin_world['admin']


def url(world, theme, rest):
    domain = world['default'] if theme.site_domain_id == world['default'].id else world['onion']
    return themes_url(domain, f'/{theme.id}{rest}')


def reload(theme):
    db.session.expire_all()
    return db.session.get(CustomTheme, theme.id)


def logged(action):
    return [(entry.link_text, entry.reason) for entry in db.session.query(ModLog).filter_by(action=action)]


def test_a_coder_adds_and_replaces_an_override(custom_themes_admin_world, coder):
    day = custom_themes_admin_world['day']
    before = day.updated_at
    response = coder.post(url(custom_themes_admin_world, day, '/template'), name='index.html', source='<p>own home</p>')
    assert response.status_code == 302
    assert {t.name: t.source for t in reload(day).templates} == {'index.html': '<p>own home</p>'}
    assert reload(day).updated_at > before and reload(day).updated_by == coder.user.id
    coder.post(url(custom_themes_admin_world, day, '/template'), name='index.html', source='<p>again</p>')
    assert [t.source for t in reload(day).templates] == ['<p>again</p>']
    assert logged('edit_theme_template') == [('Daylight', 'index.html'), ('Daylight', 'index.html')]


def test_the_template_page_shows_an_override_to_edit(custom_themes_admin_world, coder):
    day = custom_themes_admin_world['day']
    coder.post(url(custom_themes_admin_world, day, '/template'), name='about.html', source='<b>about us</b>')
    page = coder.get(url(custom_themes_admin_world, day, '/template?name=about.html')).get_data(as_text=True)
    assert '&lt;b&gt;about us&lt;/b&gt;' in page and 'value="about.html"' in page
    assert coder.get(url(custom_themes_admin_world, day, '/template')).status_code == 200      # a new one


@pytest.mark.parametrize('name, source, said', [
    ('nope.html', 'x', 'is not a template this site has'),
    ('index.html', '{% if %}', 'does not parse, line 1'),
])
def test_a_bad_override_is_refused_with_the_reason(custom_themes_admin_world, coder, name, source, said):
    day = custom_themes_admin_world['day']
    response = coder.post(url(custom_themes_admin_world, day, '/template'), name=name, source=source)
    assert response.status_code == 302 and reload(day).templates == []
    assert said in coder.get(url(custom_themes_admin_world, day, '/edit')).get_data(as_text=True)


def test_the_number_of_overrides_is_limited(app, custom_themes_admin_world, coder, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_TEMPLATES', 1)
    day = custom_themes_admin_world['day']
    coder.post(url(custom_themes_admin_world, day, '/template'), name='index.html', source='a')
    coder.post(url(custom_themes_admin_world, day, '/template'), name='about.html', source='b')
    coder.post(url(custom_themes_admin_world, day, '/template'), name='index.html', source='c')   # replacing one is not adding one
    assert {t.name: t.source for t in reload(day).templates} == {'index.html': 'c'}


@pytest.mark.parametrize('data', [{}, {'name': 'index.html'}, {'source': 'x'}])
def test_a_template_post_without_its_fields_is_a_400(custom_themes_admin_world, coder, data):
    assert coder.post(url(custom_themes_admin_world, custom_themes_admin_world['day'], '/template'), **data).status_code == 400


def test_removing_an_override(custom_themes_admin_world, coder):
    day = custom_themes_admin_world['day']
    coder.post(url(custom_themes_admin_world, day, '/template'), name='index.html', source='a')
    assert coder.post(url(custom_themes_admin_world, day, '/template/remove'), name='index.html').status_code == 302
    assert reload(day).templates == []
    assert logged('remove_theme_template') == [('Daylight', 'index.html')]
    assert coder.post(url(custom_themes_admin_world, day, '/template/remove'), name='index.html').status_code == 404
    assert coder.post(url(custom_themes_admin_world, day, '/template/remove')).status_code == 400


def test_the_script(app, custom_themes_admin_world, coder, monkeypatch):
    day = custom_themes_admin_world['day']
    assert coder.post(url(custom_themes_admin_world, day, '/script'), script='console.log(1);\r\n').status_code == 302
    assert reload(day).script == 'console.log(1);\n'
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_SCRIPT_BYTES', 4)
    coder.post(url(custom_themes_admin_world, day, '/script'), script='too long')
    assert reload(day).script == 'console.log(1);\n'
    assert 'The script is over' in coder.get(url(custom_themes_admin_world, day, '/edit')).get_data(as_text=True)
    coder.post(url(custom_themes_admin_world, day, '/script'), script='a\x00')
    assert reload(day).script == 'console.log(1);\n'
    assert coder.post(url(custom_themes_admin_world, day, '/script/remove')).status_code == 302
    assert reload(day).script is None
    assert coder.post(url(custom_themes_admin_world, day, '/script/remove')).status_code == 404
    assert coder.post(url(custom_themes_admin_world, day, '/script')).status_code == 400
    assert coder.post(url(custom_themes_admin_world, day, '/script'), script='').status_code == 400
    assert logged('edit_theme_script') == [('Daylight', '')] and logged('remove_theme_script') == [('Daylight', '')]


def test_a_code_change_clears_the_recorded_failure(custom_themes_admin_world, coder):
    day = custom_themes_admin_world['day']
    day.code_failed_template, day.code_failure, day.code_failed_at = 'index.html', 'division by zero', utcnow()
    db.session.commit()
    assert 'division by zero' in coder.get(url(custom_themes_admin_world, day, '/edit')).get_data(as_text=True)
    coder.post(url(custom_themes_admin_world, day, '/template'), name='index.html', source='fixed')
    assert reload(day).code_failure is None and reload(day).code_failed_at is None


def test_an_old_failure_is_not_shown(custom_themes_admin_world, coder):
    day = custom_themes_admin_world['day']
    day.code_failed_template, day.code_failure = 'index.html', 'long ago'
    day.code_failed_at = utcnow() - timedelta(days=8)
    db.session.commit()
    assert 'long ago' not in coder.get(url(custom_themes_admin_world, day, '/edit')).get_data(as_text=True)


def test_a_coder_sees_the_code_forms(custom_themes_admin_world, coder):
    page = coder.get(url(custom_themes_admin_world, custom_themes_admin_world['day'], '/edit')).get_data(as_text=True)
    assert 'name="script"' in page and url(custom_themes_admin_world, custom_themes_admin_world['day'], '/template') in page and NEEDS not in page


def test_a_holder_without_the_permission_sees_code_read_only_and_cannot_change_it(custom_themes_admin_world):
    night = custom_themes_admin_world['night']
    night.script = 'x();'
    db.session.add(CustomThemeTemplate(custom_theme_id=night.id, name='index.html', source='own'))
    db.session.commit()
    holder = custom_themes_admin_world['holder']
    page = holder.get(url(custom_themes_admin_world, night, '/edit')).get_data(as_text=True)
    assert NEEDS in page and 'index.html' in page and 'name="script"' not in page
    for rest, data in (('/template', {'name': 'about.html', 'source': 'x'}), ('/template/remove', {'name': 'index.html'}),
                       ('/script', {'script': 'y();'}), ('/script/remove', {})):
        assert holder.post(url(custom_themes_admin_world, night, rest), **data).status_code == 403
    assert holder.get(url(custom_themes_admin_world, night, '/template')).status_code == 403
    assert reload(night).script == 'x();' and [t.name for t in reload(night).templates] == ['index.html']


def test_the_code_permission_alone_does_not_make_a_theme_holder(app, custom_themes_admin_world):
    """Theme code needs both: a holder of `manage theme code` on the onion who is not one of its theme holders."""
    user = custom_themes_admin_world['two_domains'].local_user('coderonly', custom_themes_admin_world['default'])
    role = Role(name='onion code only')
    db.session.add(role)
    db.session.commit()
    db.session.add(RolePermission(role_id=role.id, permission='manage theme code'))
    db.session.commit()
    limit_role(role, custom_themes_admin_world['onion'])
    grant_role(user, role)
    actor = Actor(app, user)
    assert actor.post(url(custom_themes_admin_world, custom_themes_admin_world['night'], '/script'), script='y();').status_code == 403


def test_another_domains_theme_is_not_found(custom_themes_admin_world, coder):
    assert coder.post(themes_url(custom_themes_admin_world['default'], f'/{custom_themes_admin_world["night"].id}/script'), script='x').status_code == 404
