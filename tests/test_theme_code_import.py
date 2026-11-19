"""Import a theme from one zip (theme code plan, Task 7; spec §3.5).

Every check runs before anything is stored; each failure is a 400 naming the entry and the reason. An archive with
templates or a script needs `manage theme code` on the domain; one without needs only theme-holder rights."""
import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

from app import db
from app.models import CustomTheme, ModLog
from app.theme_code import ThemeCodeError, read_archive
from tests.conftest import forget_login
from tests.test_custom_themes_admin import BASE, csrf, themes_url
from tests.test_custom_themes_files import PNG, no_stored_files_left, stored_files  # noqa: F401  (autouse)

pytestmark = pytest.mark.usefixtures('site')
MANIFEST = {'format': 1, 'name': 'Imported', 'base_theme': 'forest', 'software': 'piefed'}


def make_zip(entries=None, manifest=MANIFEST, infos=(), css='.imported { color: red; }'):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        if manifest is not None:
            archive.writestr('manifest.json', json.dumps(manifest) if isinstance(manifest, dict) else manifest)
        archive.writestr('styles.css', css)
        for name, data in (entries or {}).items():
            archive.writestr(name, data)
        for info, data in infos:
            archive.writestr(info, data)
    return buffer.getvalue()


def post(actor, domain, data=None, filename='theme.zip'):
    form = {'csrf_token': csrf(actor.app, actor.client)}
    if data is not None:
        form['archive'] = (io.BytesIO(data), filename)
    forget_login()
    return actor.client.post(themes_url(domain, '/import'), data=form, base_url=BASE,
                             content_type='multipart/form-data')


def imported(domain):
    db.session.expire_all()
    return db.session.query(CustomTheme).filter(CustomTheme.site_domain_id == domain.id,
                                                CustomTheme.name.like('%Imported%')).all()


@pytest.fixture
def coder(custom_themes_admin_world):
    from tests.factories import grant_permission
    grant_permission(custom_themes_admin_world['admin'].user, 'manage theme code')
    return custom_themes_admin_world['admin']


# --- read_archive: what a zip may hold

def test_a_whole_archive_is_read(app):
    found = read_archive(make_zip({'files/logo.png': PNG, 'templates/index.html': 'home', 'scripts.js': 'go();'}))
    assert (found.name, found.base_theme, found.css) == ('Imported', 'forest', '.imported { color: red; }')
    assert found.files == {'logo.png': PNG} and found.templates == {'index.html': 'home'} and found.script == 'go();'
    assert found.has_code


def test_a_css_only_archive_has_no_code(app):
    found = read_archive(make_zip())
    assert not found.has_code and found.script is None and found.templates == {} and found.files == {}


def symlink():
    info = zipfile.ZipInfo('files/logo.png')
    info.external_attr = (0o120777 << 16)
    return info


def bzip2():
    info = zipfile.ZipInfo('files/logo.png')
    info.compress_type = zipfile.ZIP_BZIP2
    return info


@pytest.mark.parametrize('archive, said', [
    (lambda: b'not a zip at all', 'not a zip'),
    (lambda: make_zip({'../evil.css': 'x'}), '../evil.css'),
    (lambda: make_zip({'/abs.css': 'x'}), '/abs.css'),
    (lambda: make_zip({'files\\a.png': PNG}), 'files\\a.png'),
    (lambda: make_zip({'files/./a.png': PNG}), 'files/./a.png'),
    (lambda: make_zip({'files//a.png': PNG}), 'files//a.png'),
    (lambda: make_zip({'extra.txt': 'x'}), 'extra.txt'),
    (lambda: make_zip({'files/sub/a.png': PNG}), 'files/sub/a.png'),
    (lambda: make_zip({'styles.css': 'again'}), 'twice'),
    (lambda: make_zip(infos=[(symlink(), b'/etc/passwd')]), 'link'),
    (lambda: encrypted_zip(), 'encrypted'),
    (lambda: make_zip(infos=[(bzip2(), PNG)]), 'compression'),
    (lambda: make_zip(manifest=None), 'manifest.json'),
    (lambda: make_zip(manifest='{not json'), 'manifest.json'),
    (lambda: make_zip(manifest='[1]'), 'manifest.json'),
    (lambda: make_zip(manifest={**MANIFEST, 'format': 2}), 'newer version'),
    (lambda: make_zip(manifest={**MANIFEST, 'format': '1'}), 'format'),
    (lambda: make_zip(manifest={**MANIFEST, 'name': ''}), 'name'),
    (lambda: make_zip(manifest={**MANIFEST, 'base_theme': 7}), 'base_theme'),
    (lambda: make_zip(css=b'\xff\xfe not utf-8'), 'UTF-8'),
    (lambda: make_zip({'scripts.js': b'\xff\xfe not utf-8'}), 'UTF-8'),
    (lambda: make_zip({'scripts.js': 'a\x00b'}), 'NUL'),
    (lambda: make_zip({'templates/nope.html': 'x'}), 'nope.html'),
    (lambda: make_zip({'templates/index.html': '{% if %}'}), 'line 1'),
    (lambda: make_zip({'files/logo.png': b'<html>not an image</html>'}), 'logo.png'),
    (lambda: make_zip({'files/logo.jpg': PNG}), 'logo.jpg'),
])
@pytest.mark.filterwarnings('ignore:Duplicate name')   # the 'twice' case is made on purpose
def test_an_archive_that_cannot_be_used_is_refused_with_the_reason(app, archive, said):
    with pytest.raises(ThemeCodeError) as refused:
        read_archive(archive())
    assert said in str(refused.value)


def test_a_zip_bomb_stops_at_the_entry_limit_while_decompressing(app):
    bomb = make_zip({'files/logo.png': b'\x00' * (30 * 1024 * 1024)})   # 30 MB of zeros, about 30 KB zipped
    assert len(bomb) < 100 * 1024
    with pytest.raises(ThemeCodeError) as refused:
        read_archive(bomb)
    assert 'files/logo.png' in str(refused.value) and 'too big' in str(refused.value)


def test_the_whole_archive_is_limited_too(app, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_TEMPLATE_BYTES', 1000)
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_TEMPLATES', 2)
    with pytest.raises(ThemeCodeError) as refused:
        read_archive(make_zip({'templates/index.html': 'a', 'templates/about.html': 'b', 'templates/base.html': 'c'}))
    assert 'templates' in str(refused.value)


def test_entries_each_within_their_limit_can_still_be_too_much_together(app, monkeypatch):
    for key, value in (('CUSTOM_THEME_MAX_FILE_BYTES', 100_000), ('CUSTOM_THEME_MAX_BYTES', 1),
                       ('CUSTOM_THEME_MAX_CSS_BYTES', 1000), ('CUSTOM_THEME_MAX_SCRIPT_BYTES', 1),
                       ('CUSTOM_THEME_MAX_TEMPLATES', 0)):
        monkeypatch.setitem(app.config, key, value)   # altogether: the manifest's 64 KB and about 1 KB more
    with pytest.raises(ThemeCodeError) as refused:
        read_archive(make_zip({'files/a.png': b'x' * 90_000}))
    assert 'more than a theme may' in str(refused.value)


def test_the_number_of_entries_is_limited(app, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_FILES', 1)
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_TEMPLATES', 1)
    with pytest.raises(ThemeCodeError) as refused:
        read_archive(make_zip({f'files/a{n}.png': PNG for n in range(6)}))
    assert 'entries' in str(refused.value)


def patched(data: bytes, name: str, local: tuple, central: tuple) -> bytes:
    """`data` with entry `name`'s local header field at `local` = (offset, bytes) and central directory field at
    `central` replaced: what an archive made by other software could say, which zipfile itself will not write."""
    data = bytearray(data)
    for signature, (offset, value), name_at in ((b'PK\x03\x04', local, 30), (b'PK\x01\x02', central, 46)):
        at = data.find(signature)
        while at != -1:
            if bytes(data[at + name_at:at + name_at + len(name)]) == name.encode():
                data[at + offset:at + offset + len(value)] = value
            at = data.find(signature, at + 4)
    return bytes(data)


def understated(data: bytes, name: str, size: int) -> bytes:
    return patched(data, name, (22, struct.pack('<I', size)), (24, struct.pack('<I', size)))


def encrypted_zip() -> bytes:
    return patched(make_zip({'files/logo.png': PNG}), 'files/logo.png', (6, b'\x01\x00'), (8, b'\x01\x00'))


def test_header_sizes_are_not_trusted(app):
    """An entry whose headers claim less than it holds is refused as damaged, never read past its claim or its limit."""
    honest = make_zip({'scripts.js': 'x' * 50})
    assert zipfile.ZipFile(io.BytesIO(understated(honest, 'scripts.js', 5))).getinfo('scripts.js').file_size == 5
    with pytest.raises(ThemeCodeError) as refused:
        read_archive(understated(honest, 'scripts.js', 5))
    assert 'scripts.js' in str(refused.value)


@pytest.mark.parametrize('seed, said', [('negative_seek_header', 'damaged'), ('unknown_zip_version', 'not a zip')])
def test_what_the_fuzzer_found_is_refused(app, seed, said):
    """Found by the theme_archive fuzz campaign: zipfile raised ValueError (a negative seek) and NotImplementedError
    (an unknown zip version), not BadZipFile."""
    crafted = (Path(__file__).parent / 'fuzz/corpus/theme_archive' / seed).read_bytes()
    with pytest.raises(ThemeCodeError) as refused:
        read_archive(crafted)
    assert said in str(refused.value)


def test_a_directory_entry_is_ignored(app):
    found = read_archive(make_zip(infos=[(zipfile.ZipInfo('files/'), b'')]))
    assert found.files == {}


# --- the import route

def test_a_holder_imports_a_css_only_theme(custom_themes_admin_world):
    response = post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], make_zip({'files/logo.png': PNG}))
    assert response.status_code == 302
    [theme] = imported(custom_themes_admin_world['onion'])
    assert (theme.name, theme.base_theme, theme.css, theme.retired) == \
        ('Imported', 'forest', '.imported { color: red; }', False)
    assert [f.filename for f in theme.files] == ['logo.png'] and theme.updated_by == custom_themes_admin_world['holder'].user.id
    assert [(m.link_text, m.reason) for m in db.session.query(ModLog).filter_by(action='import_theme')] == \
        [('Imported', 'theme.zip')]


def test_code_needs_the_permission(custom_themes_admin_world):
    response = post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], make_zip({'scripts.js': 'go();'}))
    assert response.status_code == 403 and imported(custom_themes_admin_world['onion']) == []


def test_a_coder_imports_code(custom_themes_admin_world, coder):
    response = post(coder, custom_themes_admin_world['default'], make_zip({'templates/index.html': 'home', 'scripts.js': 'go();'}))
    assert response.status_code == 302
    [theme] = imported(custom_themes_admin_world['default'])
    assert theme.script == 'go();' and [(t.name, t.source) for t in theme.templates] == [('index.html', 'home')]
    assert [m.reason for m in db.session.query(ModLog).filter_by(action='import_theme')] == ['theme.zip, with code']


def test_a_taken_name_gets_the_copy_suffix(custom_themes_admin_world):
    post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], make_zip())
    post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], make_zip())
    assert sorted(t.name for t in imported(custom_themes_admin_world['onion'])) == ['Copy of Imported', 'Imported']


def test_a_base_theme_this_site_does_not_have_is_refused(custom_themes_admin_world):
    response = post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], make_zip(manifest={**MANIFEST, 'base_theme': 'gone-theme'}))
    assert response.status_code == 400 and 'gone-theme' in response.get_data(as_text=True)
    assert imported(custom_themes_admin_world['onion']) == []


def test_piefed_is_a_base_theme_every_site_has(custom_themes_admin_world):
    assert post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], make_zip(manifest={**MANIFEST, 'base_theme': 'piefed'})).status_code \
        == 302


def test_a_refused_archive_is_a_400_that_names_the_entry_and_stores_nothing(custom_themes_admin_world):
    before = stored_files()
    response = post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], make_zip({'files/logo.png': PNG, '../evil': 'x'}))
    assert response.status_code == 400 and '../evil' in response.get_data(as_text=True)
    assert imported(custom_themes_admin_world['onion']) == [] and stored_files() == before


def test_an_onion_import_refuses_off_site_css(custom_themes_admin_world):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(MANIFEST))
        archive.writestr('styles.css', 'body { background: url(https://elsewhere.example/x.png); }')
    response = post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], data.getvalue())
    assert response.status_code == 400 and 'elsewhere.example' in response.get_data(as_text=True)


def test_a_clearnet_import_with_off_site_css_is_taken_with_a_warning(custom_themes_admin_world):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(MANIFEST))
        archive.writestr('styles.css', 'body { background: url(https://elsewhere.example/x.png); }')
    response = post(custom_themes_admin_world['admin'], custom_themes_admin_world['default'], data.getvalue())
    assert response.status_code == 302 and len(imported(custom_themes_admin_world['default'])) == 1
    forget_login()
    page = custom_themes_admin_world['admin'].get(response.headers['Location']).get_data(as_text=True)
    assert 'elsewhere.example' in page


def test_too_many_files_or_too_large_files_for_the_theme_are_refused(app, custom_themes_admin_world, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_BYTES', len(PNG) + 1)
    response = post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], make_zip({'files/a.png': PNG, 'files/b.png': PNG}))
    assert response.status_code == 400 and imported(custom_themes_admin_world['onion']) == []


def test_an_archive_over_the_upload_limit_is_refused(app, custom_themes_admin_world, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_ARCHIVE_BYTES', 100)
    response = post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion'], make_zip({'files/a.png': PNG}))
    assert response.status_code == 400 and 'too big' in response.get_data(as_text=True)


def test_an_import_without_a_file_is_a_400(custom_themes_admin_world):
    assert post(custom_themes_admin_world['holder'], custom_themes_admin_world['onion']).status_code == 400


def test_import_needs_theme_holder_rights(custom_themes_admin_world):
    assert post(custom_themes_admin_world['holder'], custom_themes_admin_world['default'], make_zip()).status_code == 403


def test_an_export_imports_back_to_the_same_theme(app, custom_themes_admin_world, coder):
    from app.custom_themes import add_theme_file
    from app.models import CustomThemeTemplate
    from tests.test_custom_themes_files import LIMITS
    day = custom_themes_admin_world['day']
    add_theme_file(day, 'logo.png', PNG, False, LIMITS)
    day.script = 'go();'
    db.session.add(CustomThemeTemplate(custom_theme_id=day.id, name='about.html', source='about'))
    db.session.commit()
    exported = coder.post(themes_url(custom_themes_admin_world['default'], f'/{day.id}/export')).get_data()
    assert post(coder, custom_themes_admin_world['default'], exported).status_code == 302
    copy = db.session.query(CustomTheme).filter_by(name='Copy of Daylight').one()
    assert (copy.css, copy.base_theme, copy.script) == (day.css, day.base_theme, day.script)
    assert [(t.name, t.source) for t in copy.templates] == [('about.html', 'about')]
    assert [(f.filename, f.size) for f in copy.files] == [('logo.png', len(PNG))]

