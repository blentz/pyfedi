"""A site with Stripe in its environment keeps its plans and its subscribers through the deploy (C5, C6)."""
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from app import db
from app.constants import DONOR_WHILE_SUBSCRIBED
from tests.factories import make_instance, make_user

TABLES = ('payment_provider', 'offer', 'offer_domain', 'payment', 'member_subscription', 'payment_event')


def load():
    path = next(Path(__file__).parent.parent.glob('migrations/versions/*_payments.py'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def conn(app, db_session, monkeypatch):
    for name in ('STRIPE_MONTHLY_SMALL', 'STRIPE_MONTHLY_BIG', 'STRIPE_MONTHLY_SMALL_TEXT', 'STRIPE_MONTHLY_BIG_TEXT'):
        monkeypatch.delenv(name, raising=False)
    instance = make_instance('local.example')
    donor = make_user(instance, 'donor', local=True)
    donor.stripe_subscription_id = 'sub_one'
    donor.stripe_customer_id = 'cus_one'
    blank = make_user(instance, 'blank', local=True)
    blank.stripe_subscription_id = ''
    make_user(instance, 'never', local=True)
    db.session.commit()
    connection = db.session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        load().downgrade()
        yield connection
    db.session.rollback()


def upgrade():
    load().upgrade()


def test_the_downgrade_removes_it_all_and_keeps_the_stripe_columns(conn):
    inspector = inspect(conn)
    assert not any(inspector.has_table(table) for table in TABLES)
    columns = {column['name'] for column in inspector.get_columns('user')}
    assert 'donor_until' not in columns and {'stripe_customer_id', 'stripe_subscription_id'} <= columns


def test_it_makes_every_table(conn):
    upgrade()
    assert all(inspect(conn).has_table(table) for table in TABLES)


def test_a_site_with_no_stripe_gets_no_offers(conn):
    upgrade()
    assert conn.execute(text('SELECT count(*) FROM offer')).scalar() == 0


def test_a_sites_plans_become_legacy_offers_granting_the_label(conn, monkeypatch):
    monkeypatch.setenv('STRIPE_MONTHLY_SMALL', 'price_small')
    monkeypatch.setenv('STRIPE_MONTHLY_SMALL_TEXT', 'Pay 5')
    monkeypatch.setenv('STRIPE_MONTHLY_BIG', 'price_big')
    upgrade()
    rows = conn.execute(text('SELECT provider_kind, kind, title, offer_group, provider_price_id, amount_minor, '
                             'interval, grants_donor, position, active FROM offer ORDER BY position')).fetchall()
    assert [tuple(row) for row in rows] == [
        ('stripe', 'subscription', 'Pay 5', 'legacy', 'price_small', None, 'month', True, 0, True),
        ('stripe', 'subscription', 'price_big', 'legacy', 'price_big', None, 'month', True, 1, True)]


def test_subscribers_carry_over_with_the_label(conn):
    upgrade()
    rows = conn.execute(text('SELECT u.user_name, s.offer_id, s.provider_kind, s.provider_reference, s.status '
                             'FROM member_subscription s JOIN "user" u ON u.id = s.user_id')).fetchall()
    assert [tuple(row) for row in rows] == [('donor', None, 'stripe', 'sub_one', 'active')]
    until = dict(conn.execute(text('SELECT user_name, donor_until FROM "user" '
                                   "WHERE user_name IN ('donor', 'blank', 'never')")).fetchall())
    assert until == {'donor': DONOR_WHILE_SUBSCRIBED, 'blank': None, 'never': None}


def test_the_sentinel_is_the_apps(conn):
    assert datetime.fromisoformat(load().WHILE_SUBSCRIBED) == DONOR_WHILE_SUBSCRIBED
