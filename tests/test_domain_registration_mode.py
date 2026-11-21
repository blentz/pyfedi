"""One way of saying who may register, and one of who may read (spec 2026-11-20-multitenant-domain-config MT6, MT7).

Each domain has a registration mode (Open, RequireApplication, Closed, ReferralCode) and a private instance switch; a
sister's blank one is the first domain's. Whether a domain's members pay is its own, never inherited, and a paid
domain's registration mode still applies: closed, nobody new joins while its members go on paying. Migration
f5b1d8e3a7c9 writes out each domain's behaviour from the columns a sister had before (`registration`,
`login_required`).
"""
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import g
from sqlalchemy import inspect, text

from app import db
from app.domain_config import effective, site_view
from app.models import SiteDomain, first_config, utcnow
from app.storefront import (clear_domain_cache, default_domain, domain_by_id, open_registrations, paid_registration,
                            public_registration_open, registration_mode_of, registration_mode_on, signup_open_to)
from app.utils import login_required_here
from tests.schema_history import load_revision
from tests.two_domains import request_on

MIGRATION = 'f5b1d8e3a7c9_domain_config_one_registration.py'


@pytest.fixture
def sister(app, site):
    db.session.add(SiteDomain(domain=app.config['SERVER_NAME'], scheme='https', federates=True, is_default=True))
    db.session.add(SiteDomain(domain='sister.example', scheme='https', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield db.session.query(SiteDomain).filter_by(domain='sister.example').one()
    clear_domain_cache()


def info(row):
    return domain_by_id(row.id)


def change(row, **values):
    for name, value in values.items():
        setattr(row, name, value)
    db.session.commit()


# ---------------------------------------------------------------------------------------------------------------------
# Who may register

def test_a_sisters_blank_mode_is_the_first_domains_and_its_own_is_its_own(sister):
    change(first_config(), registration_mode='RequireApplication')
    assert sister.registration_mode is None and registration_mode_of(info(sister)) == 'RequireApplication'
    change(sister, registration_mode='Closed')
    assert registration_mode_of(info(sister)) == 'Closed'
    assert registration_mode_of(default_domain()) == 'RequireApplication'


@pytest.mark.parametrize('mode, paid, public, advertised, open_', [
    ('Open', False, True, 'Open', True),
    ('RequireApplication', False, True, 'RequireApplication', True),
    ('ReferralCode', False, True, 'RequireApplication', False),   # RC6: offered as an application
    ('Closed', False, False, 'Closed', False),
    ('Open', True, False, 'Closed', False),                       # paid: only /register, which leads to paying
    ('ReferralCode', True, False, 'RequireApplication', False),   # M4: a paid domain still takes a code
    ('Closed', True, False, 'Closed', False),
])
def test_what_a_domain_offers(sister, mode, paid, public, advertised, open_):
    change(sister, registration_mode=mode, paid_membership=paid)
    domain = info(sister)
    assert (paid_registration(domain), public_registration_open(domain)) == (paid, public)
    assert (registration_mode_on(domain), open_registrations(domain)) == (advertised, open_)


def test_a_closed_domain_is_opened_by_a_code_only_where_codes_are_on(sister):
    change(sister, registration_mode='Closed')
    assert signup_open_to(info(sister), True) is True and signup_open_to(info(sister), False) is False
    change(sister, referral_codes=False)
    assert signup_open_to(info(sister), True) is False


def test_whether_members_pay_is_never_the_first_domains(sister):
    first = db.session.query(SiteDomain).filter_by(is_default=True).one()
    first.set_paid_membership(True)
    db.session.commit()
    assert paid_registration(default_domain()) and not paid_registration(info(sister))


def test_paying_begins_once_and_ends_with_its_date(sister):
    sister.set_paid_membership(True)
    db.session.commit()
    since = sister.paid_since
    assert since is not None and since <= utcnow()
    sister.set_paid_membership(True)   # saving again keeps the date (plan M2)
    assert sister.paid_since == since
    sister.set_paid_membership(False)
    db.session.commit()
    assert (sister.paid_membership, sister.paid_since) == (False, None)


# ---------------------------------------------------------------------------------------------------------------------
# Who may read

def test_a_new_domain_is_readable_without_login_whatever_the_first_domain_says(app, sister):
    assert sister.private_instance is None and effective(sister)['private_instance'] is False   # the first domain's
    change(first_config(), private_instance=True)
    new = SiteDomain(domain='new.example', scheme='https', federates=False)
    given = SiteDomain(domain='another.example', scheme='https', federates=False, private_instance=True)
    db.session.add_all([new, given])
    db.session.commit()
    assert new.private_instance is False and effective(new)['private_instance'] is False
    assert given.private_instance is True   # one it is given is kept


def test_a_sisters_blank_login_is_the_first_domains(app, sister):
    change(sister, private_instance=None)
    for private in (True, False):
        change(first_config(), private_instance=private)
        with request_on(app, info(sister)):
            g.site = site_view({}, info(sister))   # as before_request makes it
            assert login_required_here() is private


def test_a_federating_domain_cannot_be_made_private_by_inheriting(app, site, admin_domain_branding_admin,
                                                                   admin_domain_branding_domains):
    """MT7: a federating sister left to the first domain's login is refused when that would require it."""
    from tests.test_admin_misc_tenant import post
    first_config().private_instance = True
    db.session.commit()
    fed = SiteDomain(domain='fed.example', scheme='https', federates=True)
    db.session.add(fed)
    db.session.commit()
    clear_domain_cache()
    response = post(admin_domain_branding_admin, fed, private_instance='', registration_offer_id='0',
                    lapse_grace_days='7')
    assert 'already left this server' in response.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(SiteDomain, fed.id).private_instance is False
    post(admin_domain_branding_admin, fed, private_instance='n', registration_offer_id='0', lapse_grace_days='7')
    db.session.expire_all()
    assert db.session.get(SiteDomain, fed.id).private_instance is False


# ---------------------------------------------------------------------------------------------------------------------
# The domains page

def test_the_domains_page_shows_each_domains_mode_and_whether_its_members_pay(site, admin_domain_branding_admin,
                                                                             admin_domain_branding_domains):
    shop = db.session.get(SiteDomain, admin_domain_branding_domains['shop'].id)
    shop.registration_mode = 'Closed'
    shop.paid_membership = True
    first_config().registration_mode = 'Open'
    db.session.commit()
    body = admin_domain_branding_admin[0].get('/admin/domains').get_data(as_text=True)
    assert '<td>Closed (members pay)</td>' in body and '<td>Open</td>' in body


def test_a_site_with_no_domain_rows_shows_the_first_domains_mode(app, site):
    from tests.test_admin_domains import _admin, login
    first_config().registration_mode = 'RequireApplication'
    db.session.commit()
    client = app.test_client()
    login(client, _admin())
    body = client.get('/admin/domains').get_data(as_text=True)
    assert '<td>RequireApplication</td>' in body


# ---------------------------------------------------------------------------------------------------------------------
# Migration f5b1d8e3a7c9

@pytest.fixture
def before(app, site):
    """The schema before the migration: the first domain's row in referral-code mode and paid, and a sister for each
    case, one with no configuration row."""
    first_config().registration_mode = 'ReferralCode'
    db.session.commit()
    migration = load_revision(MIGRATION)
    connection = db.session.connection()
    try:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            connection.execute(text("UPDATE domain_config SET registration = 'paid' WHERE site_domain_id IS NULL"))
            for name, values in {
                    'opened': "'closed', NULL, NULL, NULL",            # a code opened it: referral mode, codes on
                    'refusing': "'closed', NULL, false, NULL",         # RC11a: codes off
                    'own-open': "'closed', 'Open', NULL, NULL",        # its own mode was not referral codes
                    'paid': "'paid', NULL, NULL, true",
                    'anyone': "'anyone', 'Open', NULL, NULL",
                    'no-row': None}.items():
                connection.execute(text("INSERT INTO instance (domain, software) VALUES (:d, 'piefed')"),
                                   {'d': f'{name}.example'})
                connection.execute(text(
                    "INSERT INTO site_domain (domain, scheme, federates, is_default, is_onion, instance_id) "
                    "SELECT :d, 'https', false, false, false, id FROM instance WHERE domain = :d"), {'d': f'{name}.example'})
                if values is not None:
                    connection.execute(text(
                        "INSERT INTO domain_config (site_domain_id, registration, registration_mode, referral_codes, "
                        f"login_required) SELECT id, {values} FROM site_domain WHERE domain = :d"), {'d': f'{name}.example'})
            yield migration, connection
    finally:
        db.session.rollback()
        clear_domain_cache()


def rows(connection, *columns):
    names = ', '.join(f'c.{column}' for column in columns)
    return {row[0]: tuple(row[1:]) for row in connection.execute(text(
        f"SELECT coalesce(split_part(d.domain, '.', 1), 'first'), {names} FROM domain_config c "
        'LEFT JOIN site_domain d ON d.id = c.site_domain_id'))}


def test_the_upgrade_writes_out_each_domains_behaviour(before):
    migration, connection = before
    migration.upgrade()
    found = rows(connection, 'registration_mode', 'paid_membership', 'private_instance')
    assert found['first'][:2] == ('ReferralCode', True)
    assert found['opened'] == ('ReferralCode', False, False)
    assert found['refusing'] == ('Closed', False, False)
    assert found['own-open'] == ('Closed', False, False)
    assert found['paid'] == (None, True, True)   # its mode is still the first domain's
    assert found['anyone'] == ('Open', False, False)
    assert found['no-row'] == (None, False, False)   # given a row: the first domain's login is not its own
    columns = {column['name'] for column in inspect(connection).get_columns('domain_config')}
    assert not columns & {'registration', 'login_required'}


def test_the_downgrade_writes_the_old_columns_back(before):
    migration, connection = before
    migration.upgrade()
    migration.downgrade()
    found = rows(connection, 'registration', 'login_required', 'private_instance')
    assert found['first'][0] == 'paid'
    assert found['opened'] == ('anyone', False, None)   # in referral-code mode a code opens it, as before
    assert found['refusing'] == ('closed', False, None)
    assert found['own-open'] == ('closed', False, None)
    assert found['paid'] == ('paid', True, None)   # its login back to the first domain's, as sisters' were
    assert found['anyone'] == ('anyone', False, None)
    assert 'paid_membership' not in {column['name'] for column in inspect(connection).get_columns('domain_config')}
