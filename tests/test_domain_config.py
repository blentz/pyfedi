"""Each domain's configuration (spec 2026-11-20-multitenant-domain-config-design.md): the first domain's row, a sister's
row inheriting what it leaves blank (MT3), `Site` and `SiteDomain` reading and writing their old names through it,
`g.site` as the request's domain's view, and migration b9e4d2a7c1f6, which moved the values into it."""
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cachelib import SimpleCache
from flask import g
from sqlalchemy import inspect, text

from app import cache, db
from app.domain_config import _CHANGED as CHANGED, SiteView, effective, own_config, resolve, site_view
from app.models import FIRST_DEFAULTS, OWN_COLUMNS, TENANT_COLUMNS, DomainConfig, Site, SiteDomain, first_config
from app.storefront import clear_domain_cache, default_domain, domain_by_id
from app.utils import get_site_as_dict
from tests.factories import config_of, configure
from tests.two_domains import FIRST, ONION

pytestmark = pytest.mark.usefixtures('site')


def sister(**values) -> SiteDomain:
    row = SiteDomain(domain='sister.example', scheme='https', federates=True, **values)
    db.session.add(row)
    db.session.commit()
    clear_domain_cache()
    return row


# ---------------------------------------------------------------------------------------------------------------------
# The first domain's row

def test_the_first_domains_row_starts_with_the_sites_old_defaults(site):
    db.session.query(DomainConfig).delete()
    db.session.commit()
    row = first_config()
    assert {name: getattr(row, name) for name in FIRST_DEFAULTS} == FIRST_DEFAULTS
    assert row.site_domain_id is None


def test_the_first_domains_row_is_one_object_until_it_is_flushed(site):
    db.session.query(DomainConfig).delete()
    db.session.commit()
    first_config().name = 'One'
    first_config().description = 'Two'   # not a second pending row
    db.session.commit()
    assert db.session.query(DomainConfig).count() == 1
    assert (site.name, site.description) == ('One', 'Two')


def test_a_rolled_back_first_row_is_made_again(site):
    db.session.query(DomainConfig).delete()
    db.session.commit()
    first_config().name = 'Rolled back'
    db.session.rollback()
    assert first_config().name is None   # a new row, with the defaults
    db.session.commit()
    assert db.session.query(DomainConfig).count() == 1


def test_only_one_row_may_be_the_first_domains(site):
    first_config()
    db.session.commit()
    db.session.add(DomainConfig(name='A second first row'))
    with pytest.raises(Exception, match='ix_domain_config_first'):
        db.session.commit()
    db.session.rollback()


def test_site_reads_and_writes_the_first_domains_row(site):
    site.enable_nsfw = True
    site.name = 'Renamed'
    db.session.commit()
    db.session.expire_all()
    row = db.session.query(DomainConfig).filter(DomainConfig.site_domain_id.is_(None)).one()
    assert (row.enable_nsfw, row.name) == (True, 'Renamed')
    assert 'name' not in {column.name for column in Site.__table__.columns}


# ---------------------------------------------------------------------------------------------------------------------
# A sister domain's row

def test_a_sister_with_no_row_inherits_everything(site):
    row = sister()
    assert own_config(row.id) is None
    assert effective(row) == effective(default_domain())


def test_a_sisters_null_and_blank_inherit_and_its_own_values_do_not(site):
    site.name, site.tos_url, site.enable_downvotes = 'First', 'https://first.example/tos', True
    row = sister()
    configure(row, name='', tos_url=None, enable_downvotes=False, description='Its own')
    db.session.commit()
    values = effective(row)
    assert (values['name'], values['tos_url']) == ('First', 'https://first.example/tos')
    assert (values['enable_downvotes'], values['description']) == (False, 'Its own')   # False is a value


def test_a_sister_with_a_logo_of_its_own_takes_its_sizes_and_dark_logo_from_itself(site):
    site.logo, site.logo_32, site.logo_dark = '/first.png', '/first-32.png', '/first-dark.png'
    first = first_config()
    own = DomainConfig(logo='/own.png')
    values = resolve(own, first)
    assert (values['logo'], values['logo_32'], values['logo_dark']) == ('/own.png', '', '')
    dark_only = resolve(DomainConfig(logo_dark='/own-dark.png'), first)
    assert (dark_only['logo'], dark_only['logo_dark'], dark_only['logo_dark_152']) == ('/first.png', '/own-dark.png', '')


def test_the_first_domains_values_are_its_own_none_included(site):
    site.tos_url = None
    assert resolve(None, first_config())['tos_url'] is None
    assert set(resolve(None, first_config())) == set(TENANT_COLUMNS)


# ---------------------------------------------------------------------------------------------------------------------
# A domain's own storefront settings

def test_a_domains_own_settings_default_when_unset(site):
    row = sister()
    assert {name: getattr(row, name) for name in OWN_COLUMNS} == OWN_COLUMNS
    assert own_config(row.id) is None   # reading made no row


def test_a_sisters_own_settings_are_its_rows_and_never_inherited(site):
    first_config().registration = 'closed'
    row = sister(registration='paid', lapse_grace_days=3)
    assert own_config(row.id).registration == 'paid'
    assert domain_by_id(row.id).lapse_grace_days == 3
    other = SiteDomain(domain='other.example', scheme='https', federates=True)
    db.session.add(other)
    db.session.commit()
    assert other.registration == 'anyone'   # not the first domain's


def test_the_default_domains_own_settings_are_the_first_domains_row(site):
    paid = datetime(2026, 1, 1)
    row = SiteDomain(paid_since=paid, domain=FIRST, scheme='https', federates=True, is_default=True)
    db.session.add(row)
    db.session.commit()
    assert first_config().paid_since == paid and row.own_config is None


def test_a_sisters_row_goes_with_it(site):
    row = sister(registration='closed')
    row_id = row.id
    db.session.delete(row)
    db.session.commit()
    assert own_config(row_id) is None and db.session.query(DomainConfig).count() == 1


def test_a_config_lookup_without_a_domain_is_none():
    assert own_config(None) is None


# ---------------------------------------------------------------------------------------------------------------------
# What is memoised, and when it goes

def test_a_commit_that_writes_a_configuration_clears_the_memo(app, site, monkeypatch):
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    row = sister()
    assert effective(row)['description'] == site.description
    config_of(row).description = 'Changed'
    db.session.flush()
    assert effective(row)['description'] == site.description   # memoised
    db.session.commit()
    assert effective(row)['description'] == 'Changed'


def test_a_rolled_back_write_owes_no_clear(site):
    row = sister()
    config_of(row).description = 'Never kept'
    db.session.flush()
    assert db.session.info[CHANGED]
    db.session.rollback()
    assert CHANGED not in db.session.info


def test_a_commit_writing_no_configuration_clears_nothing(site):
    site.blocked_phrases = 'instance-wide'
    db.session.commit()
    assert CHANGED not in db.session.info


# ---------------------------------------------------------------------------------------------------------------------
# g.site

def test_g_site_is_the_requests_domains_view(app, site):
    site.name = 'First name'
    db.session.commit()
    with app.test_request_context('/', base_url=f'https://{FIRST}'):
        app.preprocess_request()
        assert isinstance(g.site, SiteView)
        assert (g.site.name, g.site.id) == ('First name', 1)


def test_a_sisters_view_has_its_values_over_the_sites(site):
    row = sister()
    configure(row, name='Sister name')
    db.session.commit()
    view = site_view(get_site_as_dict(), row)
    assert view.name == 'Sister name'
    assert view.blocked_phrases == site.blocked_phrases   # instance-wide
    assert 'private_key' not in vars(view)


def test_the_view_offers_sites_methods_and_nothing_else(site):
    view = site_view(get_site_as_dict(), default_domain())
    assert view.admins() == Site.admins()
    assert view.active_now() == site.active_now()
    with pytest.raises(AttributeError):
        view.no_such_thing


def test_writing_to_the_view_writes_nothing_else(site):
    view = site_view(get_site_as_dict(), default_domain())
    view.registration_mode = 'Open'
    assert first_config().registration_mode != 'Open'


# ---------------------------------------------------------------------------------------------------------------------
# Migration b9e4d2a7c1f6

def load():
    path = next(Path(__file__).parent.parent.glob('migrations/versions/b9e4d2a7c1f6_domain_config.py'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def old_schema(app, db_session):
    """The schema before the migration, with a site, a default domain row and two sisters, one branded."""
    db.session.commit()
    migration = load()
    connection = db.session.connection()
    try:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            connection.execute(text(
                "UPDATE site SET name = 'Old name', enable_nsfw = true, tos_url = NULL, registration_mode = 'Open' "
                'WHERE id = 1'))
            for domain, registration, default, lapse in ((FIRST, 'closed', True, 9), ('branded.example', 'paid', False, 7),
                                                         (ONION, 'anyone', False, 7)):
                connection.execute(text(
                    "INSERT INTO instance (domain, software) VALUES (:d, 'piefed')"), {'d': domain})
                connection.execute(text(
                    "INSERT INTO site_domain (domain, scheme, federates, registration, is_default, is_onion, "
                    "instance_id, login_required, referral_codes, lapse_grace_days) "
                    "SELECT :d, 'https', :f, :r, :f, false, id, NOT :f, NOT :f, :l FROM instance WHERE domain = :d"),
                    {'d': domain, 'r': registration, 'f': default, 'l': lapse})
            connection.execute(text(
                "INSERT INTO domain_branding (site_domain_id, name, logo) "
                "SELECT id, 'Branded', '/b.png' FROM site_domain WHERE domain = 'branded.example'"))
            yield migration, connection
    finally:
        db.session.rollback()


def config_rows(connection):
    return {row.domain: row for row in connection.execute(text(
        "SELECT coalesce(d.domain, 'first') AS domain, c.* FROM domain_config c "
        'LEFT JOIN site_domain d ON d.id = c.site_domain_id')).mappings()}


def test_the_upgrade_moves_every_domains_values_into_its_row(old_schema):
    migration, connection = old_schema
    migration.upgrade()
    rows = config_rows(connection)
    assert set(rows) == {'first', 'branded.example', ONION}
    first = rows['first']
    assert (first['name'], first['enable_nsfw'], first['tos_url'], first['registration_mode']) == \
        ('Old name', True, None, 'Open')
    assert (first['registration'], first['referral_codes'], first['lapse_grace_days']) == ('closed', False, 9)
    branded = rows['branded.example']
    assert (branded['name'], branded['logo'], branded['registration'], branded['login_required']) == \
        ('Branded', '/b.png', 'paid', True)
    assert (rows[ONION]['name'], rows[ONION]['enable_nsfw']) == (None, None)   # inherits
    inspector = inspect(connection)
    assert not inspector.has_table('domain_branding')
    assert 'name' not in {column['name'] for column in inspector.get_columns('site')}
    assert 'registration' not in {column['name'] for column in inspector.get_columns('site_domain')}


def test_the_downgrade_puts_every_value_back(old_schema):
    migration, connection = old_schema
    migration.upgrade()
    connection.execute(text("UPDATE domain_config SET referral_codes = NULL WHERE referral_codes IS NOT NULL "
                            "AND site_domain_id IS NOT NULL"))
    migration.downgrade()
    site = connection.execute(text('SELECT name, enable_nsfw, registration_mode FROM site WHERE id = 1')).one()
    assert tuple(site) == ('Old name', True, 'Open')
    domains = dict(connection.execute(text(
        'SELECT domain, (registration, referral_codes, lapse_grace_days) FROM site_domain')).all())
    assert domains[FIRST] == '(closed,f,9)'
    assert domains['branded.example'] == '(paid,t,7)'   # a NULL is its old server default
    branding = connection.execute(text('SELECT name, logo FROM domain_branding')).all()
    assert [tuple(row) for row in branding] == [('Branded', '/b.png')]   # an empty row makes none
    assert not inspect(connection).has_table('domain_config')


def test_the_upgrade_without_a_site_or_default_row_makes_no_first_row(app, db_session):
    migration = load()
    connection = db.session.connection()
    try:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            connection.execute(text('DELETE FROM site'))
            migration.upgrade()
            assert config_rows(connection) == {}
    finally:
        db.session.rollback()
