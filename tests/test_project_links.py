"""The admin sets where this server's source code, bug tracker and get-involved page are (app/project_links.py).

Owner rulings, 2026-11-12: three site-wide URLs on the site profile, each blank by default and then upstream PieFed's;
the source URL (the AGPL compliance link) is used everywhere the server names its source: the footer, nodeinfo 2.0's
PixelDroid metadata and 2.1's repository, and the Mastodon API's source_url. Nodeinfo's homepage stays PieFed's."""
import io
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from markupsafe import Markup
from sqlalchemy import inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import cache, db
from app.models import Site
from app.project_links import (UPSTREAM_BUG_REPORT, UPSTREAM_GET_INVOLVED, UPSTREAM_SOURCE, ProjectLinks,
                               footer_invitation, is_web_url, project_links)
from tests.conftest import forget_login
from tests.test_admin_site_profile import _payload, admin_client  # noqa: F401  (a fixture)
from tests.test_branding import load_migration

pytestmark = pytest.mark.usefixtures('site')

REPO = Path(__file__).parent.parent
FORK = 'https://git.example.org/fork/pyfedi'
BUGS = 'https://git.example.org/fork/pyfedi/issues'
INVOLVED = 'https://example.org/help-out'
UPSTREAM = ProjectLinks(UPSTREAM_SOURCE, UPSTREAM_BUG_REPORT, UPSTREAM_GET_INVOLVED)
INVITATION = ('Please <a href="https://codeberg.org/rimu/pyfedi/issues">report bugs</a> or '
              '<a href="https://join.piefed.social/get-involved/">get involved</a>.')


# ---------------------------------------------------------------------------------------------------------------------
# The links

def test_a_site_that_sets_none_names_upstream_piefed():
    assert project_links(Site(source_url='', bug_report_url=None)) == UPSTREAM


def test_a_site_names_its_own():
    links = project_links(Site(source_url=FORK, bug_report_url=BUGS, get_involved_url=INVOLVED))
    assert links == ProjectLinks(FORK, BUGS, INVOLVED)


@pytest.mark.parametrize('value, ok', [
    (FORK, True), ('http://example.org/', True), ('https://EXAMPLE.org:8443/x?y#z', True),
    ('', False), ('example.org/fork', False), ('javascript:alert(1)', False), ('ftp://example.org/', False),
    ('https://', False), ('https://exa mple.org/', False), ('https://[::1/', False), ('data:text/html,x', False),
], ids=['https', 'http', 'port-query-fragment', 'blank', 'no-scheme', 'javascript', 'ftp', 'no-host', 'space',
        'bad-ipv6', 'data'])
def test_a_link_is_an_http_or_https_url_with_a_host(value, ok):
    assert is_web_url(value) is ok


def test_the_invitation_points_at_the_links_and_escapes_them():
    links = ProjectLinks(FORK, 'https://example.org/"><script>x</script>', INVOLVED)
    written = str(footer_invitation(Markup(INVITATION), links))
    assert f'href="{INVOLVED}"' in written
    assert '<script>' not in written and 'https://example.org/&#34;&gt;&lt;script&gt;' in written
    assert footer_invitation(Markup(INVITATION), UPSTREAM) == Markup(INVITATION)


def po_entries(text: str):
    """(msgid, msgstr) of each entry of a .po file, its continuation lines joined."""
    def joined(part):
        return ''.join(re.findall(r'"((?:[^"\\]|\\.)*)"', part))
    for match in re.finditer(r'msgid ((?:"(?:[^"\\]|\\.)*"\s*)+)msgstr ((?:"(?:[^"\\]|\\.)*"\s*)+)', text):
        yield joined(match.group(1)), joined(match.group(2))


def test_every_translation_of_the_invitation_keeps_the_upstream_links():
    """footer_invitation finds the links by upstream's URLs, so a translation that changed them would keep pointing
    upstream: every translation must copy them as they are."""
    msgid = INVITATION.replace('"', '\\"')
    checked = 0
    for po in sorted((REPO / 'app' / 'translations').glob('*/LC_MESSAGES/messages.po')):
        for found, translated in po_entries(po.read_text()):
            if found == msgid and translated:
                assert UPSTREAM_BUG_REPORT in translated and UPSTREAM_GET_INVOLVED in translated, po
                checked += 1
    assert checked > 0


# ---------------------------------------------------------------------------------------------------------------------
# Where they show

@pytest.fixture
def public_site(app, site):
    site.private_instance = False
    site.show_inoculation_block = False
    db.session.commit()
    cache.clear()
    yield site
    cache.clear()


def set_links(site, **values):
    for field, value in values.items():
        setattr(site, field, value)
    db.session.commit()
    cache.clear()


def footer(app) -> str:
    forget_login()
    body = app.test_client().get('/keyboard_shortcuts').get_data(as_text=True)
    return body[body.rindex('<footer'):]


@pytest.mark.parametrize('theme', ['', 'dillo'])
def test_the_footer_names_upstream_by_default(app, public_site, theme):
    set_links(public_site, default_theme=theme)
    shown = footer(app)
    for url in UPSTREAM:
        assert f'href="{url}"' in shown


@pytest.mark.parametrize('theme', ['', 'dillo'])
def test_the_footer_names_the_sites_own_links(app, public_site, theme):
    set_links(public_site, default_theme=theme, source_url=FORK, bug_report_url=BUGS, get_involved_url=INVOLVED)
    shown = footer(app)
    for url in (FORK, BUGS, INVOLVED):
        assert f'href="{url}"' in shown
    for url in UPSTREAM:
        assert f'href="{url}"' not in shown


def test_nodeinfo_names_the_source_and_keeps_piefeds_homepage(app, public_site):
    set_links(public_site, source_url=FORK)
    software = app.test_client().get('/nodeinfo/2.1').get_json()['software']
    assert software['repository'] == FORK and software['homepage'] == 'https://join.piefed.social'
    cache.clear()
    metadata = app.test_client().get('/nodeinfo/2.0', headers={'User-Agent': 'PixelDroid'}).get_json()['metadata']
    assert metadata['software'] == {'homepage': 'https://join.piefed.social', 'repo': FORK}


def test_nodeinfo_names_upstream_by_default(app, public_site):
    assert app.test_client().get('/nodeinfo/2.1').get_json()['software']['repository'] == UPSTREAM_SOURCE


def test_the_mastodon_api_names_the_source(app, public_site):
    assert app.test_client().get('/api/v2/instance').get_json()['source_url'] == UPSTREAM_SOURCE
    set_links(public_site, source_url=FORK)
    assert app.test_client().get('/api/v2/instance').get_json()['source_url'] == FORK


# ---------------------------------------------------------------------------------------------------------------------
# The site profile sets them

def post_profile(client, token, **fields):
    """render_template patched as tests/test_admin_site_profile.py's _post does: admin/site.html renders
    form.csrf_token, which tests (WTF_CSRF_ENABLED False) don't have."""
    forget_login()
    with patch('app.admin.routes.render_template', return_value='rendered'):
        return client.post('/admin/site', data={**_payload(token, **fields), 'icon': (io.BytesIO(b''), '')},
                           content_type='multipart/form-data')


def site_row() -> Site:
    db.session.expire_all()
    return db.session.get(Site, 1)


def test_the_site_profile_saves_the_links(admin_client):
    client, token = admin_client
    response = post_profile(client, token, source_url=f'  {FORK} ', bug_report_url=BUGS, get_involved_url=INVOLVED)
    assert response.status_code == 200
    assert (site_row().source_url, site_row().bug_report_url, site_row().get_involved_url) == (FORK, BUGS, INVOLVED)
    post_profile(client, token, source_url='', bug_report_url='', get_involved_url='')
    assert project_links(site_row()) == UPSTREAM


@pytest.mark.parametrize('field', ['source_url', 'bug_report_url', 'get_involved_url'])
@pytest.mark.parametrize('value', ['javascript:alert(1)', 'example.org/fork', 'https://x.org/' + 'a' * 300],
                         ids=['javascript', 'no-scheme', 'too-long'])
def test_the_site_profile_refuses_a_link_that_is_not_a_web_url(admin_client, field, value):
    client, token = admin_client
    post_profile(client, token, **{field: value})
    assert not getattr(site_row(), field)


def test_the_site_profile_shows_the_links_and_upstreams_as_placeholders(admin_client):
    """admin/site.html renders form.csrf_token, which tests (WTF_CSRF_ENABLED False) don't have: read the form."""
    from app.admin.forms import SiteProfileForm
    text = (REPO / 'app' / 'templates' / 'admin' / 'site.html').read_text()
    for field in ('source_url', 'bug_report_url', 'get_involved_url'):
        assert f'render_field(form.{field})' in text
    placeholders = {field: SiteProfileForm.__dict__[field].kwargs['render_kw']['placeholder']
                    for field in ('source_url', 'bug_report_url', 'get_involved_url')}
    assert tuple(placeholders.values()) == UPSTREAM


# ---------------------------------------------------------------------------------------------------------------------
# The migration

def test_the_migration_adds_and_drops_the_link_columns(db_session):
    migration = load_migration('*_project_links.py')
    db.session.commit()
    names = {'source_url', 'bug_report_url', 'get_involved_url'}

    def columns():
        return {column['name'] for column in inspect(db.session.connection()).get_columns('site')}
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            migration.downgrade()
            assert names.isdisjoint(columns())
            migration.upgrade()
            assert names <= columns()
        db.session.commit()
    finally:
        db.session.rollback()
