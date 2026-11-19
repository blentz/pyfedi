"""Export a custom theme as one zip (theme code plan, Task 6; spec §3.4). Any theme holder may export; it changes
nothing."""
import io
import json
import os
import zipfile

import pytest

from app import db
from app.custom_themes import add_theme_file
from app.models import CustomThemeTemplate, ModLog
from app.theme_code import archive_name
from tests.test_custom_themes_admin import themes_url
from tests.test_custom_themes_files import LIMITS, PNG, no_stored_files_left  # noqa: F401  (autouse: stored files go)

pytestmark = pytest.mark.usefixtures('site')


def export(actor, domain, theme):
    return actor.post(themes_url(domain, f'/{theme.id}/export'))


def entries(response):
    archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
    return {info.filename: archive.read(info) for info in archive.infolist()}


def test_an_export_holds_the_whole_theme(app, custom_themes_admin_world):
    night = custom_themes_admin_world['night']
    add_theme_file(night, 'logo.png', PNG, False, LIMITS)
    night.script = 'go();'
    db.session.add(CustomThemeTemplate(custom_theme_id=night.id, name='post/_post_teaser.html', source='teaser'))
    db.session.commit()
    response = export(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], night)
    assert response.status_code == 200 and response.mimetype == 'application/zip'
    assert 'attachment' in response.headers['Content-Disposition'] and 'night.piefed-theme.zip' in \
        response.headers['Content-Disposition']
    found = entries(response)
    manifest = json.loads(found.pop('manifest.json'))
    assert (manifest['format'], manifest['name'], manifest['base_theme'], manifest['software']) == \
        (1, 'Night', 'forest', 'piefed')
    assert found == {'styles.css': b'.night{}', 'files/logo.png': PNG, 'templates/post/_post_teaser.html': b'teaser',
                     'scripts.js': b'go();'}
    assert db.session.query(ModLog).count() == 0   # an export changes nothing and is not logged


def test_a_css_only_theme_exports_without_code_entries(custom_themes_admin_world):
    found = entries(export(custom_themes_admin_world['admin'], custom_themes_admin_world['default'], custom_themes_admin_world['day']))
    assert set(found) == {'manifest.json', 'styles.css'}


def test_a_stored_file_that_is_gone_is_left_out_and_named(app, custom_themes_admin_world):
    day = custom_themes_admin_world['day']
    added = add_theme_file(day, 'logo.png', PNG, False, LIMITS)
    os.unlink(f'app{added.path}')
    response = export(custom_themes_admin_world['admin'], custom_themes_admin_world['default'], day)
    assert response.status_code == 200 and 'files/logo.png' not in entries(response)
    page = custom_themes_admin_world['admin'].get(themes_url(custom_themes_admin_world['default'])).get_data(as_text=True)
    assert 'logo.png' in page and 'left out' in page


def test_export_needs_theme_holder_rights_and_the_owning_domain(custom_themes_admin_world):
    assert export(custom_themes_admin_world['holder'], custom_themes_admin_world['default'], custom_themes_admin_world['day']).status_code == 403
    assert custom_themes_admin_world['admin'].post(themes_url(custom_themes_admin_world['default'], f'/{custom_themes_admin_world["night"].id}/export')).status_code == 404
    assert custom_themes_admin_world['admin'].get(themes_url(custom_themes_admin_world['default'], f'/{custom_themes_admin_world["day"].id}/export')).status_code == 405


@pytest.mark.parametrize('name, expected', [('Night', 'night'), ('Dark & Moody!', 'dark-moody'), ('  ', 'theme'),
                                            ('Ünïcode ✓', 'n-code'), ('a' * 200, 'a' * 60)])
def test_archive_name(name, expected):
    assert archive_name(name) == f'{expected}.piefed-theme.zip'
