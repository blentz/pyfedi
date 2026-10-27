"""The notification stamp, `notification.local_only` (follow-up F6, round 6, re-review 4 I-A).

The rule lives in Python (`app.local_only.notification_stamps`), which the `after_flush` hook calls for every
notification a flush writes. Migration c6e2a8f4b1d9 keeps a frozen SQL copy of it only for its backfill, and drops
that copy once the backfill is done, so nothing in the database depends on the `notification` table: `flask init-db`'s
drop_all and create_all work on a migrated database, and a database built by create_all stamps as a migrated one does.
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

import app.cli as cli
from app import db
import app.local_only as local_only
from app.models import Conversation, Instance, Notification, SiteDomain, User
from app.utils import patch_db_session
from tests.factories import make_post_reply
from tests.test_instance_visible_here import scene  # noqa: F401 -- a fixture
from tests.test_local_only_actors_hidden import ONION_ART, ONION_BOB, world  # noqa: F401 -- world is a fixture
from tests.test_onion_hidden_on_clearnet import MIGRATION, alpha, onion  # noqa: F401 -- alpha, onion are fixtures
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401 -- fixtures
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401 -- two_domains is a fixture

pytestmark = pytest.mark.usefixtures('site')

HELPERS = ('notification_names_local_only', 'notification_local_only_user', 'notification_local_only_community',
           'notification_target_id')


def test_nothing_in_the_migrated_database_depends_on_the_notification_table():
    """I-A: a function that takes the `notification` row type makes Postgres refuse `DROP TABLE notification`, and
    so `flask init-db`. The migration drops the helpers its backfill used."""
    dependants = db.session.execute(text(
        "SELECT p.oid::regprocedure::text FROM pg_depend d JOIN pg_proc p ON p.oid = d.objid "
        "JOIN pg_type t ON t.oid = d.refobjid WHERE d.classid = 'pg_proc'::regclass AND t.typname = 'notification'"
    )).scalars().all()
    left = db.session.execute(text('SELECT proname FROM pg_proc WHERE proname = ANY(:names)'),
                              {'names': list(HELPERS)}).scalars().all()
    assert (dependants, left) == ([], [])


def test_init_dbs_rebuild_works_on_a_migrated_database_and_stamps_as_it_does(app):
    """I-A: init-db's drop_all and create_all, on the real (migrated) test database, in a transaction that is rolled
    back. The rebuilt schema has the partial index the counts read, and with a local-only domain a new notification
    is stamped by the hook, with no database function."""
    db.session.commit()
    db.session.close()   # nothing of this session may hold a lock the drop waits for
    with db.engine.connect() as connection:
        transaction = connection.begin()
        try:
            cli.rebuild_schema(connection)
            assert connection.execute(text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_notification_user_local_only'")).scalar() \
                == 'CREATE INDEX ix_notification_user_local_only ON public.notification USING btree (user_id) ' \
                   'WHERE local_only'
            session = Session(bind=connection, join_transaction_mode='create_savepoint')
            with patch_db_session(session):   # the storefront reads its domains through db.session
                first, hidden = Instance(domain=FIRST, software='piefed'), Instance(domain=ONION, software='piefed')
                session.add_all([first, hidden])
                session.flush()
                session.add_all([SiteDomain(domain=FIRST, scheme='https', is_default=True, instance_id=first.id),
                                 SiteDomain(domain=ONION, scheme='http', federates=False, instance_id=hidden.id,
                                            private_key='a key', public_key='a key')])
                session.flush()
                onion_id = session.execute(text('SELECT id FROM site_domain WHERE domain = :d'),
                                           {'d': ONION}).scalar()
                bob = User(user_name='bob', email='bob@example.com', instance_id=hidden.id, site_domain_id=onion_id)
                reader = User(user_name='reader', email='reader@example.com', instance_id=first.id)
                session.add_all([bob, reader])
                session.flush()
                about_bob = Notification(user_id=reader.id, title='t', url=None, author_id=bob.id, notif_type=4)
                clean = Notification(user_id=reader.id, title='t', url='/admin/reports', notif_type=4)
                session.add_all([about_bob, clean])
                session.flush()
                assert [row.local_only for row in (about_bob, clean)] == [True, False]
                session.expire_all()
                assert session.execute(text('SELECT local_only FROM notification ORDER BY id')).scalars().all() \
                    == [True, False]
            session.close()
        finally:
            transaction.rollback()


def shapes(world, with_onion, with_first, onion_reply, first_reply):
    """Every kind of link, targets and author a notification is written with (the ~60 `Notification(` sites) and
    every link `notification_href` follows, about the onion's rows and the first domain's, and the odd spellings."""
    post, first_post, in_first = world.post.id, world.first_post.id, world.in_first.id
    bob, first_bob = world.onion_bob.id, world.first_bob.id
    art, first_art = world.onion_art.id, world.first_art.id
    links = [None, '', '/', '/admin/reports', '/notifications', '/user/follow_requests', '/admin/reports?x=1#y',
             f'/post/{post}', f'/post/{first_post}', f'/post/{in_first}', '/post/2147483647', '/post/9999999999999',
             f'/post/{first_post}/comment/{onion_reply}#comment_{onion_reply}',
             f'/post/{first_post}/comment/{first_reply}#comment_{first_reply}',
             f'/post/{post}/comment/{first_reply}', f'/comment/{onion_reply}', f'/comment/{first_reply}#comment_1',
             f'https://{FIRST}/post/{post}', f'https://{FIRST}/post/{first_post}',
             f'https://{FIRST}/comment/{onion_reply}',
             f'https://{FIRST}:443/post/{first_post}', f'HTTPS://{FIRST.upper()}/post/{first_post}',
             f'https://{FIRST}/comment/{first_reply}', f'/chat/{with_onion}#message_5', f'/chat/{with_first}#message_5',
             f'/chat/ban_from_mod/{bob}/{first_art}', f'/chat/ban_from_mod/{first_bob}/{art}',
             f'/chat/ban_from_mod/{first_bob}/{first_art}', f'/chat/unban_from_mod/{first_bob}/{first_art}',
             f'/user/{bob}', f'/user/{first_bob}', f'/community/{art}', f'/community/{first_art}',
             f'/c/{ONION_ART}', '/c/art', f'/u/{ONION_BOB}', '/u/bob', '/u/quietotter', '/c/art/p/12/a-slug',
             '/admin/approve_registrations?user=3', f'/admin/community/{first_art}/move/{art}',
             f'http://{ONION}/post/in-first', f'http://{ONION.upper()}/u/bob', f'http://{ONION}/post/{first_post}',
             f'http://{ONION}/admin/reports', f'https://elsewhere.example/post/{post}',
             f'//{ONION}/post/1', f'/post/{first_post}\n', f'/post/{post}\n', f'/post/{first_post}/', '/post/0012']
    rows = [SimpleNamespace(url=link, targets={}, author_id=None) for link in links]
    targets = [{key: value} for key in ('post_id', 'suspect_post_id') for value in (post, first_post, in_first)]
    targets += [{key: value} for key in ('comment_id', 'suspect_comment_id') for value in (onion_reply, first_reply)]
    targets += [{key: value} for key in ('author_id', 'suspect_user_id') for value in (bob, first_bob)]
    targets += [{'community_id': art}, {'community_id': first_art}, {'post_id': str(post)}, {'post_id': True},
                {'post_id': float(post)}, {'post_id': [post]}, {'post': {'post_id': post}}, {'author_id': -1}]
    rows += [SimpleNamespace(url=f'/post/{first_post}', targets=json.loads(json.dumps(value)), author_id=None)
             for value in targets]
    rows += [SimpleNamespace(url=None, targets=None, author_id=author) for author in (bob, first_bob, 999999)]
    rows += [SimpleNamespace(url=f'/post/{first_post}', targets=[post], author_id=None)]
    return rows


def test_the_python_stamp_covers_every_notification_the_migrations_backfill_stamps(app, alpha):
    """The hook's rule (Python) and the backfill's frozen copy (SQL) must agree, or rows written before the
    migration and after it are stamped by different rules. Stamping more than the backfill is safe; stamping less
    is a leak: a notification that names an onion row but is unstamped is counted, and listed, on the clearnet."""
    alpha.onion_bob.alt_user_name = 'quietotter'
    for table, start in (('conversation', 5000), ('post_reply', 7000)):   # ids no row of another kind has
        db.session.execute(text(f"SELECT setval('{table}_id_seq', {start})"))
    with_onion, with_first = Conversation(user_id=alpha.first_bob.id), Conversation(user_id=alpha.first_bob.id)
    with_onion.members.extend([alpha.first_bob, alpha.onion_bob])
    with_first.members.extend([alpha.first_bob, alpha.admin])
    first_reply = make_post_reply(alpha.first_post, alpha.first_bob, 'FIRSTREPLY')
    db.session.add_all([with_onion, with_first])
    db.session.commit()
    rows = shapes(alpha, with_onion.id, with_first.id, alpha.reply.id, first_reply.id)
    python = local_only.notification_stamps(rows, db.session)
    connection = db.session.connection()
    connection.exec_driver_sql(MIGRATION.HELPERS)
    try:
        frozen = [connection.execute(text(
            'SELECT notification_names_local_only(:url, CAST(:targets AS json), :author_id, :host)'),
            {'url': row.url, 'targets': json.dumps(row.targets), 'author_id': row.author_id, 'host': FIRST}).scalar()
            for row in rows]
    finally:
        connection.exec_driver_sql(MIGRATION.DROP_HELPERS)
    leaks = [(row.url, row.targets, row.author_id) for row, by_python, by_sql in zip(rows, python, frozen)
             if by_sql and not by_python]
    assert leaks == []
    clean = [(row.url, row.targets, row.author_id) for row, by_python in zip(rows, python) if not by_python]
    assert len(clean) > 20 and frozen.count(True) > 30, (clean, frozen)   # the shapes judge both ways
    db.session.rollback()
