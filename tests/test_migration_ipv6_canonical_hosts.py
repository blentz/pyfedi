"""Migration 3d52b8a19cf5: Domain, instance-ban and allowlist rows saved under a non-canonical IPv6 spelling are
rewritten to canonical_host's spelling, merging where that spelling already has a row (owner ruling 2026-10-14).

The rows are committed through the ORM, and the revision runs on a second connection inside a transaction that is
rolled back, as tests/test_migration_quote_ap_id.py runs its revision.
"""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app import db
from app.models import AllowedInstances, BannedInstances, DefederationSubscription, Domain, DomainBlock
from app.utils import canonical_host
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / '3d52b8a19cf5_ipv6_canonical_hosts.py'


def load_migration():
    spec = importlib.util.spec_from_file_location('ipv6_canonical_hosts_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_chain():
    migration = load_migration()
    assert (migration.revision, migration.down_revision) == ('3d52b8a19cf5', 'd4a8e2c6f1b9')


@pytest.mark.parametrize('name', ['::1', '0:0::1', '[0:0:0:0:0:0:0:1]', '[::1]', '::FFFF:1.2.3.4', 'example.com',
                                  'example.com:8443', '1.2.3.4', '[::1', '', None])
def test_its_spelling_is_canonical_hosts(name):
    """Inlined so the revision does not change meaning with app/utils.py; it has to agree with it today."""
    expected = canonical_host(name) if name else name
    got = load_migration().canonical(name)
    assert (got if got is not None else name) == expected


def seed():
    """Three spellings of ::1 (one canonical), one non-canonical ::2 alone, two names to leave alone; posts, a reply
    and blocks on the spellings; bans and allows in several spellings."""
    instance = make_instance('remote.example')
    burn = make_user(instance, 'burn', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    canonical = Domain(name='::1', post_count=2)
    spelled = Domain(name='0:0::1', post_count=3, banned=True, post_warning='careful', warning_type=1)
    bracketed = Domain(name='[0:0:0:0:0:0:0:1]', post_count=1, notify_admins=True)
    alone = Domain(name='0:0::2', post_count=4)
    named = Domain(name='example.com', post_count=5)
    ported = Domain(name='example.com:8443')
    subscription = DefederationSubscription(domain='lists.example')
    db.session.add_all([canonical, spelled, bracketed, alone, named, ported, subscription])
    db.session.commit()
    community = make_community('general')
    post = make_post(community, alice, 'https://remote.example/post/1')
    post.domain_id = spelled.id
    reply = make_post_reply(post, alice)
    reply.domain_id = bracketed.id
    other_post = make_post(community, alice, 'https://remote.example/post/2')
    other_post.domain_id = alone.id
    db.session.add_all([DomainBlock(user_id=alice.id, domain_id=canonical.id),
                        DomainBlock(user_id=alice.id, domain_id=spelled.id),
                        DomainBlock(user_id=bob.id, domain_id=bracketed.id),
                        BannedInstances(domain='::1', reason='first'),
                        BannedInstances(domain='[0:0::1]', reason='second'),
                        BannedInstances(domain='0:0::3'),
                        BannedInstances(domain='0::3'),
                        BannedInstances(domain='0:0::1', subscription_id=subscription.id),
                        BannedInstances(domain='evil.example'),
                        AllowedInstances(domain='[::5]'),
                        AllowedInstances(domain='0::5'),
                        AllowedInstances(domain='friend.example')])
    db.session.commit()
    return dict(canonical=canonical.id, spelled=spelled.id, bracketed=bracketed.id, alone=alone.id,
                named=named.id, ported=ported.id, alice=alice.id, bob=bob.id, post=post.id, reply=reply.id,
                other_post=other_post.id, subscription=subscription.id)


def snapshot(connection):
    return {
        'domain': connection.execute(text(
            'SELECT id, name, post_count, banned, notify_mods, notify_admins, post_warning, warning_type '
            'FROM domain ORDER BY id')).all(),
        'post': connection.execute(text('SELECT id, domain_id FROM post ORDER BY id')).all(),
        'post_reply': connection.execute(text('SELECT id, domain_id FROM post_reply ORDER BY id')).all(),
        'domain_block': connection.execute(text(
            'SELECT user_id, domain_id FROM domain_block ORDER BY user_id, domain_id')).all(),
        'banned_instances': connection.execute(text(
            'SELECT domain, reason, subscription_id FROM banned_instances ORDER BY id')).all(),
        'allowed_instances': connection.execute(text('SELECT domain FROM allowed_instances ORDER BY id')).all(),
    }


def run(step):
    """Run `step` of the revision on its own connection; return the tables before and after, then roll back."""
    db.session.close()
    migration = load_migration()
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            before = snapshot(connection)
            with Operations.context(MigrationContext.configure(connection)):
                getattr(migration, step)()
            return before, snapshot(connection)
        finally:
            transaction.rollback()


def test_upgrade_merges_and_renames(app, db_session):
    ids = seed()
    _before, after = run('upgrade')

    # The canonical row survives and absorbs the other two spellings; ::2, with no canonical row, is renamed.
    assert after['domain'] == [
        (ids['canonical'], '::1', 6, True, False, True, 'careful', 1),
        (ids['alone'], '::2', 4, False, False, False, None, 0),
        (ids['named'], 'example.com', 5, False, False, False, None, 0),
        (ids['ported'], 'example.com:8443', 0, False, False, False, None, 0),
    ]
    assert after['post'] == [(ids['post'], ids['canonical']), (ids['other_post'], ids['alone'])]
    assert after['post_reply'] == [(ids['reply'], ids['canonical'])]
    # alice blocked two spellings: one block is left, not a primary key violation.
    assert after['domain_block'] == [(ids['alice'], ids['canonical']), (ids['bob'], ids['canonical'])]
    # The oldest ban of a host is kept; a subscription's ban is its own.
    assert after['banned_instances'] == [('::1', 'first', None), ('::3', None, None),
                                         ('::1', None, ids['subscription']), ('evil.example', None, None)]
    assert after['allowed_instances'] == [('::5',), ('friend.example',)]


def test_a_canonical_row_alone_is_not_touched(app, db_session):
    db.session.add_all([Domain(name='::1', post_count=2), BannedInstances(domain='::1'),
                        AllowedInstances(domain='::1')])
    db.session.commit()
    before, after = run('upgrade')
    assert after == before


def test_downgrade_changes_nothing(app, db_session):
    seed()
    before, after = run('downgrade')
    assert after == before
