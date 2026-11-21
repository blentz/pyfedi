"""Per-domain branding storage and the effective-branding loader (project 4, Task 1)."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from cachelib import SimpleCache

from app import cache, db
from app.branding import Branding, branding_for, current_branding
from app.domain_config import own_row
from app.models import DomainConfig, SiteDomain, first_config
from app.storefront import clear_domain_cache
from tests.schema_history import DomainConfigMigrations
from tests.two_domains import default_request, onion_request
from tests.factories import config_of
from app.domain_config import own_config

pytestmark = pytest.mark.usefixtures('site')

SITE_VALUES = dict(name='Site name', description='Site description', logo='site.png', logo_180='site180.png',
                   logo_152='site152.png', logo_32='site32.png', logo_16='site16.png', logo_dark='site_dark.png',
                   logo_dark_152='site_dark152.png', default_theme='site-theme',
                   additional_css='.site{}', sidebar='site sidebar', sidebar_html='<p>site sidebar</p>',
                   about_html='<p>site about</p>', home_sidebar='about,-new_instances',
                   community_sidebar='-actions')
ONION_VALUES = {field: f'onion {field}' for field in Branding._fields}


@pytest.fixture
def site_values(site):
    for field, value in SITE_VALUES.items():
        setattr(site, field, value)
    db.session.commit()
    clear_domain_cache()
    return SITE_VALUES


def set_branding(domain, **values):
    row = config_of(domain)
    for field, value in values.items():
        setattr(row, field, value)
    db.session.commit()
    clear_domain_cache()


def test_default_domain_is_the_site(two_domains, site_values):
    default, _onion = two_domains.domains
    assert branding_for(default)._asdict() == site_values
    assert own_row(default) is first_config()   # the first domain's own row is the first row (MT1)


def test_blank_fields_inherit_per_field(two_domains, site_values):
    default, onion = two_domains.domains
    set_branding(onion, name='Onion name', description='', logo=None)
    assert branding_for(onion)._asdict() == {**site_values, 'name': 'Onion name'}
    assert own_row(onion).name == 'Onion name'


ICON_FIELDS = ('logo_180', 'logo_152', 'logo_32', 'logo_16')


def test_an_own_logo_takes_its_icon_sizes_from_its_own_row_only(two_domains, site_values):
    """A domain with a logo of its own never shows it beside the first domain's icons: each icon size is its own, and
    a blank one is PieFed's default (the page's fallback), as the manifest's icons are. An SVG upload stores no sizes."""
    _default, onion = two_domains.domains
    set_branding(onion, logo='/static/media/logo_Own01.svg', logo_180='', logo_152=None, logo_32='', logo_16='')
    effective = branding_for(onion)
    assert effective.logo == '/static/media/logo_Own01.svg'
    assert [getattr(effective, field) for field in ICON_FIELDS] == ['', '', '', '']
    set_branding(onion, logo_180='/static/media/logo_Own01_180.png')
    assert branding_for(onion).logo_180 == '/static/media/logo_Own01_180.png'
    assert branding_for(onion).logo_32 == ''


def test_a_domain_with_no_logo_of_its_own_inherits_each_icon_size(two_domains, site_values):
    _default, onion = two_domains.domains
    set_branding(onion, logo='', logo_32='own32.png')
    assert [getattr(branding_for(onion), field) for field in ICON_FIELDS] == \
        [site_values['logo_180'], site_values['logo_152'], 'own32.png', site_values['logo_16']]


def test_a_domain_with_no_row_is_the_site(two_domains, site_values):
    assert branding_for(two_domains.domains[1])._asdict() == site_values


def test_own_values_win(two_domains, site_values):
    _default, onion = two_domains.domains
    set_branding(onion, **ONION_VALUES, about='onion about')
    assert branding_for(onion)._asdict() == ONION_VALUES


def test_the_default_domains_configuration_is_the_sites(two_domains, site, site_values):
    default, onion = two_domains.domains
    set_branding(default, name='Shared')
    db.session.expire_all()
    assert (branding_for(default).name, branding_for(onion).name, site.name) == ('Shared', 'Shared', 'Shared')


def test_no_site_values_is_blank(two_domains, site):
    for field in SITE_VALUES:
        setattr(site, field, None)
    db.session.commit()
    clear_domain_cache()
    assert set(branding_for(two_domains.domains[1])) == {''}


def test_current_branding_follows_the_storefront(app, two_domains, site_values):
    _default, onion = two_domains.domains
    set_branding(onion, name='Onion name')
    with onion_request(app):
        assert current_branding().name == 'Onion name'
    with default_request(app):
        assert current_branding().name == site_values['name']
    assert current_branding().name == site_values['name']


def test_a_saved_change_is_seen_once_committed(app, two_domains, site_values, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    clear_domain_cache()
    onion = two_domains.domains[1]
    assert branding_for(onion).name == site_values['name']
    config_of(onion).name = 'Later'
    db.session.flush()
    assert branding_for(onion).name == site_values['name']   # memoised until the commit
    db.session.commit()
    assert branding_for(onion).name == 'Later'   # a commit that writes a configuration clears it


def test_deleting_a_domain_deletes_its_branding(two_domains, site_values):
    onion = two_domains.domains[1]
    set_branding(onion, name='Onion name')
    db.session.expire_all()
    db.session.query(SiteDomain).filter_by(id=onion.id).delete()
    db.session.commit()
    assert own_config(onion.id) is None


def load_migration(pattern):
    path = next(Path(__file__).parent.parent.glob(f'migrations/versions/{pattern}'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def restore_registration_check():
    """Put back site_domain's registration check as the current schema has it. Replaying d6a1f4c8e2b3's upgrade (as
    the role tests do) remakes it as it was then, without 'paid', which a later revision added: left so, a later test
    on the same database that makes a paid domain fails on the constraint. Call inside the replay's Operations context."""
    from alembic import op
    op.drop_constraint('ck_site_domain_registration', 'site_domain', type_='check')
    op.create_check_constraint('ck_site_domain_registration', 'site_domain', "registration IN ('anyone','closed','paid')")


def test_migration_creates_and_drops_the_table(db_session):
    """The table, then its icon columns (Task 5), then its dark logo and sidebar order columns, each dropped and made again in revision
    order: the database is left as `flask db upgrade` made it."""
    migration = load_migration('*_domain_branding.py')
    icons = load_migration('*_domain_branding_icons.py')
    dark = load_migration('*_dark_mode_logo.py')
    sidebar = load_migration('*_sidebar_box_order.py')
    later = DomainConfigMigrations()
    db.session.commit()

    def columns():
        return {column['name'] for column in inspect(db.session.connection()).get_columns('domain_branding')}
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            later.downgrade()   # the table was folded into domain_config
            sidebar.downgrade()
            dark.downgrade()
            icons.downgrade()
            assert {'logo_192', 'logo_512'}.isdisjoint(columns())
            migration.downgrade()
            assert not inspect(db.session.connection()).has_table('domain_branding')
            migration.upgrade()
            assert inspect(db.session.connection()).has_table('domain_branding')
            icons.upgrade()
            assert {'logo_192', 'logo_512'} <= columns()
            dark.upgrade()
            assert {'logo_dark', 'logo_dark_152'} <= columns()
            sidebar.upgrade()
            assert {'home_sidebar', 'community_sidebar'} <= columns()
            later.upgrade()
        db.session.commit()
    finally:
        db.session.rollback()
