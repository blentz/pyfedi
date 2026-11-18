"""A custom theme's images and fonts (custom themes plan, Task 7; spec §3.7, Review Focus 4).

An upload is taken only for what its content is, never its name: PNG, JPEG, GIF, WebP, AVIF, SVG, WOFF2, WOFF, TTF,
OTF. It is stored as a random `.bin` name with the other local media, and served at /theme/<id>/files/<name> on the
theme's own domain with its checked type; an SVG with a sandbox policy, so opened by itself it runs nothing."""
import glob
import io
import logging
import os

import pytest
from PIL import Image

from app import db
from app.custom_themes import (SVG, THEME_MEDIA, ThemeFileError, add_theme_file, clean_filename, copy_theme, custom_identifier,
                               remove_theme_files, sniff_type)
from app.custom_themes_routes import SVG_POLICY
from app.models import CustomTheme, CustomThemeFile, ModLog, SiteDomain
from app.storefront import clear_domain_cache
from tests.test_branding_pages import visit
from tests.test_custom_themes_admin import Actor, themes_url, world  # noqa: F401  (world is a fixture)
from tests.factories import grant_permission
from tests.two_domains import two_domains  # noqa: F401  (a fixture world needs)

pytestmark = pytest.mark.usefixtures('site')
LIMITS = {'file_bytes': 2 * 1024 * 1024, 'theme_bytes': 10 * 1024 * 1024, 'files': 50}


def raster(kind: str) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (2, 2), (200, 30, 30)).save(buffer, format=kind)
    return buffer.getvalue()


PNG, JPEG, GIF, WEBP = raster('PNG'), raster('JPEG'), raster('GIF'), raster('WEBP')
AVIF = b'\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf'
SVG_WITH_SCRIPT = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><rect width="1" height="1"/></svg>'
FONTS = {'font/woff2': b'wOF2' + bytes(40), 'font/woff': b'wOFF' + bytes(40), 'font/otf': b'OTTO' + bytes(40),
         'font/ttf': b'\x00\x01\x00\x00' + bytes(40)}


@pytest.fixture(autouse=True)
def no_stored_files_left():
    """Each test's stored files go when it ends: the media directory is shared by the whole run."""
    before = set(glob.glob(os.path.join(THEME_MEDIA, '*')))
    yield
    for path in set(glob.glob(os.path.join(THEME_MEDIA, '*'))) - before:
        os.unlink(path)


def stored_files() -> set[str]:
    return set(glob.glob(os.path.join(THEME_MEDIA, '*.bin')))


@pytest.mark.parametrize('data, expected', [
    (PNG, ('image/png', 'png')), (JPEG, ('image/jpeg', 'jpg')), (GIF, ('image/gif', 'gif')),
    (WEBP, ('image/webp', 'webp')), (AVIF, ('image/avif', 'avif')), (SVG_WITH_SCRIPT, (SVG, 'svg')),
    (b'\xef\xbb\xbf<svg/>', (SVG, 'svg')),
    (FONTS['font/woff2'], ('font/woff2', 'woff2')), (FONTS['font/woff'], ('font/woff', 'woff')),
    (FONTS['font/otf'], ('font/otf', 'otf')), (FONTS['font/ttf'], ('font/ttf', 'ttf')),
    (b'true' + bytes(20), ('font/ttf', 'ttf')),
])
def test_sniff_type_takes_what_a_theme_may_hold(app, data, expected):
    assert sniff_type(data) == expected


@pytest.mark.parametrize('data', [
    b'', b'<html><script>alert(1)</script></html>', b'GIF89a' + bytes(10), PNG[:20],
    b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "y">]><svg>&x;</svg>', b'<svg><unclosed></svg>',
    b'<root xmlns="http://www.w3.org/2000/svg"/>', b'\xff\xfe<\x00s\x00', b'%PDF-1.7', b'RIFF\x00\x00\x00\x00WAVE',
    b'\x00\x00\x00\x1cftypisom',
])
def test_sniff_type_refuses_anything_else(app, data):
    assert sniff_type(data) is None


@pytest.mark.parametrize('name, extension, expected', [
    ('logo.png', 'png', 'logo.png'), ('logo.html', 'png', 'logo.png'), ('../../etc/passwd', 'png', 'passwd.png'),
    ('a/b.png', 'png', 'b.png'), ('C:\\x\\y z.PNG', 'png', 'y-z.png'), ('.htaccess', 'png', 'file.png'),
    ('', 'svg', 'file.svg'), ('noext', 'woff2', 'noext.woff2'), ('é ü.jpg', 'jpg', 'file.jpg'),
    ('x' * 300 + '.png', 'png', 'x' * 96 + '.png'), ('..a.b.png', 'png', 'a.b.png'),
])
def test_clean_filename(name, extension, expected):
    assert clean_filename(name, extension) == expected


def upload(world, theme, name, data, replace=False, domain=None):
    fields = {'file': (io.BytesIO(data), name)}
    if replace:
        fields['replace'] = '1'
    return world['holder'].post(themes_url(domain or world['onion'], f'/{theme.id}/files'), **fields)


def test_an_image_is_stored_and_served_as_what_it_is(app, world):
    night = world['night']
    before = stored_files()
    assert upload(world, night, 'sky.html', PNG).status_code == 302
    row = db.session.query(CustomThemeFile).filter_by(custom_theme_id=night.id).one()
    assert row.filename == 'sky.png' and row.mime_type == 'image/png' and row.size == len(PNG)
    assert row.path.startswith('/static/media/themes/') and row.path.endswith('.bin')
    assert len(stored_files() - before) == 1
    served = visit(app, f'/theme/{night.id}/files/sky.png', onion=True)
    assert served.status_code == 200
    assert served.mimetype == 'image/png' and served.get_data() == PNG
    assert served.headers['X-Content-Type-Options'] == 'nosniff'
    assert 'Content-Security-Policy' not in served.headers or 'sandbox' not in served.headers['Content-Security-Policy']
    assert db.session.query(ModLog).filter_by(action='add_theme_file').one().reason == 'sky.png'
    assert visit(app, f'/theme/{night.id}/files/sky.png').status_code == 404      # another domain
    assert visit(app, f'/theme/{night.id}/files/nope.png', onion=True).status_code == 404
    assert visit(app, '/theme/999999/files/sky.png', onion=True).status_code == 404


def test_html_named_as_an_image_is_refused(world):
    night = world['night']
    before = stored_files()
    upload(world, night, 'x.png', b'<html><script>alert(1)</script></html>')
    assert db.session.query(CustomThemeFile).count() == 0
    assert stored_files() == before


def test_an_svg_is_served_sandboxed(app, world):
    night = world['night']
    upload(world, night, 'icon.svg', SVG_WITH_SCRIPT)
    served = visit(app, f'/theme/{night.id}/files/icon.svg', onion=True)
    assert served.mimetype == SVG
    assert served.headers['Content-Security-Policy'] == SVG_POLICY
    assert served.headers['X-Content-Type-Options'] == 'nosniff'


def test_every_font_type_is_served_with_its_type(app, world):
    night = world['night']
    for mime, data in FONTS.items():
        upload(world, night, f'face-{mime.split("/")[1]}', data)
    for row in db.session.query(CustomThemeFile).filter_by(custom_theme_id=night.id):
        assert visit(app, f'/theme/{night.id}/files/{row.filename}', onion=True).mimetype == row.mime_type
    assert {row.mime_type for row in db.session.query(CustomThemeFile)} == set(FONTS)


def test_a_missing_upload_is_a_400(world):
    night = world['night']
    assert world['holder'].post(themes_url(world['onion'], f'/{night.id}/files')).status_code == 400
    assert upload(world, night, '', PNG).status_code == 400


@pytest.mark.parametrize('limit, value, uploads', [
    ('CUSTOM_THEME_MAX_FILE_BYTES', len(PNG) - 1, 1),
    ('CUSTOM_THEME_MAX_FILES', 2, 3),
    ('CUSTOM_THEME_MAX_BYTES', len(PNG) * 2 + len(PNG) // 2, 3),
])
def test_each_limit(app, world, monkeypatch, limit, value, uploads):
    monkeypatch.setitem(app.config, limit, value)
    night = world['night']
    for number in range(uploads):
        upload(world, night, f'n{number}.png', PNG)
    assert db.session.query(CustomThemeFile).count() == uploads - 1


def test_a_taken_name_needs_replace(world):
    night = world['night']
    upload(world, night, 'sky.png', PNG)
    first = db.session.query(CustomThemeFile).one()
    old_path = first.path
    upload(world, night, 'sky.png', PNG)       # refused: no replace
    db.session.expire_all()
    assert db.session.query(CustomThemeFile).one().path == old_path
    upload(world, night, 'sky.html', PNG, replace=True)   # same stored name, sky.png
    db.session.expire_all()
    replaced = db.session.query(CustomThemeFile).one()
    assert replaced.path != old_path and os.path.isfile(f'app{replaced.path}')
    assert not os.path.exists(f'app{old_path}')


def test_replacing_within_the_file_count(app, world, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_FILES', 1)
    night = world['night']
    upload(world, night, 'sky.png', PNG)
    upload(world, night, 'sky.png', PNG, replace=True)
    db.session.expire_all()
    assert db.session.query(CustomThemeFile).count() == 1


def test_removing_a_file_unlinks_it(world):
    night = world['night']
    upload(world, night, 'sky.png', PNG)
    row = db.session.query(CustomThemeFile).one()
    path = row.path
    world['holder'].post(themes_url(world['onion'], f'/{night.id}/files/{row.id}/delete'))
    assert db.session.query(CustomThemeFile).count() == 0
    assert not os.path.exists(f'app{path}')
    assert db.session.query(ModLog).filter_by(action='remove_theme_file').one().reason == 'sky.png'


def test_another_domains_file_is_out_of_reach(world):
    day, default = world['day'], world['default']
    world['admin'].post(themes_url(default, f'/{day.id}/files'), file=(io.BytesIO(PNG), 'sun.png'))
    row = db.session.query(CustomThemeFile).one()
    holder = world['holder']
    assert holder.post(themes_url(default, f'/{day.id}/files/{row.id}/delete')).status_code == 403
    assert holder.post(themes_url(world['onion'], f'/{day.id}/files/{row.id}/delete')).status_code == 404
    night = world['night']
    assert holder.post(themes_url(world['onion'], f'/{night.id}/files/{row.id}/delete')).status_code == 404
    assert upload(world, day, 'x.png', PNG, domain=default).status_code == 403
    assert db.session.query(CustomThemeFile).count() == 1


def test_the_edit_page_lists_files_with_their_css_reference(world):
    night = world['night']
    upload(world, night, 'sky.png', PNG)
    page = world['holder'].get(themes_url(world['onion'], f'/{night.id}/edit')).get_data(as_text=True)
    assert 'url(files/sky.png)' in page


def test_deleting_a_theme_unlinks_its_files(world):
    night = world['night']
    upload(world, night, 'sky.png', PNG)
    path = db.session.query(CustomThemeFile).one().path
    world['holder'].post(themes_url(world['onion'], f'/{night.id}/delete'))
    assert db.session.get(CustomTheme, night.id) is None
    assert not os.path.exists(f'app{path}')


def test_copying_a_theme_copies_its_files(app, world):
    night = world['night']
    upload(world, night, 'sky.png', PNG)
    original = db.session.query(CustomThemeFile).one()
    world['holder'].post(themes_url(world['onion'], '/copy'), source=custom_identifier(night.id))
    copy = db.session.query(CustomTheme).filter_by(name='Copy of Night').one()
    (copied,) = copy.files
    assert copied.filename == 'sky.png' and copied.path != original.path
    with open(f'app{copied.path}', 'rb') as file:
        assert file.read() == PNG
    world['holder'].post(themes_url(world['onion'], f'/{night.id}/delete'))   # the copy keeps its own
    assert os.path.isfile(f'app{copied.path}')


def test_copying_skips_a_file_gone_from_disk(app, world, caplog):
    night = world['night']
    upload(world, night, 'sky.png', PNG)
    os.unlink(f'app{db.session.query(CustomThemeFile).one().path}')
    with caplog.at_level(logging.WARNING):
        copy = copy_theme(custom_identifier(night.id), world['onion'].id, None)
    assert copy.files == []
    assert 'missing' in caplog.text


def test_a_file_gone_from_disk_is_not_found(app, world):
    night = world['night']
    upload(world, night, 'sky.png', PNG)
    os.unlink(f'app{db.session.query(CustomThemeFile).one().path}')
    assert visit(app, f'/theme/{night.id}/files/sky.png', onion=True).status_code == 404


def test_remove_theme_files_takes_only_unnamed_stored_files(app, world, tmp_path, caplog):
    night = world['night']
    upload(world, night, 'sky.png', PNG)
    named = db.session.query(CustomThemeFile).one().path
    os.makedirs(THEME_MEDIA, exist_ok=True)
    outside = tmp_path / 'victim.txt'
    outside.write_text('keep')
    link_name = 'ab' * 16 + '.bin'
    os.symlink(outside, os.path.join(THEME_MEDIA, link_name))
    stray = 'cd' * 16 + '.bin'
    with open(os.path.join(THEME_MEDIA, stray), 'wb') as file:
        file.write(b'x')
    with caplog.at_level(logging.INFO):
        remove_theme_files([named, f'/static/media/themes/{link_name}', '/static/media/logo_abcde.png',
                            '/static/media/themes/../themes/' + stray, None, f'/static/media/themes/{stray}',
                            f'/static/media/themes/{"ef" * 16}.bin'])   # already gone: nothing to do, nothing logged
    assert os.path.isfile(f'app{named}')                    # a row still names it
    assert outside.read_text() == 'keep'                    # a symlink is never followed
    assert os.path.islink(os.path.join(THEME_MEDIA, link_name))
    assert not os.path.exists(os.path.join(THEME_MEDIA, stray))   # the plain, unnamed one goes
    assert caplog.text.count('not removed') == 5


def test_add_theme_file_limits_directly(app, world):
    night = world['night']
    with pytest.raises(ThemeFileError, match='too big'):
        add_theme_file(night, 'a.png', PNG, False, {**LIMITS, 'file_bytes': 1})


def test_deleting_a_domain_unlinks_its_themes_files(app, world):
    manager = world['two_domains'].local_user('domainmanager', world['default'])
    grant_permission(manager, 'manage domains')
    grant_permission(manager, 'edit domain branding')
    spare = SiteDomain(domain='spare.example', scheme='https')
    db.session.add(spare)
    db.session.commit()
    clear_domain_cache()
    theme = CustomTheme(site_domain_id=spare.id, name='Spare', base_theme='forest')
    db.session.add(theme)
    db.session.commit()
    add_theme_file(theme, 'sky.png', PNG, False, LIMITS)
    path = db.session.query(CustomThemeFile).filter_by(custom_theme_id=theme.id).one().path
    response = Actor(app, manager).post(f'/admin/domains/{spare.id}/delete')
    assert response.status_code == 302
    assert db.session.get(SiteDomain, spare.id) is None
    assert not os.path.exists(f'app{path}')
