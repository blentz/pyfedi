"""A dark mode logo beside the logo, for the site and for each domain.

The site profile and each domain's branding take a second upload, shown instead of the logo while the page is in dark
mode (`data-bs-theme="dark"`, set by app/static/js/set_theme.js). Both images are in the page and CSS shows the one
that matches; with no dark logo the page has the one image it always had. Only what pages show changes: og:image,
favicons, the PWA manifest and the actor icon keep the logo, since nothing says which mode their reader is in.

`logo_dark` is the upload (an SVG as uploaded, else a PNG of at most 100 pixels) and `logo_dark_152` what the header
shows of a raster upload, as `logo` and `logo_152` are for the logo. A domain with its own logo or dark logo
takes its dark logo from its own row alone: a blank one is its own logo in dark mode, never the first domain's dark
logo beside its own logo."""
import io
import re
from unittest.mock import patch

import pytest
from sqlalchemy import inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import db
from app.branding import Branding, branding_for, onion_safe
from app.models import DomainBranding, Site
from app.request_hooks import BrandingOnUse
from app.storefront import clear_domain_cache
from tests.conftest import forget_login
from tests.test_admin_domain_branding import (ELSEWHERE, REPO, admin, domains, media, media_files, own_row,  # noqa: F401
                                              png, save)
from tests.test_admin_site_profile import HOSTILE_SVG, SVG
from tests.test_branding import load_migration
from tests.test_branding_pages import branded, onion_branding, page  # noqa: F401  (fixtures)
from tests.two_domains import onion_request, two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('site')

PIEFED_LOGO = '/static/images/piefed_logo_full_t_300x300.svg'


def branding(**values) -> Branding:
    return Branding(**{**dict.fromkeys(Branding._fields, ''), **values})


# ---------------------------------------------------------------------------------------------------------------------
# What a branding shows

@pytest.mark.parametrize('values, header, header_dark', [
    ({}, PIEFED_LOGO, PIEFED_LOGO),
    ({'logo': '/m/a_100.png', 'logo_152': '/m/a_152.png'}, '/m/a_152.png', '/m/a_152.png'),
    ({'logo': '/m/a.svg', 'logo_152': ''}, '/m/a.svg', '/m/a.svg'),
    ({'logo': '/m/a.SVG'}, '/m/a.SVG', '/m/a.SVG'),
    ({'logo': '/m/a.svg', 'logo_dark': '/m/d_100.png', 'logo_dark_152': '/m/d_152.png'}, '/m/a.svg', '/m/d_152.png'),
    ({'logo_dark': '/m/d.svg'}, PIEFED_LOGO, '/m/d.svg'),
    ({'logo': '/m/a.svg', 'logo_dark': '/m/d_100.png', 'logo_dark_152': ''}, '/m/a.svg', '/m/a.svg'),
], ids=['none', 'raster', 'svg', 'upper-case-svg', 'raster-dark', 'svg-dark-only', 'raster-dark-without-copy'])
def test_the_header_shows_the_logo_and_dark_mode_its_dark_logo(values, header, header_dark):
    shown = branding(**values)
    assert (shown.header_logo, shown.header_logo_dark) == (header, header_dark)


@pytest.mark.parametrize('values, embed, embed_dark', [
    ({}, PIEFED_LOGO, PIEFED_LOGO),
    ({'logo': '/m/a_100.png'}, '/m/a_100.png', '/m/a_100.png'),
    ({'logo': '/m/a_100.png', 'logo_dark': '/m/d_100.png'}, '/m/a_100.png', '/m/d_100.png'),
], ids=['none', 'logo', 'dark'])
def test_an_embed_shows_the_logo_and_dark_mode_its_dark_logo(values, embed, embed_dark):
    shown = branding(**values)
    assert (shown.embed_logo, shown.embed_logo_dark) == (embed, embed_dark)


def test_templates_may_read_what_a_branding_shows(app, two_domains):
    on_use = BrandingOnUse()
    with onion_request(app):
        assert on_use.header_logo_dark == PIEFED_LOGO
        assert on_use.embed_logo_dark == PIEFED_LOGO
    with pytest.raises(AttributeError):
        on_use.items


def test_the_onion_drops_an_offsite_dark_logo():
    safe = onion_safe(branding(logo_dark=f'{ELSEWHERE}/d.png', logo_dark_152=f'{ELSEWHERE}/d_152.png'), 'abc.onion')
    assert (safe.logo_dark, safe.logo_dark_152) == ('', '')
    assert safe.header_logo_dark == safe.header_logo


# ---------------------------------------------------------------------------------------------------------------------
# What each domain inherits

DARK = dict(logo_dark='/static/media/logo_Dark1_100.png', logo_dark_152='/static/media/logo_Dark1_152.png')


@pytest.fixture
def site_dark(two_domains, site):
    site.logo = '/static/media/logo_Site1_100.png'
    site.logo_152 = '/static/media/logo_Site1_152.png'
    for field, value in DARK.items():
        setattr(site, field, value)
    db.session.commit()
    clear_domain_cache()
    return two_domains.domains


def set_row(domain, **values):
    db.session.add(DomainBranding(site_domain_id=domain.id, **values))
    db.session.commit()
    clear_domain_cache()


def dark_of(domain) -> tuple:
    shown = branding_for(domain)
    return shown.logo_dark, shown.logo_dark_152


def test_the_first_domain_and_an_unbranded_one_show_the_sites_dark_logo(site_dark):
    first, onion = site_dark
    assert dark_of(first) == dark_of(onion) == tuple(DARK.values())


def test_a_domain_with_its_own_logo_shows_it_in_dark_mode_too(site_dark):
    _first, onion = site_dark
    set_row(onion, logo='/static/media/logo_Own01.svg', logo_dark='')
    assert dark_of(onion) == ('', '')
    assert branding_for(onion).header_logo_dark == '/static/media/logo_Own01.svg'


def test_a_domain_with_its_own_dark_logo_alone_shows_the_sites_logo_and_its_dark_logo(site_dark):
    _first, onion = site_dark
    set_row(onion, logo_dark='/static/media/logo_Own02.svg')
    shown = branding_for(onion)
    assert (shown.logo_dark, shown.logo_dark_152) == ('/static/media/logo_Own02.svg', '')
    assert shown.header_logo == '/static/media/logo_Site1_152.png'
    assert shown.header_logo_dark == '/static/media/logo_Own02.svg'


# ---------------------------------------------------------------------------------------------------------------------
# What pages show

def header_images(body: str) -> list[str]:
    brand = re.search(r'<a class="navbar-brand".*?</a>', body, re.S).group(0)
    return re.findall(r'<img [^>]*>', brand)


def test_without_a_dark_logo_the_header_has_its_one_image(app, branded):
    assert header_images(page(app, '/')) == [
        '<img src="/static/media/golden_152.png" alt="Logo" width="50" height="50" fetchpriority="high" />']


def test_with_a_dark_logo_the_header_has_both_marked_for_the_css(app, branded):
    site = db.session.get(Site, 1)
    site.logo_dark, site.logo_dark_152 = '/static/media/dark_100.png', '/static/media/dark_152.png'
    db.session.commit()
    clear_domain_cache()
    assert header_images(page(app, '/')) == [
        '<img src="/static/media/golden_152.png" class="logo_light" alt="Logo" width="50" height="50" '
        'fetchpriority="high" />',
        '<img src="/static/media/dark_152.png" class="logo_dark" alt="Logo" width="50" height="50" '
        'fetchpriority="high" />']


def test_the_onion_header_shows_its_own_dark_logo(app, branded):
    onion_branding(branded, logo_dark='/static/media/onion_dark.svg')
    images = header_images(page(app, '/', onion=True))
    assert [re.search(r'class="(\w+)"', image).group(1) for image in images] == ['logo_light', 'logo_dark']
    assert 'src="/static/media/onion_dark.svg"' in images[1]


def test_the_css_shows_the_image_that_matches_the_mode():
    compiled = (REPO / 'app' / 'static' / 'styles.css').read_text()
    assert re.search(r'img\.logo_dark \{\s*display: none;\s*\}', compiled)
    assert re.search(r'\[data-bs-theme=dark\] img\.logo_light \{\s*display: none;\s*\}', compiled)
    assert re.search(r'\[data-bs-theme=dark\] img\.logo_dark \{\s*display: inline;\s*\}', compiled)


# ---------------------------------------------------------------------------------------------------------------------
# The site profile's dark mode icon

def site_save(admin, icon=None, icon_dark=None, **fields):
    client, token = admin
    data = {'name': 'The First Site', 'description': '', 'about': '', 'sidebar': '', 'legal_information': '',
            'tos_url': '', 'announcement': '', 'contact_email': 'admin@example.com', 'submit': 'Save',
            'csrf_token': token, 'icon': icon or (io.BytesIO(b''), ''),
            'icon_dark': icon_dark or (io.BytesIO(b''), '')}
    data.update(fields)
    forget_login()
    with patch('app.admin.routes.render_template', return_value='rendered'):
        return client.post('/admin/site', data=data, content_type='multipart/form-data')


def site_dark_logo() -> tuple:
    db.session.expire_all()
    site = db.session.get(Site, 1)
    return site.logo_dark, site.logo_dark_152


def test_a_png_dark_icon_keeps_its_upload_and_header_copies(admin, domains, media):
    assert site_save(admin, icon_dark=(io.BytesIO(png(300, 300)), 'dark.png')).status_code == 200
    dark, dark_152 = site_dark_logo()
    assert re.fullmatch(r'/static/media/logo_\w{5}_100\.png', dark)
    assert dark_152 == dark.replace('_100.png', '_152.png')
    assert media_files(media) == {dark.rsplit('/', 1)[1], dark_152.rsplit('/', 1)[1]}
    site = db.session.get(Site, 1)
    assert not site.logo   # the icon is untouched


def test_an_svg_dark_icon_is_kept_as_uploaded(admin, domains, media):
    assert site_save(admin, icon_dark=(io.BytesIO(SVG), 'dark.svg')).status_code == 200
    dark, dark_152 = site_dark_logo()
    assert re.fullmatch(r'/static/media/logo_\w{5}\.svg', dark) and dark_152 == ''
    assert media_files(media) == {dark.rsplit('/', 1)[1]}


def test_a_save_without_a_dark_upload_keeps_the_dark_icon(admin, domains, media):
    site_save(admin, icon_dark=(io.BytesIO(SVG), 'dark.svg'))
    kept = site_dark_logo()
    assert site_save(admin).status_code == 200
    assert site_dark_logo() == kept


def test_replacing_and_removing_the_dark_icon_removes_its_files(admin, domains, media):
    site_save(admin, icon_dark=(io.BytesIO(png()), 'one.png'))
    site_save(admin, icon_dark=(io.BytesIO(png()), 'two.png'))
    second = site_dark_logo()
    assert media_files(media) == {path.rsplit('/', 1)[1] for path in second}
    assert site_save(admin, remove_icon_dark='y').status_code == 200
    assert site_dark_logo() == ('', '')
    assert media_files(media) == set()


def test_both_icons_in_one_save(admin, domains, media):
    response = site_save(admin, icon=(io.BytesIO(png()), 'light.png'), icon_dark=(io.BytesIO(SVG), 'dark.svg'))
    assert response.status_code == 200
    site = db.session.get(Site, 1)
    assert site.logo_152 and site.logo_dark.endswith('.svg')
    assert site.logo_dark not in (site.logo, site.logo_152)


def test_a_refused_dark_icon_leaves_everything_alone(admin, domains, media):
    site_save(admin, icon_dark=(io.BytesIO(SVG), 'dark.svg'))
    before, files = site_dark_logo(), media_files(media)
    assert site_save(admin, icon_dark=(io.BytesIO(HOSTILE_SVG), 'bad.svg')).status_code == 400
    assert site_dark_logo() == before and media_files(media) == files


def test_a_refused_icon_takes_the_dark_icon_uploaded_with_it(admin, domains, media):
    response = site_save(admin, icon=(io.BytesIO(b'not an image'), 'light.png'),
                         icon_dark=(io.BytesIO(png()), 'dark.png'))
    assert response.status_code == 400
    assert site_dark_logo() == ('', '')
    assert media_files(media) == set()


# ---------------------------------------------------------------------------------------------------------------------
# A domain's dark mode logo

def test_a_domain_uploads_and_removes_its_dark_logo(admin, domains, media):
    shop = domains['shop']
    assert save(admin, shop, logo_dark=(io.BytesIO(png()), 'dark.png')).status_code == 302
    row = own_row(shop)
    assert row.logo_dark.endswith('_100.png') and row.logo_dark_152.endswith('_152.png')
    assert not row.logo   # the logo is untouched
    assert media_files(media) == {row.logo_dark.rsplit('/', 1)[1], row.logo_dark_152.rsplit('/', 1)[1]}
    assert branding_for(shop).header_logo_dark == row.logo_dark_152
    assert save(admin, shop).status_code == 302
    assert own_row(shop).logo_dark == row.logo_dark   # kept by a save without an upload
    assert save(admin, shop, remove_logo_dark='y').status_code == 302
    row = own_row(shop)
    assert (row.logo_dark, row.logo_dark_152) == ('', '')
    assert media_files(media) == set()


def test_a_domains_logo_and_dark_logo_in_one_save(admin, domains, media):
    shop = domains['shop']
    response = save(admin, shop, logo=(io.BytesIO(png()), 'light.png'), logo_dark=(io.BytesIO(SVG), 'dark.svg'))
    assert response.status_code == 302
    row = own_row(shop)
    assert row.logo and row.logo_dark.endswith('.svg') and row.logo_dark_152 == ''
    assert save(admin, shop, remove_logo='y').status_code == 302
    assert own_row(shop).logo_dark == row.logo_dark   # removing the logo keeps the dark logo


def test_a_domains_refused_logo_takes_its_dark_logo_with_it(admin, domains, media):
    shop = domains['shop']
    response = save(admin, shop, logo=(io.BytesIO(b'not an image'), 'light.png'),
                    logo_dark=(io.BytesIO(png()), 'dark.png'))
    assert response.status_code == 400
    assert own_row(shop) is None
    assert media_files(media) == set()


def test_deleting_a_domain_removes_its_dark_logo(admin, domains, media):
    shop = domains['shop']
    save(admin, shop, logo_dark=(io.BytesIO(png()), 'dark.png'))
    assert media_files(media)
    client, token = admin
    forget_login()
    assert client.post(f'/admin/domains/{shop.id}/delete', data={'csrf_token': token}).status_code == 302
    assert media_files(media) == set()


def test_a_dark_logo_the_site_names_is_never_removed(admin, domains, media):
    shop = domains['shop']
    save(admin, shop, logo_dark=(io.BytesIO(SVG), 'dark.svg'))
    shared = own_row(shop).logo_dark
    site = db.session.get(Site, 1)
    site.logo_dark = shared
    db.session.commit()
    save(admin, shop, remove_logo_dark='y')
    assert shared.rsplit('/', 1)[1] in media_files(media)


def test_the_onion_refuses_a_stored_offsite_dark_logo(admin, domains, media):
    onion = domains['onion']
    db.session.add(DomainBranding(site_domain_id=onion.id, logo_dark=f'{ELSEWHERE}/dark.png'))
    db.session.commit()
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        assert save(admin, onion).status_code == 200
    form = render.call_args.kwargs['form']
    assert form.logo_dark.errors == [f'Off the onion: {ELSEWHERE}/dark.png']
    assert save(admin, onion, remove_logo_dark='y').status_code == 302
    assert own_row(onion).logo_dark == ''


def test_the_branding_page_shows_the_dark_logo(admin, domains, media):
    shop = domains['shop']
    save(admin, shop, logo_dark=(io.BytesIO(SVG), 'dark.svg'))
    client, _token = admin
    forget_login()
    body = client.get(f'/admin/domains/{shop.id}/branding').get_data(as_text=True)
    assert f'<img src="{own_row(shop).logo_dark}" alt="This domain\'s dark mode logo" height="50">' in body


# ---------------------------------------------------------------------------------------------------------------------
# The migration

def test_the_migration_adds_and_drops_the_dark_logo_columns(db_session):
    migration = load_migration('*_dark_mode_logo.py')
    db.session.commit()

    def columns(table):
        return {column['name'] for column in inspect(db.session.connection()).get_columns(table)}
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            migration.downgrade()
            for table in ('site', 'domain_branding'):
                assert {'logo_dark', 'logo_dark_152'}.isdisjoint(columns(table))
            migration.upgrade()
            for table in ('site', 'domain_branding'):
                assert {'logo_dark', 'logo_dark_152'} <= columns(table)
        db.session.commit()
    finally:
        db.session.rollback()
