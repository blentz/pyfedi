"""Each domain's pages show its own branding (project 4, Task 2).

Pages read the Jinja value `branding`, the current domain's effective `Branding` (app/branding.py), never `g.site`'s
branding fields. The first domain's branding is `Site` row 1, so its pages do not change by a byte: the golden copies in
tests/golden/branding/ were captured from the code before this task (BASE 74e2e82f6), for one fixed fixture. Only what
varies per request or per checkout is masked (`stable`): the script nonce, a static file's mtime and the release.

A missing golden file is written from the served page and the test fails, so a capture is always deliberate: delete
the file, run once, review the diff and commit it."""
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from cachelib import SimpleCache
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf
from jinja2 import ChoiceLoader, DictLoader

from app import cache, db
from app.models import DomainBranding, Language, Site
from app.storefront import clear_domain_cache
from app.utils import current_theme
from tests.conftest import forget_login
from tests.factories import grant_permission
from tests.two_domains import HELL, ONION, onion_request, two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('site')

GOLDEN = Path(__file__).parent / 'golden' / 'branding'
TEMPLATES = Path(__file__).parent.parent / 'app' / 'templates'
PROXY = '10.89.0.2'
DEFAULT_BASE = f'https://{HELL}'
ONION_HEADERS = {'X-PieFed-Via': 'onion', 'X-Forwarded-For': '10.89.0.7'}
CREATED = datetime(2025, 3, 4, 5, 6, 7)

SITE_VALUES = dict(name='Golden Site', description='The golden tagline', logo='/static/media/golden_100.png',
                   logo_180='/static/media/golden_180.png', logo_152='/static/media/golden_152.png',
                   logo_32='/static/media/golden_32.png', logo_16='/static/media/golden_16.png',
                   default_theme='pyroclast', additional_css='.golden-site { color: gold; }',
                   sidebar_html='<p>The golden sidebar</p>', about_html='<p>The golden about</p>')
ONION_VALUES = dict(name='Onion Branded', description='The onion tagline', logo='/static/media/onion_logo.svg',
                    logo_180='/static/media/onion_180.png', logo_152='/static/media/onion_152.png',
                    logo_32='/static/media/onion_32.png', logo_16='/static/media/onion_16.png',
                    default_theme='forest', additional_css='.onion-own { color: purple; }',
                    sidebar='The onion sidebar', sidebar_html='<p>The onion sidebar</p>',
                    about_html='<p>The onion about</p>')

PAGES = {'index.html': '/', 'about.html': '/about', 'community.html': '/c/goldart',
         'keyboard_shortcuts.html': '/keyboard_shortcuts'}


@pytest.fixture
def branded(app, two_domains, monkeypatch):
    """The first domain's Site values, all fixed, an onion beside it, and a community on the first domain."""
    site = db.session.get(Site, 1)
    for field, value in SITE_VALUES.items():
        setattr(site, field, value)
    site.private_instance = False
    site.show_inoculation_block = False   # a random pick per request
    site.sidebar = 'The golden sidebar'
    site.about = 'The golden about'
    site.created_at = CREATED
    site.updated = CREATED
    site.last_active = CREATED
    db.session.commit()
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)
    default = two_domains.domains[0]
    community = two_domains.local_community('goldart', default)
    community.description = 'About *art*'
    community.description_html = '<p>About <em>art</em></p>'
    community.created_at = CREATED
    community.last_active = CREATED
    db.session.commit()
    clear_domain_cache()
    cache.clear()
    yield two_domains
    cache.clear()


def onion_branding(two_domains, **values):
    db.session.add(DomainBranding(site_domain_id=two_domains.domains[1].id, **values))
    db.session.commit()
    clear_domain_cache()


def log_in(client, user, base_url=None):
    with client.session_transaction(base_url=base_url) as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def visit(app, path, onion=False, user=None, client=None):
    forget_login()   # each visit is its own caller
    client = client or app.test_client()
    if not onion:
        if user is not None:
            log_in(client, user, DEFAULT_BASE)
        return client.get(path, base_url=DEFAULT_BASE)
    if user is not None:
        log_in(client, user, f'http://{ONION}')
    response = None
    for _ in range(3):   # a cookieless onion request is sent out and back for its session cookie (app/onion_guard.py)
        response = client.get(path, base_url=f'http://{ONION}', headers=ONION_HEADERS,
                              environ_base={'REMOTE_ADDR': PROXY})
        if response.status_code != 303:
            break
        path = response.headers['Location']
    return response


def page(app, path, **kwargs) -> str:
    response = visit(app, path, **kwargs)
    assert response.status_code == 200, (path, response.headers.get("Location"))
    return response.get_data(as_text=True)


def stable(app, body: str) -> str:
    """The page with what varies per request or per checkout masked: the nonce, the CSRF token, mtimes and the
    release."""
    token = re.search(r'<meta name="csrf-token" content="([^"]*)">', body)
    if token:
        body = body.replace(token.group(1), '{CSRF}')
    body = re.sub(r'nonce="[^"]*"', 'nonce="{NONCE}"', body)
    body = re.sub(r'nonce-[^\']*\'', "nonce-{NONCE}'", body)
    body = re.sub(r'changed=[0-9.]+', 'changed={MTIME}', body)
    return body.replace(app.config['VERSION'], '{VERSION}')


@pytest.mark.parametrize('name, path', sorted(PAGES.items()))
def test_first_domain_pages_unchanged(app, branded, name, path):
    onion_branding(branded, **ONION_VALUES)   # the onion's own branding changes nothing on the first domain
    body = stable(app, page(app, path))
    golden = GOLDEN / name
    if not golden.exists():
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(body)
        pytest.fail(f'captured {golden}; review it and run again')
    assert body == golden.read_text()


def test_onion_header_title_logo_sidebar_about_css_are_its_own(app, branded):
    onion_branding(branded, **ONION_VALUES)
    home = page(app, '/', onion=True)
    assert '<title>Onion Branded - The onion tagline</title>' in home
    assert '/static/media/onion_logo.svg' in home            # the header logo, an SVG, and og:image
    assert 'Onion Branded</a>' in home                       # the header's name
    for icon in ('onion_180.png', 'onion_152.png', 'onion_32.png', 'onion_16.png'):
        assert f'/static/media/{icon}' in home
    assert '.onion-own { color: purple; }' in home
    assert '<p>The onion sidebar</p>' in home
    assert '<meta name="description" content="The onion sidebar" />' in home   # from its own Markdown sidebar
    assert 'About Onion Branded' in home
    assert '<strong>The onion tagline</strong>' in home
    about = page(app, '/about', onion=True)
    assert 'About Onion Branded' in about
    assert '<p>The onion about</p>' in about
    for site_value in ('Golden Site', 'golden tagline', 'golden_', '<p>The golden sidebar</p>', 'golden about',
                       '.golden-site'):
        assert site_value not in home + about


def test_onion_without_own_branding_shows_the_first_domains(app, branded):
    home = page(app, '/', onion=True)
    assert '<title>Golden Site - The golden tagline</title>' in home
    assert '<p>The golden sidebar</p>' in home
    assert '.golden-site { color: gold; }' in home


def test_user_theme_beats_domain_theme(app, branded):
    onion_branding(branded, default_theme='forest')
    fan = branded.local_user('themefan', branded.domains[1])
    fan.theme = 'amethyst'
    db.session.commit()
    home = page(app, '/', onion=True, user=fan)
    assert 'themes/amethyst/styles.css' in home
    assert 'themes/forest/styles.css' not in home


def test_domain_theme_beats_site_theme_for_visitors(app, branded):
    onion_branding(branded, default_theme='forest')
    onion_home = page(app, '/', onion=True)
    assert 'themes/forest/styles.css' in onion_home
    assert 'themes/pyroclast/styles.css' not in onion_home
    assert 'themes/pyroclast/styles.css' in page(app, '/')


def test_a_domain_can_choose_the_piefed_theme(app, branded):
    """PieFed is the first theme the branding form offers. It has no theme directory, so it is never 'shipped', but a
    domain that picks it renders in it, over a site default of pyroclast."""
    onion_branding(branded, default_theme='piefed')
    with onion_request(app):
        g.site = db.session.get(Site, 1)
        assert current_theme() == 'piefed'
    onion_home = page(app, '/', onion=True)
    assert re.search(r'/static/themes/[^/"]+/styles\.css\?changed', onion_home) is None
    assert 'themes/pyroclast/styles.css' in page(app, '/')   # the first domain keeps the site default


def test_unknown_stored_theme_falls_back(app, branded):
    """Review Focus 3: a domain's stored theme no longer shipped renders with the next one in theme order, no 500."""
    onion_branding(branded, default_theme='gone-theme')
    home = page(app, '/', onion=True)
    assert 'gone-theme' not in home
    assert 'themes/pyroclast/styles.css' in home
    site = db.session.get(Site, 1)
    site.default_theme = 'also-gone'
    db.session.commit()
    clear_domain_cache()
    with onion_request(app):
        g.site = db.session.get(Site, 1)   # as before_request sets it
        assert current_theme() == 'piefed'
    for onion in (True, False):
        home = page(app, '/', onion=onion)
        assert 'also-gone' not in home
        assert 'gone-theme' not in home
        # piefed, the last in theme order, has no stylesheet of its own
        assert re.search(r'/static/themes/[^/"]+/styles\.css\?changed', home) is None


def test_no_template_reads_site_branding_directly():
    """A ratchet: pages and emails read `branding` (or `current_branding()` in a macro file), the domain's, never
    `g.site`'s or the bare `site` value's branding fields. An email renders under the recipient's home domain (Task 3),
    so its `branding` is that domain's."""
    fields = 'name|description|logo|logo_180|logo_152|logo_32|logo_16|default_theme|additional_css|sidebar|' \
             'sidebar_html|about|about_html'
    pattern = re.compile(rf'(?<![\w.])(g\.)?site\.({fields})\b')   # g.site and the bare `site` context value
    hits = [f'{path.relative_to(TEMPLATES)}:{number}: {line.strip()}'
            for path in sorted(TEMPLATES.rglob('*')) if path.is_file()
            for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1) if pattern.search(line)]
    assert hits == []


def test_a_macro_imported_without_context_reads_the_domains_branding(app, branded):
    """An imported macro file sees only Jinja globals, not context-processor values: `current_branding` is one."""
    onion_branding(branded, name='Onion Branded')
    env = app.jinja_env.overlay(loader=ChoiceLoader([DictLoader({
        'macro.html': '{% macro shop_name() %}{{ current_branding().name }}{% endmacro %}'}), app.jinja_env.loader]))
    with onion_request(app):
        assert env.from_string("{% from 'macro.html' import shop_name %}{{ shop_name() }}").render() == 'Onion Branded'
    macros = [TEMPLATES / 'post' / 'post_teaser' / '_macros.html',
              TEMPLATES / 'themes' / 'dillo' / 'post' / 'post_teaser' / '_macros.html']
    for path in macros:
        assert re.search(r'(?<![\w.])branding\.', path.read_text()) is None, path


# ---------------------------------------------------------------------------------------------------------------------
# A site-settings save shows on the next page load: the first domain's branding is memoised (app/branding.py), so each
# save of a Site branding field clears it.

@pytest.fixture
def real_cache(app, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    clear_domain_cache()


@pytest.fixture
def settings_admin(app, branded):
    admin = branded.local_user('settingsadmin', branded.domains[0])
    grant_permission(admin, 'change instance settings')
    db.session.add(Language(id=2, code='en', name='English'))
    db.session.commit()
    client = app.test_client()
    log_in(client, admin)
    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return client, token


def test_site_profile_save_shows_on_next_page_load(app, branded, real_cache, settings_admin):
    client, token = settings_admin
    assert 'Golden Site' in page(app, '/about')   # memoised now
    forget_login()
    with patch('app.admin.routes.render_template', return_value='rendered'):
        response = client.post('/admin/site', content_type='multipart/form-data',
                               data={'name': 'Renamed Site', 'description': 'A new tagline', 'about': 'New about',
                                     'sidebar': 'New sidebar', 'legal_information': '', 'tos_url': '',
                                     'announcement': '', 'contact_email': 'admin@example.com', 'submit': 'Save',
                                     'csrf_token': token, 'icon': (b'', '')})
    assert response.status_code == 200, response.headers.get('Location')
    about = page(app, '/about')
    assert 'About Renamed Site' in about
    assert 'New about' in about
    assert 'Golden Site' not in about


def test_site_misc_save_shows_on_next_page_load(app, branded, real_cache, settings_admin):
    client, token = settings_admin
    assert '.golden-site' in page(app, '/')   # memoised now
    forget_login()
    # No 'submit' key: SiteMiscForm and CloseInstanceForm both name their submit field 'submit'
    with patch('app.admin.routes.render_template', return_value='rendered'):
        response = client.post('/admin/misc', data={
            'csrf_token': token, 'redirect_policy': 'same_origin', 'allow_video_file_uploads': 'no',
            'allow_audio_file_uploads': 'no', 'default_filter': 'popular', 'default_theme': 'forest',
            'registration_mode': 'Open', 'language_id': '2', 'read_posts_cutoff': '180', 'nsfw_country_restriction': '',
            'auto_decline_countries': '', 'application_question': '', 'auto_decline_referrers': '',
            'ban_check_servers': '', 'registration_approved_email': '', 'additional_css': '.renamed { color: red; }',
            'additional_js': ''})
    assert response.status_code == 200, response.headers.get('Location')
    home = page(app, '/')
    assert '.renamed { color: red; }' in home
    assert '.golden-site' not in home
    assert 'themes/forest/styles.css' in home
