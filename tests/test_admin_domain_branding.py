"""Admins edit each domain's branding on the Domains page (project 4, Task 5; spec §3.4, §3.5).

`/admin/domains/<id>/branding` edits a sister domain's `DomainBranding` row; the first domain's branding is Site row
1's, edited at the site settings page. A blank field inherits the first domain's value again.

A logo upload makes the files the site logo upload makes (`save_logo_upload`), the 192 and 512 pixel manifest icons
included. Replacing or clearing a domain's logo removes only files that domain's own upload wrote: never a file Site's
logo or icon settings name, another domain's row names, or the form names.

Off-site references: a save of the onion's branding with one is refused, naming it, and writes nothing; anywhere else,
the site settings included, the save goes ahead with a warning naming each one.

Each test runs in a scratch directory holding its own `app/static/media` (and the real templates, for the themes), so
what an upload writes and what a guard removes is only ever this test's."""
import io
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from cachelib import SimpleCache
from flask import current_app
from PIL import Image

from app import cache, db
from app.branding import branding_for
from app.models import DomainBranding, Language, Site, SiteDomain
from app.storefront import clear_domain_cache, domain_for_host
from app.utils import get_setting, set_setting
from tests.conftest import forget_login
from tests.factories import grant_permission, make_instance, make_user
from tests.test_admin_site_profile import HOSTILE_SVG, SVG, csrf, login

pytestmark = pytest.mark.usefixtures('site')

REPO = Path(__file__).parent.parent
SHOP = 'shop.example.org'
ONION = 'abc.onion'
ELSEWHERE = 'https://elsewhere.example.org'
LOGO_FIELDS = ('logo', 'logo_180', 'logo_152', 'logo_32', 'logo_16', 'logo_192', 'logo_512')
TEXT_FIELDS = dict(name='Shop Branded', description='The shop tagline', default_theme='forest',
                   additional_css='.shop-own { color: teal; }', sidebar='The shop *sidebar*', about='The shop *about*')
BLANK_FIELDS = dict.fromkeys(TEXT_FIELDS, '')


def png(width=200, height=200) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), (10, 20, 30)).save(buffer, format='PNG')
    return buffer.getvalue()


@pytest.fixture
def media(tmp_path, monkeypatch):
    """A scratch working directory: an empty `app/static/media`, the real templates and PWA manifests (theme_list
    and /manifest.json read them by a relative path)."""
    directory = tmp_path / 'app' / 'static' / 'media'
    directory.mkdir(parents=True)
    (tmp_path / 'app' / 'templates').symlink_to(REPO / 'app' / 'templates')
    (tmp_path / 'app' / 'static' / 'pwa_manifests').symlink_to(REPO / 'app' / 'static' / 'pwa_manifests')
    monkeypatch.chdir(tmp_path)
    return directory


@pytest.fixture
def domains(app, db_session, media):
    """The first domain at SERVER_NAME, a clearnet sister and the onion. Site row 1 is open to visitors."""
    first = current_app.config['SERVER_NAME']
    instance = make_instance(first, software='piefed')
    make_user(instance, 'founder', local=True)   # user 1, who passes every permission check
    db.session.add(SiteDomain(domain=first, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=SHOP, scheme='https', federates=False))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.show_inoculation_block = False
    site.name = 'The First Site'
    site.about_html = '<p>The first about</p>'
    db.session.commit()
    clear_domain_cache()
    yield {'first': domain_for_host(first), 'shop': domain_for_host(SHOP), 'onion': domain_for_host(ONION),
           'instance': instance}
    clear_domain_cache()


@pytest.fixture
def admin(app, domains):
    user = make_user(domains['instance'], 'brandadmin', local=True)
    user.verified = True
    db.session.commit()
    grant_permission(user, 'change instance settings')
    client = app.test_client()
    login(client, user)
    return client, csrf(app, client)


def save(admin, domain, logo=None, **fields):
    client, token = admin
    data = dict(TEXT_FIELDS, csrf_token=token, submit='Save')
    data.update(fields)
    data['logo'] = logo if logo is not None else (io.BytesIO(b''), '')
    forget_login()
    return client.post(f'/admin/domains/{domain.id}/branding', data=data, content_type='multipart/form-data')


def flashes(admin) -> list:
    with admin[0].session_transaction() as session:
        return session.pop('_flashes', [])


def own_row(domain) -> DomainBranding | None:
    db.session.expire_all()
    return db.session.get(DomainBranding, domain.id)


def media_files(media) -> set[str]:
    return {path.name for path in media.iterdir()}


def page(app, path, host=SHOP) -> str:
    forget_login()
    response = app.test_client().get(path, base_url=f'https://{host}')
    assert response.status_code == 200, (host, path, response.status_code)
    return response.get_data(as_text=True)


def write_media(media, *names):
    for name in names:
        (media / name).write_bytes(b'already here')


# ---------------------------------------------------------------------------------------------------------------------
# Who may, and which domain

def test_non_admin_403(app, domains):
    ordinary = make_user(domains['instance'], 'ordinary', local=True)
    ordinary.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, ordinary)
    assert client.get(f"/admin/domains/{domains['shop'].id}/branding").status_code == 403
    response = save((client, csrf(app, client)), domains['shop'], name='Not allowed')
    assert response.status_code == 403
    assert own_row(domains['shop']) is None


def test_default_row_redirects_to_site_settings(admin, domains):
    response = admin[0].get(f"/admin/domains/{domains['first'].id}/branding")
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin/site')
    response = save(admin, domains['first'], name='Never stored')
    assert response.status_code == 302 and response.headers['Location'].endswith('/admin/site')
    assert db.session.query(DomainBranding).count() == 0


def test_unknown_domain_is_404(admin, domains):
    assert admin[0].get('/admin/domains/999/branding').status_code == 404


def test_the_domains_page_links_each_sister_to_its_branding(admin, domains):
    body = admin[0].get('/admin/domains').get_data(as_text=True)
    assert f"/admin/domains/{domains['shop'].id}/branding" in body
    assert f"/admin/domains/{domains['onion'].id}/branding" in body
    assert f"/admin/domains/{domains['first'].id}/branding" not in body


# ---------------------------------------------------------------------------------------------------------------------
# Setting and clearing

def test_admin_sets_and_clears_each_field(app, admin, domains, media):
    shop = domains['shop']
    response = save(admin, shop, logo=(io.BytesIO(png()), 'shop.png'))
    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/admin/domains/{shop.id}/branding')
    row = own_row(shop)
    assert (row.name, row.description, row.default_theme, row.additional_css) == \
        ('Shop Branded', 'The shop tagline', 'forest', '.shop-own { color: teal; }')
    # each Markdown source is written with the HTML made from it, never one without the other
    assert (row.sidebar, row.sidebar_html) == ('The shop *sidebar*', '<p>The shop <em>sidebar</em></p>\n')
    assert (row.about, row.about_html) == ('The shop *about*', '<p>The shop <em>about</em></p>\n')
    logos = {field: getattr(row, field) for field in LOGO_FIELDS}
    assert all(path.startswith('/static/media/logo_') for path in logos.values()), logos
    assert {Path(path).name for path in logos.values()} == media_files(media)   # the original is not kept
    branding = branding_for(shop)
    assert (branding.name, branding.logo_180, branding.sidebar_html) == \
        ('Shop Branded', logos['logo_180'], row.sidebar_html)

    # A form showing the domain's own values
    body = admin[0].get(f'/admin/domains/{shop.id}/branding').get_data(as_text=True)
    assert 'Shop Branded' in body and 'The shop *sidebar*' in body and logos['logo'] in body

    response = save(admin, shop, remove_logo='y', **BLANK_FIELDS)
    assert response.status_code == 302
    row = own_row(shop)
    assert all(getattr(row, field) == '' for field in list(TEXT_FIELDS) + list(LOGO_FIELDS))
    assert (row.sidebar_html, row.about_html) == ('', '')
    assert media_files(media) == set()   # the domain's own logo files are gone with its logo
    site = db.session.get(Site, 1)
    assert branding_for(shop) == branding_for(domains['first'])   # inherits every field again
    assert branding_for(shop).name == site.name


def test_a_field_cleared_alone_clears_its_html_too(admin, domains):
    shop = domains['shop']
    save(admin, shop)
    save(admin, shop, sidebar='')
    row = own_row(shop)
    assert (row.sidebar, row.sidebar_html) == ('', '')
    assert (row.about, row.about_html) == ('The shop *about*', '<p>The shop <em>about</em></p>\n')
    save(admin, shop, about='')
    row = own_row(shop)
    assert (row.about, row.about_html) == ('', '')


def test_a_save_without_an_upload_keeps_the_logo(admin, domains, media):
    shop = domains['shop']
    save(admin, shop, logo=(io.BytesIO(png()), 'shop.png'))
    before = {field: getattr(own_row(shop), field) for field in LOGO_FIELDS}
    files = media_files(media)
    save(admin, shop, name='Renamed')
    row = own_row(shop)
    assert row.name == 'Renamed'
    assert {field: getattr(row, field) for field in LOGO_FIELDS} == before
    assert media_files(media) == files


def test_a_domain_with_no_row_shows_an_empty_form(admin, domains):
    response = admin[0].get(f"/admin/domains/{domains['shop'].id}/branding")
    assert response.status_code == 200
    assert SHOP in response.get_data(as_text=True)
    assert own_row(domains['shop']) is None


def test_an_invalid_form_writes_nothing(admin, domains):
    response = save(admin, domains['shop'], default_theme='no-such-theme', name='x' * 257)
    assert response.status_code == 200
    assert own_row(domains['shop']) is None


def test_saved_branding_shows_on_next_load(app, admin, domains, monkeypatch):
    """The branding memo is shared by every worker (app/branding.py): a save clears it, so the next page load on that
    domain shows the change."""
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    clear_domain_cache()
    shop = domains['shop']
    about = page(app, '/about')   # memoised now
    assert 'The First Site' in about and 'Shop Branded' not in about
    assert save(admin, shop).status_code == 302
    about = page(app, '/about')
    assert 'Shop Branded' in about and 'The shop <em>about</em>' in about
    assert 'themes/forest/styles.css' in page(app, '/')
    assert 'Shop Branded' not in page(app, '/about', host=current_app.config['SERVER_NAME'])
    save(admin, shop, name='')
    assert 'About The First Site' in page(app, '/about')


# ---------------------------------------------------------------------------------------------------------------------
# Uploads

@pytest.mark.parametrize('logo', [
    pytest.param(lambda: (io.BytesIO(b'just text'), 'notes.txt'), id='not an image type'),
    pytest.param(lambda: (io.BytesIO(b'GIF89a'), 'anim.gif'), id='a gif'),
    pytest.param(lambda: (io.BytesIO(b'\x89PNG\r\n\x1a\nnot really'), 'broken.png'), id='cannot be decoded'),
    pytest.param(lambda: (io.BytesIO(png()[:60]), 'truncated.png'), id='truncated'),
    pytest.param(lambda: (io.BytesIO(HOSTILE_SVG), 'hostile.svg'), id='an svg that cannot be sanitized'),
])
def test_bad_upload_is_400_not_500(admin, domains, media, logo):
    response = save(admin, domains['shop'], logo=logo())
    assert response.status_code == 400
    assert own_row(domains['shop']) is None
    assert media_files(media) == set()


def test_a_bad_upload_leaves_the_existing_logo_alone(admin, domains, media):
    shop = domains['shop']
    save(admin, shop, logo=(io.BytesIO(png()), 'shop.png'))
    before = {field: getattr(own_row(shop), field) for field in LOGO_FIELDS}
    files = media_files(media)
    assert save(admin, shop, name='Never stored', logo=(io.BytesIO(b'nope'), 'x.png')).status_code == 400
    row = own_row(shop)
    assert row.name == 'Shop Branded'
    assert {field: getattr(row, field) for field in LOGO_FIELDS} == before
    assert media_files(media) == files


def manifest_icons(app, host=SHOP) -> dict:
    forget_login()
    document = app.test_client().get('/manifest.json', base_url=f'https://{host}').get_json()
    return {icon['sizes']: (icon['src'], icon['type']) for icon in document['icons']}


def test_a_png_upload_makes_the_manifest_icons(app, admin, domains, media):
    shop = domains['shop']
    save(admin, shop, logo=(io.BytesIO(png(600, 600)), 'big.png'))
    row = own_row(shop)
    assert Image.open(f'app{row.logo_512}').size == (512, 512)
    assert Image.open(f'app{row.logo_192}').size == (192, 192)
    assert Image.open(f'app{row.logo}').size == (100, 100)
    assert manifest_icons(app) == {'192x192': (row.logo_192, 'image/png'), '512x512': (row.logo_512, 'image/png')}


def test_a_small_png_upload_is_its_own_logo(admin, domains, media):
    save(admin, domains['shop'], logo=(io.BytesIO(png(64, 64)), 'small.png'))
    row = own_row(domains['shop'])
    assert row.logo.endswith('.png') and not row.logo.endswith('_100.png')
    assert os.path.isfile(f'app{row.logo}')


def test_an_svg_upload_keeps_no_sized_copies_and_the_manifest_falls_back(app, admin, domains, media):
    """As the site logo upload does: an SVG is the logo itself, with no PNG copies, and the manifest never names an
    SVG as a PNG icon. A domain's own logo is never shown beside the first domain's icons, so it falls back to
    PieFed's."""
    set_setting('logo_192', '/static/media/site_192.png')
    set_setting('logo_512', '/static/media/site_512.png')
    shop = domains['shop']
    save(admin, shop, logo=(io.BytesIO(png()), 'shop.png'))
    save(admin, shop, logo=(io.BytesIO(SVG), 'shop.SVG'))
    row = own_row(shop)
    assert row.logo.endswith('.SVG')
    assert all(getattr(row, field) == '' for field in LOGO_FIELDS[1:])
    assert media_files(media) == {Path(row.logo).name}
    assert manifest_icons(app) == {'192x192': ('/static/images/piefed_logo_icon_t_192.png', 'image/png'),
                                   '512x512': ('/static/images/piefed_logo_icon_t_512.png', 'image/png')}


def test_a_domain_with_no_logo_of_its_own_has_the_first_domains_icons(app, admin, domains):
    set_setting('logo_192', '/static/media/site_192.png')
    set_setting('logo_512', '/static/media/site_512.png')
    save(admin, domains['shop'])
    assert manifest_icons(app) == {'192x192': ('/static/media/site_192.png', 'image/png'),
                                   '512x512': ('/static/media/site_512.png', 'image/png')}


# ---------------------------------------------------------------------------------------------------------------------
# Removing replaced logo files: only this domain's own upload's

def own_logo(domain, **paths):
    """`domain`'s row naming these logo paths, as a past upload (or a hand edit of the row) left it."""
    row = own_row(domain) or DomainBranding(site_domain_id=domain.id)
    for field in LOGO_FIELDS:
        setattr(row, field, paths.get(field, ''))
    db.session.add(row)
    db.session.commit()


def test_replacing_a_logo_removes_the_domains_previous_files(admin, domains, media):
    shop = domains['shop']
    save(admin, shop, logo=(io.BytesIO(png()), 'first.png'))
    old = {Path(getattr(own_row(shop), field)).name for field in LOGO_FIELDS}
    save(admin, shop, logo=(io.BytesIO(png()), 'second.jpg'))
    new = {Path(getattr(own_row(shop), field)).name for field in LOGO_FIELDS}
    assert old.isdisjoint(new)
    assert media_files(media) == new


def test_logo_replace_never_deletes_site_logo_files(admin, domains, media):
    """Review Focus 1: the domain's row names the files Site's logo and icon settings name (it was inheriting them, or
    a hand edit copied them). Replacing the domain's logo leaves every one of them."""
    site = db.session.get(Site, 1)
    names = {field: f'logo_SiteA_{field[5:] or "100"}.png' for field in LOGO_FIELDS}
    write_media(media, *names.values())
    for field in LOGO_FIELDS[:5]:
        setattr(site, field, f'/static/media/{names[field]}')
    db.session.commit()
    set_setting('logo_192', f"/static/media/{names['logo_192']}")
    set_setting('logo_512', f"/static/media/{names['logo_512']}")
    own_logo(domains['shop'], **{field: f'/static/media/{name}' for field, name in names.items()})
    assert save(admin, domains['shop'], logo=(io.BytesIO(png()), 'mine.png')).status_code == 302
    assert set(names.values()) <= media_files(media)
    save(admin, domains['shop'], remove_logo='y')
    assert set(names.values()) <= media_files(media)
    assert get_setting('logo_192') == f"/static/media/{names['logo_192']}"


@pytest.mark.parametrize('stored, victim', [
    pytest.param('/static/media/../logo_abcde.png', 'app/static/logo_abcde.png', id='outside the media directory'),
    pytest.param('/static/media/logo_a/../../logo_b.png', 'app/static/logo_b.png', id='a dot-dot path'),
    pytest.param('/static/media/someone_else.png', 'app/static/media/someone_else.png',
                 id='not a name the upload gives'),
    pytest.param('/static/media/posts/logo_abcde.png', 'app/static/media/posts/logo_abcde.png',
                 id='a sub-directory'),
])
def test_a_stored_path_the_upload_did_not_write_is_never_removed(admin, domains, media, stored, victim):
    os.makedirs(os.path.dirname(victim), exist_ok=True)
    Path(victim).write_bytes(b'not ours')
    own_logo(domains['shop'], logo=stored)
    assert save(admin, domains['shop'], logo=(io.BytesIO(png()), 'mine.png')).status_code == 302
    assert Path(victim).read_bytes() == b'not ours'


def test_a_symlink_out_of_the_media_directory_is_never_followed(admin, domains, media, tmp_path):
    outside = tmp_path / 'secret.png'
    outside.write_bytes(b'not ours')
    (media / 'logo_abcde.png').symlink_to(outside)
    own_logo(domains['shop'], logo='/static/media/logo_abcde.png')
    save(admin, domains['shop'], remove_logo='y')
    assert outside.read_bytes() == b'not ours'
    assert (media / 'logo_abcde.png').is_symlink()


@pytest.mark.parametrize('target', ['precious.png', 'logo_Other.png'])
def test_a_symlink_to_another_media_file_is_never_followed(admin, domains, media, target):
    """The row names a symlink inside the media directory, pointing at another file there that nothing names. Neither
    the file nor the link is removed: a logo save never deletes through a symlink."""
    (media / target).write_bytes(b'not ours')
    (media / 'logo_abcde.png').symlink_to(media / target)
    own_logo(domains['shop'], logo='/static/media/logo_abcde.png')
    assert save(admin, domains['shop'], remove_logo='y').status_code == 302
    assert (media / target).read_bytes() == b'not ours'
    assert (media / 'logo_abcde.png').is_symlink()


def test_a_file_another_domain_names_is_never_removed(admin, domains, media):
    write_media(media, 'logo_Share.png', 'logo_Share_180.png')
    own_logo(domains['onion'], logo='/static/media/logo_Share.png', logo_180='/static/media/logo_Share_180.png')
    own_logo(domains['shop'], logo='/static/media/logo_Share.png', logo_180='/static/media/logo_Share_180.png')
    save(admin, domains['shop'], remove_logo='y')
    assert {'logo_Share.png', 'logo_Share_180.png'} <= media_files(media)
    assert own_row(domains['onion']).logo == '/static/media/logo_Share.png'


def test_a_path_the_form_names_is_never_removed(admin, domains, media):
    """The form has no path field: every path a removal reads is the domain's stored row's. A posted path is ignored."""
    write_media(media, 'logo_Formy.png', 'logo_Formy_180.png')
    response = save(admin, domains['shop'], remove_logo='y', logo_180='/static/media/logo_Formy_180.png',
                    logo='/static/media/logo_Formy.png')
    assert response.status_code == 302
    assert {'logo_Formy.png', 'logo_Formy_180.png'} <= media_files(media)


def test_a_previous_file_already_gone_is_no_error(admin, domains, media):
    own_logo(domains['shop'], logo='/static/media/logo_Gone1.png', logo_180='/static/media/logo_Gone1_180.png')
    write_media(media, 'logo_Gone1_180.png')
    assert save(admin, domains['shop'], remove_logo='y').status_code == 302
    assert media_files(media) == set()


# ---------------------------------------------------------------------------------------------------------------------
# Off-site references (spec §3.4)

OFFSITE = [
    pytest.param({'additional_css': f'a {{ background: url({ELSEWHERE}/a.png) }}'}, 'additional_css',
                 f'{ELSEWHERE}/a.png', id='css'),
    # in the Markdown the image is not HTML: only the HTML made from it names the image as loading
    pytest.param({'sidebar': f'![a cat]({ELSEWHERE}/s.png)'}, 'sidebar', f'{ELSEWHERE}/s.png', id='sidebar markdown'),
    pytest.param({'about': f'Hello ![a cat]({ELSEWHERE}/b.png)'}, 'about', f'{ELSEWHERE}/b.png', id='about markdown'),
    pytest.param({'additional_css': f'</style><img src="{ELSEWHERE}/c.png">'}, 'additional_css', f'{ELSEWHERE}/c.png',
                 id='markup in the css'),
]


@pytest.mark.parametrize('fields, field, reference', OFFSITE)
def test_onion_offsite_refused(admin, domains, media, fields, field, reference):
    response = save(admin, domains['onion'], logo=(io.BytesIO(png()), 'onion.png'), **fields)
    assert response.status_code == 200
    assert reference in response.get_data(as_text=True)
    assert own_row(domains['onion']) is None   # nothing written: no row, no logo files
    assert media_files(media) == set()


def test_the_onions_stored_offsite_logo_is_refused_until_replaced(admin, domains, media):
    own_logo(domains['onion'], logo=f'{ELSEWHERE}/logo.png')
    response = save(admin, domains['onion'], name='Onion Branded')
    assert response.status_code == 200
    assert f'{ELSEWHERE}/logo.png' in response.get_data(as_text=True)
    assert own_row(domains['onion']).name is None
    assert save(admin, domains['onion'], name='Onion Branded', remove_logo='y').status_code == 302
    assert (own_row(domains['onion']).name, own_row(domains['onion']).logo) == ('Onion Branded', '')


def test_the_onion_saves_its_own_references(admin, domains):
    response = save(admin, domains['onion'], additional_css=f'a {{ background: url(http://{ONION}/a.png) }}',
                    sidebar='![a cat](/static/media/cat.png)')
    assert response.status_code == 302
    assert [category for category, _message in flashes(admin)] == ['message']
    assert own_row(domains['onion']).additional_css == f'a {{ background: url(http://{ONION}/a.png) }}'


@pytest.mark.parametrize('fields, field, reference', OFFSITE)
def test_other_domain_offsite_warns(admin, domains, fields, field, reference):
    response = save(admin, domains['shop'], **fields)
    assert response.status_code == 302
    row = own_row(domains['shop'])
    assert row is not None
    warnings = [message for category, message in flashes(admin) if category == 'warning']
    assert len(warnings) == 1 and reference in warnings[0]


def test_a_warning_names_each_reference(admin, domains):
    save(admin, domains['shop'], additional_css=f'a {{ background: url({ELSEWHERE}/a.png) }}',
         sidebar=f'![a]({ELSEWHERE}/s.png)', about=f'![b]({ELSEWHERE}/b.png)')
    [warning] = [message for category, message in flashes(admin) if category == 'warning']
    for reference in (f'{ELSEWHERE}/a.png', f'{ELSEWHERE}/s.png', f'{ELSEWHERE}/b.png'):
        assert reference in warning


@pytest.fixture
def site_admin(app, admin):
    db.session.add(Language(id=2, code='en', name='English'))
    db.session.commit()
    return admin


def test_site_settings_offsite_warns(admin, domains):
    client, token = admin
    forget_login()
    with patch('app.admin.routes.render_template', return_value='rendered'):
        response = client.post('/admin/site', content_type='multipart/form-data', data={
            'name': 'The First Site', 'description': '', 'about': f'![b]({ELSEWHERE}/b.png)',
            'sidebar': f'![s]({ELSEWHERE}/s.png)', 'legal_information': '', 'tos_url': '', 'announcement': '',
            'contact_email': 'admin@example.org', 'submit': 'Save', 'csrf_token': token, 'icon': (b'', '')})
    assert response.status_code == 200
    assert db.session.get(Site, 1).sidebar == f'![s]({ELSEWHERE}/s.png)'   # saved
    [warning] = [message for category, message in flashes(admin) if category == 'warning']
    assert f'{ELSEWHERE}/s.png' in warning and f'{ELSEWHERE}/b.png' in warning


def test_site_settings_without_offsite_references_do_not_warn(admin, domains):
    client, token = admin
    forget_login()
    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/site', content_type='multipart/form-data', data={
            'name': 'The First Site', 'description': '', 'about': '![b](/static/media/b.png)', 'sidebar': 'plain',
            'legal_information': '', 'tos_url': '', 'announcement': '', 'contact_email': 'admin@example.org',
            'submit': 'Save', 'csrf_token': token, 'icon': (b'', '')})
    assert [category for category, _message in flashes(admin)] == ['message']


def misc_post(admin, css):
    client, token = admin
    forget_login()
    # No 'submit' key: SiteMiscForm and CloseInstanceForm both name their submit field 'submit'
    with patch('app.admin.routes.render_template', return_value='rendered'):
        return client.post('/admin/misc', data={
            'csrf_token': token, 'redirect_policy': 'same_origin', 'allow_video_file_uploads': 'no',
            'allow_audio_file_uploads': 'no', 'default_filter': 'popular', 'default_theme': 'piefed',
            'registration_mode': 'Open', 'language_id': '2', 'read_posts_cutoff': '180', 'nsfw_country_restriction': '',
            'auto_decline_countries': '', 'application_question': '', 'auto_decline_referrers': '',
            'ban_check_servers': '', 'registration_approved_email': '', 'additional_css': css, 'additional_js': ''})


def test_site_css_offsite_warns(site_admin, domains):
    response = misc_post(site_admin, f'@import "{ELSEWHERE}/e.css";')
    assert response.status_code == 200
    assert db.session.get(Site, 1).additional_css == f'@import "{ELSEWHERE}/e.css";'
    [warning] = [message for category, message in flashes(site_admin) if category == 'warning']
    assert f'{ELSEWHERE}/e.css' in warning
    misc_post(site_admin, '.plain { color: red; }')
    assert [category for category, _message in flashes(site_admin)] == ['message']


SCRIPTED = f'{ELSEWHERE}/<script>alert(1)</script>.png'


@pytest.mark.parametrize('domain', ['shop', 'onion'])
def test_a_reference_is_shown_escaped(admin, domains, domain):
    """A reference is the admin's own text, shown back in the warning (off the onion) or the field error (on it):
    escaped, never as markup."""
    response = save(admin, domains[domain], additional_css=f'a {{ background: url("{SCRIPTED}") }}')
    if domain == 'shop':
        assert response.status_code == 302
        forget_login()
        response = admin[0].get(response.headers['Location'])
    body = response.get_data(as_text=True)
    assert 'elsewhere.example.org/&lt;script&gt;alert(1)&lt;/script&gt;.png' in body
    assert '<script>alert(1)</script>' not in body
