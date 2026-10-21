"""Each hosted domain has its own instance row; the default keeps row 1 (plan C2 task 1, spec 2026-10-19 §3.1)."""
import importlib.util
import re
from pathlib import Path

import httpx
import respx

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cachelib import SimpleCache
from sqlalchemy import inspect, text

from app import cache, db
from app.activitypub.util import new_instance_profile_task
from unittest.mock import patch

from app.auth.oauth_util import initialize_new_user
from app.auth.util import create_new_user, create_new_user_from_ldap
from app.activitypub.util import find_microblogging_community
from app.constants import POST_TYPE_ARTICLE, SRC_API
from app.models import (Community, Instance, InstanceInUse, Language, RolePermission, Site, SiteDomain, User,
                        user_role)
from app.shared.community import make_community as make_community_api
from app.shared.post import make_post as make_post_through
from app.shared.reply import make_reply
from app.storefront import (clear_domain_cache, current_instance_id, default_domain, domain_for_host,
                            hosted_instance_ids, instance_id_of)
from tests.factories import bearer, make_community, make_instance, make_post, make_post_reply, make_user
from tests.test_admin_user_administration import _add_payload, _roles
from tests.test_community_lifecycle import able_to_create
from tests.test_per_domain_creation import _create, _form as _register_form, creators, domains   # noqa: F401
from tests.test_shared_community_lifecycle import _api_input, _seed_und_language
from tests.test_shared_post_edit import _api_input as _api_input_post
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
            # Row 1 holds hell.cloud's own users, communities and content: the default takes it all the same.
            assert connection.execute(text('SELECT count(*) FROM "user" WHERE instance_id = 1')).scalar() > 0
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
            made = connection.execute(text('SELECT d.instance_id, i.domain, i.software, i.inbox FROM site_domain d '
                                           'JOIN instance i ON i.id = d.instance_id')).one()
        finally:
            transaction.rollback()
    # The instance table is empty and its sequence fresh (db_session resets it), so the new row is row 1, as init-db's
    # would be.
    assert tuple(made) == (1, HELL, 'PieFed', None)


# --- an instance row a new domain may not take over (fix round 1, I1) ---

def _server_with_content(domain: str) -> Instance:
    """A peer this site federates with: one of its users is stored here."""
    peer = make_instance(domain)
    make_user(peer, 'someone')
    return peer


def test_the_domains_form_refuses_a_server_this_site_federates_with(app, db_session, site):
    client = app.test_client()
    login(client, _admin())
    peer = _server_with_content(ONION)
    peer.inbox = f'http://{ONION}/inbox'
    db.session.commit()
    response = client.post('/admin/domains', data=_form(csrf(app, client)))
    assert response.status_code == 200
    assert 'That domain is already a server this site federates with.' in response.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.query(SiteDomain).filter_by(domain=ONION).first() is None
    assert (peer.software, peer.inbox) == ('mastodon', f'http://{ONION}/inbox')


def test_the_domains_form_refuses_the_main_instance_rows_domain(app, db_session, site):
    """SERVER_NAME changed since init-db: row 1 still carries the old name, which is not the default's to give."""
    make_instance('old.example', software='piefed')
    db.session.add(SiteDomain(domain=HELL, is_default=True))
    db.session.commit()
    client = app.test_client()
    login(client, _admin())
    response = client.post('/admin/domains', data=_form(csrf(app, client), domain='old.example'))
    assert 'That domain is already a server this site federates with.' in response.get_data(as_text=True)
    assert db.session.query(SiteDomain).count() == 1


@pytest.mark.parametrize('content', ['user', 'community', 'post', 'post_reply'])
def test_a_domain_row_never_takes_over_a_server_with_content(app, db_session, content):
    local = make_instance(HELL, software='piefed')
    owner = make_user(local, 'owner', local=True)
    peer = make_instance(ONION)
    if content == 'user':
        make_user(peer, 'someone')
    else:
        community = make_community('cave', host=ONION)
        rows = {'community': community}
        if content != 'community':
            post = make_post(community, owner, ap_id=f'http://{ONION}/post/1')
            rows['post'] = post
            if content == 'post_reply':
                rows['post_reply'] = make_post_reply(post, owner)
        for name, row in rows.items():
            row.instance_id = peer.id if name == content else local.id
        db.session.commit()
    db.session.add(SiteDomain(domain=ONION, federates=False))
    with pytest.raises(InstanceInUse, match=ONION):
        db.session.commit()
    db.session.rollback()


def test_a_domain_row_never_takes_over_row_one(app, db_session):
    make_instance('old.example', software='piefed')
    db.session.add(SiteDomain(domain=HELL, is_default=True))
    db.session.commit()
    db.session.add(SiteDomain(domain='OLD.example'.lower(), federates=False))
    with pytest.raises(InstanceInUse, match='old.example'):
        db.session.commit()
    db.session.rollback()


def test_the_default_takes_row_one_full_of_its_own_content(app, db_session):
    local = make_instance(HELL, software='piefed')
    make_user(local, 'owner', local=True)
    make_community('hellish')
    db.session.add(SiteDomain(domain=HELL, is_default=True))
    db.session.commit()
    assert db.session.query(SiteDomain).one().instance_id == 1


def test_a_domain_row_takes_the_exactly_spelt_row_first(app, db_session):
    """find_instance_id looks a host up by its exact spelling: that row is the one that must become hosted."""
    make_instance(HELL, software='piefed')
    mixed = make_instance(ONION.upper())
    exact = make_instance(ONION)
    db.session.add(SiteDomain(domain=ONION, federates=False))
    db.session.commit()
    assert db.session.query(SiteDomain).filter_by(domain=ONION).one().instance_id == exact.id != mixed.id


def test_the_constraints_have_the_migrations_names(app, db_session):
    """So a database built by `flask init-db` (create_all) downgrades like a migrated one."""
    names = {constraint.name for constraint in SiteDomain.__table__.constraints}
    assert {'fk_site_domain_instance_id', 'uq_site_domain_instance_id'} <= names


def test_the_upgrade_refuses_to_take_over_a_server_with_content(app, pre_c2):
    rows, peer_id, existing = pre_c2
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                connection.execute(text('UPDATE "user" SET instance_id = :i WHERE id = :u'),
                                   {'i': existing, 'u': rows['user']['remote']})
                with pytest.raises(RuntimeError, match=ONION):
                    migration.upgrade()
        finally:
            transaction.rollback()


def test_the_upgrade_takes_the_exactly_spelt_row_first(app, pre_c2):
    rows, peer_id, existing = pre_c2
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                connection.execute(text('UPDATE instance SET domain = :d WHERE id = :i'),
                                   {'d': ONION.upper(), 'i': existing})
                exact = connection.execute(text("INSERT INTO instance (domain) VALUES (:d) RETURNING id"),
                                           {'d': ONION}).scalar()
                migration.upgrade()
            linked = connection.execute(text('SELECT instance_id FROM site_domain WHERE domain = :d'),
                                        {'d': ONION}).scalar()
        finally:
            transaction.rollback()
    assert linked == exact != existing


def test_the_downgrade_leaves_remote_rows_on_a_sister_row_alone(app, pre_c2):
    """It mirrors the upgrade: only local rows go back to row 1."""
    rows, peer_id, existing = pre_c2
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text('UPDATE "user" SET instance_id = :i WHERE id = :u'),
                               {'i': existing, 'u': rows['user']['remote']})
            connection.execute(text('UPDATE post SET instance_id = :i WHERE id = :p'),
                               {'i': existing, 'p': rows['post']['remote']})
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            after = (_instance_ids(connection, 'user', [rows['user']['remote']]),
                     _instance_ids(connection, 'post', [rows['post']['remote']]))
        finally:
            transaction.rollback()
    assert after == ({rows['user']['remote']: existing}, {rows['post']['remote']: existing})


# --- a hosted row never gets an inbox back ---

def test_the_instance_profile_task_leaves_a_hosted_row_alone(app, db_session):
    make_instance(HELL, software='piefed')
    db.session.add(SiteDomain(domain='faraway.test', federates=False))
    db.session.commit()
    clear_domain_cache()
    hosted = db.session.query(SiteDomain).filter_by(domain='faraway.test').one().instance_id
    with respx.mock(assert_all_called=False) as router:
        actor = router.get('https://faraway.test').mock(return_value=httpx.Response(404))
        new_instance_profile_task(hosted)
    db.session.expire_all()
    assert not actor.called
    assert db.session.get(Instance, hosted).inbox is None


def test_the_interop_harness_probes_a_table_only_init_db_fills():
    """interop/run.sh runs `flask init-db` when its probe table is empty. `flask db upgrade` runs first and some
    migrations seed rows (from c8f4d2a6b9e1 an instance row), so the probe must be a table no migration writes:
    `site`, which only init-db's `Site(...)` fills."""
    script = (Path(__file__).resolve().parent.parent / 'interop' / 'run.sh').read_text()
    probe = re.search(r"select count\(\*\) from (\w+)'", script)
    assert probe is not None and probe.group(1) == 'site'
    versions = (Path(__file__).resolve().parent.parent / 'migrations' / 'versions').glob('*.py')
    writes = re.compile(r'insert\s+into\s+"?site"?[\s(]|(?<!\w)table\(\s*[\'"]site[\'"]', re.IGNORECASE)
    assert [path.name for path in versions if writes.search(path.read_text())] == []


# --- a new row takes its domain's instance (plan C2 task 2) ---

def _admin_adding(app):
    """A client logged in as an administrator who may add users, and its CSRF token."""
    make_user(db.session.get(Instance, 1), 'founder', local=True)
    admin = make_user(db.session.get(Instance, 1), 'useradmin', local=True)
    admin.verified = True
    db.session.commit()
    roles = _roles()
    db.session.add(RolePermission(role_id=roles['Admin'].id, permission='administer all users'))
    db.session.add(RolePermission(role_id=roles['Admin'].id, permission='change user roles'))
    db.session.execute(user_role.insert().values(user_id=admin.id, role_id=roles['Admin'].id))
    db.session.commit()
    client = app.test_client()
    login(client, admin)
    return client, csrf(app, client)


def _admin_add_user(app, client, token, name, domain):
    with patch('app.admin.routes.render_template', return_value='rendered'):
        client.post('/admin/users/add', data=_add_payload(token, user_name=name, email=f'{name}@example.com',
                                             site_domain=str(domain.id)),
                    content_type='multipart/form-data')
    return User.query.filter_by(user_name=name).one()


def test_an_admin_added_user_takes_its_domains_instance(app, db_session, site, domains):
    default, onion = domains
    client, token = _admin_adding(app)
    assert _admin_add_user(app, client, token, 'torfan', onion).instance_id == onion.instance_id
    assert _admin_add_user(app, client, token, 'clearfan', default).instance_id == 1
    assert onion.instance_id != 1


def test_registering_on_the_onion_storefront_takes_its_instance(app, db_session, site, domains):
    default, onion = domains
    with app.test_request_context(base_url='http://abc.onion'):
        app.preprocess_request()
        on_onion = create_new_user(_register_form('wakko'), '127.0.0.1', 'GB', 'token')
    with app.test_request_context(base_url=f'https://{default.domain}'):
        app.preprocess_request()
        on_default = create_new_user(_register_form('yakko'), '127.0.0.1', 'GB', 'token')
    assert (on_onion.instance_id, on_default.instance_id) == (onion.instance_id, 1)


def test_ldap_and_oauth_users_are_on_the_default_instance(app, db_session, site, domains):
    default, onion = domains
    with app.test_request_context(base_url='http://abc.onion'):
        app.preprocess_request()
        ldap_user = create_new_user_from_ldap('ldapper', 'ldapper@example.com', 'password123', '127.0.0.1')
        with patch('app.auth.oauth_util.finalize_user_setup'):
            oauth_user = initialize_new_user('newcomer@example.com', 'New', 'google_oauth_id', {'id': 'r1'},
                                             '127.0.0.1', 'GB')
    assert (ldap_user.instance_id, oauth_user.instance_id) == (1, 1)


def test_an_onion_users_community_post_and_reply_carry_the_onion_instance(app, db_session, site, domains):
    default, onion = domains
    client, token = _admin_adding(app)
    torfan = _admin_add_user(app, client, token, 'torfan', onion)
    able_to_create(torfan)
    torfan.private_key = 'test-key-not-a-real-pem'
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.get(Site, 1).community_creation_admin_only = False
    db.session.commit()
    community = _create(app, torfan, 'abc.onion', url='hidden')
    assert community.instance_id == onion.instance_id

    user_id, post = make_post_through(_api_input_post(), community, POST_TYPE_ARTICLE, SRC_API, auth=bearer(torfan))
    assert post.instance_id == onion.instance_id
    _, reply = make_reply({'body': 'hi', 'notify_author': False, 'language_id': None}, post, None, SRC_API,
                          auth=bearer(torfan))
    assert reply.instance_id == onion.instance_id


def test_a_clearnet_community_belongs_to_the_default_instance(app, db_session, site, creators, domains):
    default, onion = domains
    founder, other = creators
    assert _create(app, founder, 'abc.onion', url='clearnet').instance_id == 1


def test_the_api_makes_an_onion_creators_community_on_the_onion_instance(app, db_session, site, domains):
    default, onion = domains
    make_user(db.session.get(Instance, 1), 'founder', local=True)
    _seed_und_language()
    user = make_user(db.session.get(Instance, 1), 'onionite', local=True, with_keys=True)
    user.site_domain_id = onion.id
    db.session.commit()
    make_community_api(_api_input(name='hidden'), SRC_API, bearer(user))
    assert Community.query.filter_by(name='hidden').one().instance_id == onion.instance_id


def test_the_microblogs_community_stays_on_the_default_instance(app, db_session, site, domains):
    make_user(db.session.get(Instance, 1), 'founder', local=True)
    assert find_microblogging_community().instance_id == 1
