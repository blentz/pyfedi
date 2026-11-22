"""`flask themes import`: a theme export installed as a custom theme of one or more domains, by name.

The archive comes on standard input (raw, or base64), is checked as the admin import checks it, and every domain is
checked before any is changed. A domain without a theme of that name gets one; a domain with one has it brought up to
date part by part; a domain already up to date is left alone, so a second run changes nothing."""
import base64
import io
import json
import os
import zipfile

import pytest

from app import cli, db
from app.custom_themes import add_theme_file
from app.models import CustomTheme, CustomThemeFile, CustomThemeTemplate, SiteDomain
from app.storefront import clear_domain_cache
from app.theme_cli import ThemeImportError, read_input
from tests.test_custom_themes_files import GIF, LIMITS, PNG, no_stored_files_left, stored_files  # noqa: F401  (autouse)

pytestmark = pytest.mark.usefixtures('site')
MANIFEST = {'format': 1, 'name': 'Brand', 'base_theme': 'piefed', 'software': 'piefed'}
CSS = ':root { --brand: teal; }'


def make_zip(css=CSS, manifest=None, entries=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest or MANIFEST))
        archive.writestr('styles.css', css)
        for name, data in (entries or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def domains(app):
    rows = [SiteDomain(domain='one.example', scheme='https', is_default=True),
            SiteDomain(domain='two.example', scheme='https'),
            SiteDomain(domain='abcdef.onion', scheme='http', is_onion=True)]
    db.session.add_all(rows)
    db.session.commit()
    clear_domain_cache()
    return {row.domain: row for row in rows}


@pytest.fixture
def run(app):
    cli.register(app)
    runner = app.test_cli_runner()

    def invoke(*args, input=None):
        return runner.invoke(args=['themes', 'import', *args], input=input)
    return invoke


def result_of(outcome):
    assert outcome.exit_code == 0, outcome.output
    return json.loads(outcome.output)


def themes_on(domain):
    db.session.expire_all()
    return db.session.query(CustomTheme).filter_by(site_domain_id=domain.id).all()


def test_a_new_theme_goes_on_every_domain(run, domains):
    result = result_of(run(input=make_zip()))
    assert result == {'theme': 'Brand', 'unchanged': [],
                      'changed': {name: ['new theme'] for name in ('abcdef.onion', 'one.example', 'two.example')}}
    for domain in domains.values():
        (theme,) = themes_on(domain)
        assert (theme.name, theme.base_theme, theme.css, theme.script, theme.retired) == \
            ('Brand', 'piefed', CSS, None, False)


def test_a_second_run_changes_nothing(run, domains):
    archive = make_zip(entries={'files/logo.png': PNG, 'templates/index.html': 'home', 'scripts.js': 'go();'})
    result_of(run(input=archive))
    before = {theme.id: theme.updated_at for theme in db.session.query(CustomTheme)}
    result = result_of(run(input=archive))
    assert result['changed'] == {} and result['unchanged'] == sorted(domains)
    db.session.expire_all()
    assert {theme.id: theme.updated_at for theme in db.session.query(CustomTheme)} == before


def test_only_the_domains_named(run, domains):
    result = result_of(run('--domain', 'TWO.example', input=make_zip()))
    assert result['changed'] == {'two.example': ['new theme']}
    assert themes_on(domains['one.example']) == [] and len(themes_on(domains['two.example'])) == 1


def test_an_unknown_domain_stores_nothing(run, domains):
    outcome = run('--domain', 'two.example', '--domain', 'nowhere.example', input=make_zip())
    assert outcome.exit_code == 1 and 'No such domain: nowhere.example.' in outcome.output
    assert db.session.query(CustomTheme).count() == 0


def test_base64_input(run, domains):
    result = result_of(run('--base64', '--domain', 'one.example', input=base64.b64encode(make_zip())))
    assert result['changed'] == {'one.example': ['new theme']}


def test_input_that_is_not_base64(run, domains):
    outcome = run('--base64', input=b'a')
    assert outcome.exit_code == 1 and 'The input is not base64.' in outcome.output


def test_check_says_what_would_change_and_changes_nothing(run, domains):
    result_of(run('--domain', 'one.example', input=make_zip()))
    result = result_of(run('--check', '--domain', 'one.example', '--domain', 'two.example',
                           input=make_zip(css='.new {}')))
    assert result['changed'] == {'one.example': ['styles.css'], 'two.example': ['new theme']}
    assert themes_on(domains['one.example'])[0].css == CSS and themes_on(domains['two.example']) == []


def test_an_existing_theme_is_brought_up_to_date_by_name(run, domains):
    one = domains['one.example']
    theme = CustomTheme(site_domain_id=one.id, name='BRAND', base_theme='forest', css='old', script='old();',
                        retired=True, code_failure='boom', code_failed_template='index.html')
    db.session.add(theme)
    db.session.commit()
    db.session.add_all([CustomThemeTemplate(custom_theme_id=theme.id, name='index.html', source='old home'),
                        CustomThemeTemplate(custom_theme_id=theme.id, name='base.html', source='same'),
                        CustomThemeTemplate(custom_theme_id=theme.id, name='auth/register.html', source='bye')])
    db.session.commit()
    add_theme_file(theme, 'logo.png', GIF, False, LIMITS)
    add_theme_file(theme, 'same.png', PNG, False, LIMITS)
    add_theme_file(theme, 'gone.png', PNG, False, LIMITS)
    gone_path = next(file.path for file in theme.files if file.filename == 'gone.png')
    archive = make_zip(entries={'files/logo.png': PNG, 'files/same.png': PNG, 'files/new.png': PNG,
                                'templates/index.html': 'home', 'templates/base.html': 'same',
                                'templates/auth/login.html': 'fresh'})
    result = result_of(run('--domain', 'one.example', input=archive))
    assert result['changed'] == {'one.example': ['styles.css', 'base theme', 'scripts.js', 'templates', 'files']}
    (stored,) = themes_on(one)
    assert stored.id == theme.id and stored.name == 'BRAND' and stored.retired   # its name and retirement stay
    assert (stored.base_theme, stored.css, stored.script, stored.code_failure, stored.code_failed_template) == \
        ('piefed', CSS, None, None, None)
    assert {t.name: t.source for t in stored.templates} == {'index.html': 'home', 'base.html': 'same',
                                                            'auth/login.html': 'fresh'}
    assert sorted(file.filename for file in stored.files) == ['logo.png', 'new.png', 'same.png']
    assert all(file.mime_type == 'image/png' for file in stored.files)
    assert not os.path.exists(f'app{gone_path}')
    assert result_of(run('--domain', 'one.example', input=archive))['changed'] == {}


def test_a_stored_file_gone_from_disk_is_stored_again(run, domains):
    archive = make_zip(entries={'files/logo.png': PNG})
    result_of(run('--domain', 'one.example', input=archive))
    path = db.session.query(CustomThemeFile).one().path
    os.unlink(f'app{path}')
    assert result_of(run('--domain', 'one.example', input=archive))['changed'] == {'one.example': ['files']}
    path = db.session.query(CustomThemeFile).one().path
    assert open(f'app{path}', 'rb').read() == PNG


def test_a_name_a_shipped_theme_has_is_refused(run, domains):
    outcome = run(input=make_zip(manifest={**MANIFEST, 'name': 'forest'}))
    assert outcome.exit_code == 1 and 'abcdef.onion: A shipped theme has that name.' in outcome.output
    assert db.session.query(CustomTheme).count() == 0


def test_off_site_references_are_refused_on_an_onion_before_anything_is_stored(run, domains):
    outcome = run(input=make_zip(css='@import url(https://fonts.example/a.css);'))
    assert outcome.exit_code == 1 and 'abcdef.onion: styles.css: off the onion: https://fonts.example/a.css' in \
        outcome.output
    assert db.session.query(CustomTheme).count() == 0
    result = result_of(run('--domain', 'one.example', input=make_zip(css='@import url(https://fonts.example/a.css);')))
    assert result['changed'] == {'one.example': ['new theme']}


def test_an_onion_takes_a_theme_without_off_site_references(run, domains):
    assert result_of(run('--domain', 'abcdef.onion', input=make_zip()))['changed'] == {'abcdef.onion': ['new theme']}


@pytest.mark.parametrize('archive, message', [
    (b'not a zip', 'The upload is not a zip archive.'),
    (make_zip(manifest={**MANIFEST, 'base_theme': 'nosuch'}), 'needs base theme nosuch'),
])
def test_a_bad_archive_is_refused(run, domains, archive, message):
    outcome = run(input=archive)
    assert outcome.exit_code == 1 and message in outcome.output
    assert db.session.query(CustomTheme).count() == 0


def test_a_shipped_base_theme_is_taken(run, domains):
    result_of(run('--domain', 'one.example', input=make_zip(manifest={**MANIFEST, 'base_theme': 'forest'})))
    assert themes_on(domains['one.example'])[0].base_theme == 'forest'


def test_an_archive_over_the_size_limit(app, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_ARCHIVE_BYTES', 10)
    with pytest.raises(ThemeImportError, match='too big'):
        read_input(make_zip(), False)


def test_more_files_than_a_theme_may_hold(app, monkeypatch):
    monkeypatch.setitem(app.config, 'CUSTOM_THEME_MAX_FILES', 1)
    with pytest.raises(ThemeImportError, match='more than a theme may hold'):
        read_input(make_zip(entries={'files/a.png': PNG, 'files/b.png': PNG}), False)


# --- --default: the theme is also each domain's default theme

def default_of(domain):
    db.session.expire_all()
    row = db.session.get(SiteDomain, domain.id)
    config = row.config() if row.is_default else row.own_config
    return config.default_theme if config is not None else None


def test_default_makes_a_new_theme_each_domains_default(run, domains):
    result = result_of(run('--default', '--domain', 'one.example', '--domain', 'two.example', input=make_zip()))
    assert result['changed'] == {'one.example': ['new theme', 'default theme'],
                                 'two.example': ['new theme', 'default theme']}
    for name in ('one.example', 'two.example'):
        (theme,) = themes_on(domains[name])
        assert default_of(domains[name]) == f'custom:{theme.id}'
    assert default_of(domains['abcdef.onion']) is None
    again = result_of(run('--default', '--domain', 'one.example', '--domain', 'two.example', input=make_zip()))
    assert again['changed'] == {} and again['unchanged'] == ['one.example', 'two.example']


def test_default_on_a_theme_already_up_to_date_only_sets_the_default(run, domains):
    result_of(run('--domain', 'two.example', input=make_zip()))
    (theme,) = themes_on(domains['two.example'])
    before = theme.updated_at
    result = result_of(run('--default', '--domain', 'two.example', input=make_zip()))
    assert result['changed'] == {'two.example': ['default theme']}
    assert default_of(domains['two.example']) == f'custom:{theme.id}'
    assert themes_on(domains['two.example'])[0].updated_at == before   # the theme itself was not saved again


def test_default_with_check_changes_nothing(run, domains):
    result_of(run('--domain', 'two.example', input=make_zip()))
    result = result_of(run('--default', '--check', '--domain', 'two.example', input=make_zip()))
    assert result['changed'] == {'two.example': ['default theme']}
    assert default_of(domains['two.example']) is None


def test_without_default_the_domains_default_is_left_alone(run, domains):
    result_of(run('--default', '--domain', 'two.example', input=make_zip()))
    db.session.get(SiteDomain, domains['two.example'].id).config().default_theme = 'forest'
    db.session.commit()
    assert result_of(run('--domain', 'two.example', input=make_zip()))['changed'] == {}
    assert default_of(domains['two.example']) == 'forest'


def test_a_retired_theme_cannot_be_made_the_default(run, domains):
    result_of(run('--domain', 'two.example', input=make_zip()))
    themes_on(domains['two.example'])[0].retired = True
    db.session.commit()
    outcome = run('--default', input=make_zip())
    assert outcome.exit_code == 1 and 'two.example: its theme Brand is retired' in outcome.output
    assert default_of(domains['two.example']) is None and themes_on(domains['one.example']) == []
