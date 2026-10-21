"""Each hosted domain has its own instance row; the default keeps row 1 (plan C2 task 1, spec 2026-10-19 §3.1)."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cachelib import SimpleCache
from sqlalchemy import inspect, text

from app import cache, db
from app.models import Instance, SiteDomain
from app.storefront import (clear_domain_cache, current_instance_id, default_domain, domain_for_host,
                            hosted_instance_ids, instance_id_of)
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user
from tests.test_admin_domains import _admin, _form
from tests.test_admin_site_profile import csrf, login
from tests.two_domains import HELL, ONION, default_request, onion_request, two_domains  # noqa: F401

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'c8f4d2a6b9e1_per_domain_instance.py'


def load_migration():
    spec = importlib.util.spec_from_file_location('per_domain_instance_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def real_cache(app, monkeypatch):
    backend = SimpleCache()
    monkeypatch.setitem(app.extensions['cache'], cache, backend)
    yield backend
    clear_domain_cache()


def test_the_default_domain_keeps_instance_one(app, db_session, two_domains):
    hell, onion = two_domains.domains
    assert db.session.query(SiteDomain).filter_by(is_default=True).one().instance_id == 1
    assert (hell.instance_id, instance_id_of(hell), instance_id_of(default_domain())) == (1, 1, 1)
    assert instance_id_of(onion) not in (None, 1)


def test_the_default_row_is_instance_one_even_when_another_row_has_its_domain_spelt_otherwise(app, db_session):
    make_instance(HELL, software='piefed')
    make_instance(HELL.upper())   # a sparse row find_instance_id could have made from a mixed-case host
    db.session.add(SiteDomain(domain=HELL, is_default=True))
    db.session.commit()
    clear_domain_cache()
    assert default_domain().instance_id == 1


def test_a_synthesised_default_is_instance_one(app, db_session):
    """An install with no site_domain rows serves SERVER_NAME alone: no row id, but still row 1's domain."""
    assert default_domain().id is None
    assert instance_id_of(default_domain()) == 1
    assert hosted_instance_ids() == frozenset({1})
    assert current_instance_id() == 1


def test_adding_a_domain_creates_its_instance_row_without_an_inbox(app, db_session, site):
    client = app.test_client()
    login(client, _admin())    # makes row 1, test.piefed.local
    response = client.post('/admin/domains', data=_form(csrf(app, client)))
    assert response.status_code == 302
    seeded = db.session.query(SiteDomain).filter_by(is_default=True).one()
    added = db.session.query(SiteDomain).filter_by(domain=ONION).one()
    assert seeded.instance_id == 1
    instance = db.session.get(Instance, added.instance_id)
    assert (instance.domain, instance.software, instance.inbox, instance.shared_inbox) == \
           (ONION, 'PieFed', None, None)
    assert instance.id != 1
    assert domain_for_host(ONION).instance_id == instance.id


def test_adding_a_domain_reuses_an_instance_row_already_named_for_it(app, db_session, site):
    """find_instance_id may already have made a sparse row for the host. The unique domain index allows one row, so
    the domain takes it over, without the inbox a sister row must never have."""
    client = app.test_client()
    login(client, _admin())
    sparse = make_instance(ONION)
    sparse.inbox, sparse.shared_inbox = f'http://{ONION}/inbox', f'http://{ONION}/inbox'
    db.session.commit()
    sparse_id = sparse.id
    client.post('/admin/domains', data=_form(csrf(app, client)))
    db.session.expire_all()
    assert db.session.query(SiteDomain).filter_by(domain=ONION).one().instance_id == sparse_id
    reused = db.session.get(Instance, sparse_id)
    assert (reused.software, reused.inbox, reused.shared_inbox) == ('PieFed', None, None)
    assert db.session.query(Instance).filter_by(domain=ONION).count() == 1


def test_hosted_instance_ids_are_every_domains_row(app, db_session, two_domains):
    hell, onion = two_domains.domains
    assert hosted_instance_ids() == frozenset({1, onion.instance_id})
    db.session.add(SiteDomain(domain='shop.example', federates=False))
    db.session.commit()
    clear_domain_cache()
    shop = domain_for_host('shop.example')
    assert shop.instance_id not in (1, onion.instance_id)
    assert hosted_instance_ids() == frozenset({1, onion.instance_id, shop.instance_id})


def test_hosted_instance_ids_are_cached_with_the_domain_rows(app, db_session, two_domains, real_cache):
    hell, onion = two_domains.domains
    clear_domain_cache()
    before = hosted_instance_ids()
    db.session.add(SiteDomain(domain='shop.example', federates=False))
    db.session.commit()
    assert hosted_instance_ids() == before
    clear_domain_cache()
    assert hosted_instance_ids() == before | {domain_for_host('shop.example').instance_id}


def test_current_instance_id_follows_the_storefront_and_is_one_outside_a_request(app, db_session, two_domains):
    hell, onion = two_domains.domains
    assert current_instance_id() == 1
    with onion_request(app):
        assert current_instance_id() == onion.instance_id
    with default_request(app):
        assert current_instance_id() == 1


def test_local_rows_on_the_onion_carry_its_instance(app, db_session, two_domains):
    hell, onion = two_domains.domains
    assert two_domains.local_user('anna', onion).instance_id == onion.instance_id
    assert two_domains.local_community('cave', onion).instance_id == onion.instance_id
    assert two_domains.local_user('anna', hell).instance_id == 1
    assert two_domains.local_community('cave', hell).instance_id == 1


# --- the migration ---

def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('c8f4d2a6b9e1', 'b7e3c1a9d5f2')


def test_the_column_is_a_required_unique_link_to_an_instance(app, db_session):
    with db.engine.connect() as connection:
        found = inspect(connection)
        column = next(c for c in found.get_columns('site_domain') if c['name'] == 'instance_id')
        keys = found.get_foreign_keys('site_domain')
        uniques = found.get_unique_constraints('site_domain')
    assert column['nullable'] is False
    assert any(k['constrained_columns'] == ['instance_id'] and k['referred_table'] == 'instance' for k in keys)
    assert any(u['column_names'] == ['instance_id'] for u in uniques)


def _instance_ids(connection, table, ids):
    rows = connection.execute(text(f'SELECT id, instance_id FROM "{table}" WHERE id = ANY(:ids)'), {'ids': list(ids)})
    return dict(rows.all())


@pytest.fixture
def pre_c2(app, db_session):
    """The rows as plan C1 left them: every local row on instance 1, a sister domain, and a peer's rows."""
    hell_instance = make_instance(HELL, software='piefed')
    peer = make_instance('peer.example')
    owner = make_user(hell_instance, 'owner', local=True)    # user 1, make_community's owner
    db.session.add(SiteDomain(domain=HELL, is_default=True))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    hell, onion = domain_for_host(HELL), domain_for_host(ONION)
    owner.site_domain_id = hell.id
    hell_community = make_community('hellish')
    hell_community.site_domain_id = hell.id
    onion_community = make_community('cave', host=ONION)
    onion_community.site_domain_id = onion.id
    onion_user = make_user(hell_instance, 'anna', local=True)
    onion_user.site_domain_id = onion.id
    remote_user = make_user(peer, 'remote')
    db.session.commit()
    hell_post = make_post(hell_community, owner, ap_id='https://test.piefed.local/post/1')
    rows = {
        'user': {'hell': owner.id, 'onion': onion_user.id, 'remote': remote_user.id},
        'community': {'hell': hell_community.id, 'onion': onion_community.id},
        'post': {'hell': hell_post.id,
                 'onion': make_post(onion_community, onion_user, ap_id='http://abc.onion/post/2').id,
                 'remote': make_post(hell_community, remote_user, ap_id='https://peer.example/p/1').id,
                 'onion_by_hell': make_post(onion_community, owner, ap_id='https://test.piefed.local/post/4').id},
        'post_reply': {'hell': make_post_reply(hell_post, owner).id, 'onion': make_post_reply(hell_post, onion_user).id,
                       'remote': make_post_reply(hell_post, remote_user).id},
    }
    for table in ('user', 'community', 'post', 'post_reply'):
        db.session.execute(text(f'UPDATE "{table}" SET instance_id = 1 WHERE id = ANY(:ids) AND instance_id <> :peer'),
                           {'ids': list(rows[table].values()), 'peer': peer.id})
    peer_id, onion_instance = peer.id, onion.instance_id
    db.session.commit()
    db.session.close()
    yield rows, peer_id, onion_instance
    clear_domain_cache()


def _upgrade_then_downgrade(connection, rows, peer_id, *, drop_onion_instance: bool):
    """Downgrade to plan C1's schema, optionally remove the onion's instance row (prod has none), and upgrade."""
    migration = load_migration()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        if drop_onion_instance:
            connection.execute(text("DELETE FROM instance WHERE domain = :d"), {'d': ONION})
        migration.upgrade()
        onion_instance = connection.execute(text('SELECT instance_id FROM site_domain WHERE domain = :d'),
                                            {'d': ONION}).scalar()
        upgraded = {table: _instance_ids(connection, table, ids.values()) for table, ids in rows.items()}
        migration.downgrade()
        downgraded = {table: _instance_ids(connection, table, ids.values()) for table, ids in rows.items()}
        migration.upgrade()
    return onion_instance, upgraded, downgraded


def _assert_moved(rows, peer_id, onion_instance, upgraded, downgraded):
    for table, ids in rows.items():
        for which, row_id in ids.items():
            expected = {'onion': onion_instance, 'remote': peer_id}.get(which, 1)
            assert upgraded[table][row_id] == expected, (table, which)
            assert downgraded[table][row_id] == (peer_id if which == 'remote' else 1), (table, which)


def test_the_upgrade_gives_the_domain_a_new_instance_row_and_moves_its_rows(app, pre_c2):
    rows, peer_id, _ = pre_c2
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            onion_instance, upgraded, downgraded = _upgrade_then_downgrade(connection, rows, peer_id,
                                                                           drop_onion_instance=True)
            made = connection.execute(text('SELECT domain, software, inbox, shared_inbox, failures, dormant, '
                                           'gone_forever, trusted, silenced, popular FROM instance WHERE id = :id'),
                                      {'id': onion_instance}).one()
            assert tuple(made) == (ONION, 'PieFed', None, None, 0, False, False, False, False, True)
            assert connection.execute(text('SELECT instance_id FROM site_domain WHERE is_default')).scalar() == 1
        finally:
            transaction.rollback()
    assert onion_instance not in (None, 1, peer_id)
    _assert_moved(rows, peer_id, onion_instance, upgraded, downgraded)


def test_the_upgrade_reuses_an_instance_row_named_for_the_domain(app, pre_c2):
    rows, peer_id, existing = pre_c2
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("UPDATE instance SET inbox = 'http://abc.onion/inbox', "
                                    "shared_inbox = 'http://abc.onion/inbox', software = 'lemmy' WHERE id = :id"),
                               {'id': existing})
            onion_instance, upgraded, downgraded = _upgrade_then_downgrade(connection, rows, peer_id,
                                                                           drop_onion_instance=False)
            reused = connection.execute(text('SELECT software, inbox, shared_inbox FROM instance WHERE id = :id'),
                                        {'id': existing}).one()
            count = connection.execute(text('SELECT count(*) FROM instance WHERE domain = :d'), {'d': ONION}).scalar()
            hell_row = connection.execute(text('SELECT domain FROM instance WHERE id = 1')).scalar()
        finally:
            transaction.rollback()
    assert onion_instance == existing
    assert (tuple(reused), count, hell_row) == (('PieFed', None, None), 1, HELL)
    _assert_moved(rows, peer_id, onion_instance, upgraded, downgraded)


def test_on_a_database_without_row_one_the_default_gets_a_row_of_its_own(app, db_session):
    """A database migrated before `flask init-db` has made row 1, as the test stack's is."""
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                connection.execute(text("INSERT INTO site_domain (domain, scheme, is_default) "
                                        "VALUES (:d, 'https', true)"), {'d': HELL})
                migration.upgrade()
            made = connection.execute(text('SELECT i.domain, i.software, i.inbox FROM site_domain d '
                                           'JOIN instance i ON i.id = d.instance_id')).one()
        finally:
            transaction.rollback()
    assert tuple(made) == (HELL, 'PieFed', None)
