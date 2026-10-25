"""Per-domain branding storage and the effective-branding loader (project 4, Task 1)."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from cachelib import SimpleCache

from app import cache, db
from app.branding import Branding, branding_for, current_branding, own_branding
from app.models import DomainBranding, SiteDomain
from app.storefront import clear_domain_cache
from tests.two_domains import default_request, onion_request, two_domains  # noqa: F401  (a fixture)

pytestmark = pytest.mark.usefixtures('site')

SITE_VALUES = dict(name='Site name', description='Site description', logo='site.png', logo_180='site180.png',
                   logo_152='site152.png', logo_32='site32.png', logo_16='site16.png', default_theme='site-theme',
                   additional_css='.site{}', sidebar='site sidebar', sidebar_html='<p>site sidebar</p>',
                   about_html='<p>site about</p>')
ONION_VALUES = {field: f'onion {field}' for field in Branding._fields}


@pytest.fixture
def site_values(site):
    for field, value in SITE_VALUES.items():
        setattr(site, field, value)
    db.session.commit()
    clear_domain_cache()
    return SITE_VALUES


def set_branding(domain, **values):
    row = db.session.get(DomainBranding, domain.id) or DomainBranding(site_domain_id=domain.id)
    for field, value in values.items():
        setattr(row, field, value)
    db.session.add(row)
    db.session.commit()
    clear_domain_cache()


def test_default_domain_is_the_site(two_domains, site_values):
    default, _onion = two_domains.domains
    assert branding_for(default)._asdict() == site_values
    assert own_branding(default) is None


def test_blank_fields_inherit_per_field(two_domains, site_values):
    default, onion = two_domains.domains
    set_branding(onion, name='Onion name', description='', logo=None)
    assert branding_for(onion)._asdict() == {**site_values, 'name': 'Onion name'}
    assert own_branding(onion).name == 'Onion name'


def test_a_domain_with_no_row_is_the_site(two_domains, site_values):
    assert branding_for(two_domains.domains[1])._asdict() == site_values


def test_own_values_win(two_domains, site_values):
    _default, onion = two_domains.domains
    set_branding(onion, **ONION_VALUES, about='onion about')
    assert branding_for(onion)._asdict() == ONION_VALUES


def test_a_default_row_is_ignored(two_domains, site_values):
    default, _onion = two_domains.domains
    set_branding(default, name='Not used')
    assert branding_for(default).name == site_values['name']


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


def test_saved_change_is_seen_after_clear_domain_cache(app, two_domains, site_values, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    clear_domain_cache()
    onion = two_domains.domains[1]
    assert branding_for(onion).name == site_values['name']
    db.session.add(DomainBranding(site_domain_id=onion.id, name='Later'))
    db.session.commit()
    assert branding_for(onion).name == site_values['name']   # memoised
    clear_domain_cache()
    assert branding_for(onion).name == 'Later'


def test_deleting_a_domain_deletes_its_branding(two_domains, site_values):
    onion = two_domains.domains[1]
    set_branding(onion, name='Onion name')
    db.session.expire_all()
    db.session.query(SiteDomain).filter_by(id=onion.id).delete()
    db.session.commit()
    assert db.session.get(DomainBranding, onion.id) is None


def load_migration(pattern):
    path = next(Path(__file__).parent.parent.glob(f'migrations/versions/{pattern}'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_migration_creates_and_drops_the_table(db_session):
    """The table, then its icon columns (Task 5), each dropped and made again in revision order: the database is left
    as `flask db upgrade` made it."""
    migration = load_migration('*_domain_branding.py')
    icons = load_migration('*_domain_branding_icons.py')
    db.session.commit()

    def columns():
        return {column['name'] for column in inspect(db.session.connection()).get_columns('domain_branding')}
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            icons.downgrade()
            assert {'logo_192', 'logo_512'}.isdisjoint(columns())
            migration.downgrade()
            assert not inspect(db.session.connection()).has_table('domain_branding')
            migration.upgrade()
            assert inspect(db.session.connection()).has_table('domain_branding')
            icons.upgrade()
            assert {'logo_192', 'logo_512'} <= columns()
        db.session.commit()
    finally:
        db.session.rollback()
