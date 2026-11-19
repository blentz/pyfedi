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
from tests.test_custom_themes_admin import themes_url, world  # noqa: F401  (world is a fixture)
from tests.test_custom_themes_files import LIMITS, PNG, no_stored_files_left  # noqa: F401  (autouse: stored files go)
from tests.two_domains import two_domains  # noqa: F401  (a fixture world needs)

pytestmark = pytest.mark.usefixtures('site')


def export(actor, domain, theme):
    return actor.post(themes_url(domain, f'/{theme.id}/export'))


def entries(response):
    archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
    return {info.filename: archive.read(info) for info in archive.infolist()}


def test_an_export_holds_the_whole_theme(app, world):
    night = world['night']
    add_theme_file(night, 'logo.png', PNG, False, LIMITS)
    night.script = 'go();'
    db.session.add(CustomThemeTemplate(custom_theme_id=night.id, name='post/_post_teaser.html', source='teaser'))
    db.session.commit()
    response = export(world['holder'], world['onion'], night)
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


def test_a_css_only_theme_exports_without_code_entries(world):
    found = entries(export(world['admin'], world['default'], world['day']))
    assert set(found) == {'manifest.json', 'styles.css'}


def test_a_stored_file_that_is_gone_is_left_out_and_named(app, world):
    day = world['day']
    added = add_theme_file(day, 'logo.png', PNG, False, LIMITS)
    os.unlink(f'app{added.path}')
    response = export(world['admin'], world['default'], day)
    assert response.status_code == 200 and 'files/logo.png' not in entries(response)
    page = world['admin'].get(themes_url(world['default'])).get_data(as_text=True)
    assert 'logo.png' in page and 'left out' in page


def test_export_needs_theme_holder_rights_and_the_owning_domain(world):
    assert export(world['holder'], world['default'], world['day']).status_code == 403
    assert world['admin'].post(themes_url(world['default'], f'/{world["night"].id}/export')).status_code == 404
    assert world['admin'].get(themes_url(world['default'], f'/{world["day"].id}/export')).status_code == 405


@pytest.mark.parametrize('name, expected', [('Night', 'night'), ('Dark & Moody!', 'dark-moody'), ('  ', 'theme'),
                                            ('Ünïcode ✓', 'n-code'), ('a' * 200, 'a' * 60)])
def test_archive_name(name, expected):
    assert archive_name(name) == f'{expected}.piefed-theme.zip'
