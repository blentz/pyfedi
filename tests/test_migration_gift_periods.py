"""Migration c6f1b4e8a2d5: a gift's days become a number of its tier's billing periods, rounded up (owner 2026-11-17)."""
import importlib.util
import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app import db
from app.models import Offer
from tests.factories import make_instance, make_user


def load():
    path = next(Path(__file__).parent.parent.glob('migrations/versions/c6f1b4e8a2d5_gift_periods.py'))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def world(app, db_session):
    month = Offer(provider_kind='btcpay', kind='subscription', title='Month', offer_group='tiers', amount_minor=500,
                  currency='usd', interval='month')
    year = Offer(provider_kind='btcpay', kind='subscription', title='Year', offer_group='tiers', amount_minor=5000,
                 currency='usd', interval='year')
    db.session.add_all([month, year])
    db.session.commit()
    owner = make_user(make_instance('local.example'), 'referrer', local=True)
    ids = dict(month=month.id, year=year.id, owner=owner.id)
    db.session.commit()
    migration = load()
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            migration.downgrade()
            yield migration, ids
    finally:
        db.session.rollback()


def code(name, days, offer_id=None, owner_id=None):
    db.session.execute(text('INSERT INTO promo_code (code, kind, offer_id, days, owner_id, active) '
                            "VALUES (:c, 'gift', :o, :d, :w, true)"), {'c': name, 'o': offer_id, 'd': days, 'w': owner_id})


def periods(name):
    return db.session.execute(text('SELECT periods FROM promo_code WHERE code = :c'), {'c': name}).scalar()


def setting(name, value=None, write=False):
    if write:
        db.session.execute(text('DELETE FROM settings WHERE name = :n'), {'n': name})
        db.session.execute(text('INSERT INTO settings (name, value) VALUES (:n, :v)'), {'n': name, 'v': json.dumps(value)})
        return None
    row = db.session.execute(text('SELECT value FROM settings WHERE name = :n'), {'n': name}).scalar()
    return json.loads(row) if row is not None else None


@pytest.mark.parametrize('days, offer, expected', [(30, 'month', 1), (45, 'month', 2), (365, 'month', 13),
                                                   (365, 'year', 1), (400, 'year', 2), (60, None, 2)],
                         ids=['a-month', 'rounded-up', 'a-year-on-months', 'a-year', 'over-a-year', 'no-tier'])
def test_days_become_periods_rounded_up(world, days, offer, expected):
    migration, ids = world
    code('the-code', days, ids.get(offer), None if offer else ids['owner'])
    migration.upgrade()
    assert periods('the-code') == expected


def test_a_code_without_days_has_no_periods(world):
    migration, ids = world
    db.session.execute(text("INSERT INTO promo_code (code, kind, percent, active) VALUES ('ten-off', 'percent', 10, true)"))
    migration.upgrade()
    assert periods('ten-off') is None


def test_the_referral_default_counts_in_periods(world):
    migration, ids = world
    setting('referral_benefit', {'kind': 'gift', 'offer_id': ids['year'], 'days': 400}, write=True)
    migration.upgrade()
    assert setting('referral_benefit') == {'kind': 'gift', 'offer_id': ids['year'], 'periods': 2}


def test_a_default_from_before_r3_is_moved_into_it(world):
    migration, ids = world
    setting('referral_gift_offer_id', ids['month'], write=True)
    setting('referral_gift_days', 60, write=True)
    migration.upgrade()
    assert setting('referral_benefit') == {'kind': 'gift', 'offer_id': ids['month'], 'periods': 2}
    assert setting('referral_gift_offer_id') is None and setting('referral_gift_days') is None


def test_a_default_with_no_days_stays_one_period(world):
    migration, ids = world
    setting('referral_benefit', {'kind': 'gift', 'offer_id': ids['month'], 'days': None}, write=True)
    migration.upgrade()
    assert setting('referral_benefit') == {'kind': 'gift', 'offer_id': ids['month']}


def test_the_downgrade_gives_days_back(world):
    migration, ids = world
    code('a-year', 365, ids['year'])
    setting('referral_benefit', {'kind': 'gift', 'offer_id': ids['year'], 'days': 365}, write=True)
    migration.upgrade()
    migration.downgrade()
    assert db.session.execute(text("SELECT days FROM promo_code WHERE code = 'a-year'")).scalar() == 365
    assert setting('referral_benefit') == {'kind': 'gift', 'offer_id': ids['year'], 'days': 365}
    migration.upgrade()
