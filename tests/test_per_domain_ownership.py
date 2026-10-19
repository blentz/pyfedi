"""Each local account and community belongs to one hosted domain (per-domain identity, plan B task 1)."""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import SiteDomain, User
from app.storefront import clear_domain_cache, default_domain, domain_by_id
from tests.factories import make_community, make_instance, make_user, seed_community_owner

_MIGRATION = (Path(__file__).resolve().parent.parent / 'migrations' / 'versions' /
              'b7e3c1a9d5f2_per_domain_ownership.py')
_spec = importlib.util.spec_from_file_location('per_domain_ownership_migration', _MIGRATION)
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


@pytest.fixture
def domains(db_session):
    """The default clearnet row and an onion row."""
    default = db.session.query(SiteDomain).filter_by(is_default=True).first()
    if default is None:   # db_session may or may not have cleared the migration-seeded default row
        default = SiteDomain(domain='default.example', is_default=True)
        db.session.add(default)
    onion = SiteDomain(domain='abc.onion', scheme='http', federates=False)
    db.session.add(onion)
    db.session.commit()
    clear_domain_cache()
    yield default, onion
    clear_domain_cache()


def test_the_migration_backfills_every_local_user_and_community_to_the_default_domain(app, db_session, domains):
    default, onion = domains
    peer = seed_community_owner()
    remote = make_user(peer, 'remote')
    local = make_user(None, 'local', local=True)
    community = make_community('local_comm')
    remote_community = make_community('remote_comm')
    remote_community.ap_id = 'remote_comm@peer.example'
    db.session.commit()
    db.session.execute(text(migration.BACKFILL_USERS))
    db.session.execute(text(migration.BACKFILL_COMMUNITIES))
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(User, local.id).site_domain_id == default.id
    assert db.session.get(User, remote.id).site_domain_id is None
    assert community.site_domain_id == default.id
    assert remote_community.site_domain_id is None


def test_a_local_name_is_unique_per_domain_case_insensitively(app, db_session, domains):
    default, onion = domains
    make_instance('peer.example')
    make_user(None, 'Wakko', local=True).site_domain_id = default.id
    db.session.commit()
    clash = make_user(None, 'wakko', local=True)
    clash.email = 'other@example.com'
    clash.site_domain_id = default.id
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    clash = make_user(None, 'wakko', local=True)
    clash.site_domain_id = onion.id
    db.session.commit()
    assert clash.id is not None


def test_a_community_name_is_unique_per_domain_case_insensitively(app, db_session, domains):
    default, onion = domains
    seed_community_owner()
    make_community('Pets').site_domain_id = default.id
    db.session.commit()
    clash = make_community('pets')
    clash.site_domain_id = default.id
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
    other = make_community('pets', host='abc.onion')
    other.site_domain_id = onion.id
    db.session.commit()
    assert other.id is not None


def test_remote_rows_are_not_held_to_the_index(app, db_session, domains):
    first = make_user(make_instance('one.example'), 'wakko')
    second = make_user(make_instance('two.example'), 'wakko')
    assert first.id != second.id and first.site_domain_id is None and second.site_domain_id is None


def test_domain_by_id_falls_back_to_the_default(app, db_session, domains):
    default, onion = domains
    assert domain_by_id(None) == default_domain()
    assert domain_by_id(999999) == default_domain()
    assert domain_by_id(onion.id).domain == 'abc.onion'
    assert domain_by_id(onion.id).id == onion.id


def test_the_synthesised_default_has_no_id(app, db_session):
    clear_domain_cache()
    assert default_domain().id is None
    assert domain_by_id(5) == default_domain()


def test_home_domain_reads_the_owning_row(app, db_session, domains):
    default, onion = domains
    seed_community_owner()
    user = make_user(None, 'wakko', local=True)
    community = make_community('pets')
    assert user.home_domain() == default_domain()
    user.site_domain_id = community.site_domain_id = onion.id
    db.session.commit()
    assert user.home_domain().domain == 'abc.onion'
    assert community.home_domain().domain == 'abc.onion'
