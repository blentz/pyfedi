"""PieFed's state in the harness stack, taken once the session has seeded it and put back around every test (owner
ruling H1, 2026-11-12: each test starts from the same clean state, and its teardown removes whatever it changed).

PieFed's state is its database, its Redis (the cache and Celery's queues) and its media and tmp folders. All three are
reached from the apps-runner: Postgres and Redis over the stack's network, the two folders as the volumes PieFed's
containers mount, mounted here too (compose.interop.yaml). Nothing is restarted, so restoring works in place:

* the database: every table of the public schema is copied into the schema SNAPSHOT, with every sequence's value. A
  restore empties the public tables and copies the snapshot back in one transaction, with triggers (and so foreign
  keys) off while it does (session_replication_role), then sets the sequences back. Connections PieFed holds stay open.
* Redis: every key of every database, by DUMP with its time to live, kept in memory here; a restore empties Redis and
  RESTOREs them.
* the folders: copied here with rsync, and copied back with --delete.

A restore first waits until PieFed's Celery is idle (no queued and no unacknowledged task), so no task writes after it.
`fingerprint` is what a restore is checked by: each table's row count and an md5 of its rows in order.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

import psycopg
import redis

DSN = 'host=piefed-db user=piefed password=piefed dbname=piefed'
REDIS_URL = 'redis://piefed-redis:6379'
SNAPSHOT = 'harness_snapshot'
FOLDERS = {'/piefed/media/': '/harness-snapshot/media/', '/piefed/tmp/': '/harness-snapshot/tmp/'}
CELERY_QUEUES = ('celery', 'background', 'onion_media')
IDLE_TIMEOUT = 300


def _tables(cursor) -> list[str]:
    cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    return [row[0] for row in cursor.fetchall()]


def _sequences(cursor) -> list[str]:
    cursor.execute("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename")
    return [row[0] for row in cursor.fetchall()]


def _redis_databases(client: redis.Redis) -> list[int]:
    return sorted(int(name[2:]) for name in client.info('keyspace'))


@dataclass
class Snapshot:
    tables: list[str] = field(default_factory=list)
    redis_keys: dict[int, list[tuple[bytes, bytes, int]]] = field(default_factory=dict)   # db -> (key, dump, pttl)
    fingerprint: dict[str, tuple[int, str]] = field(default_factory=dict)


def fingerprint() -> dict[str, tuple[int, str]]:
    """Each public table's row count and an md5 of its rows, ordered by their text."""
    with psycopg.connect(DSN, autocommit=True) as connection, connection.cursor() as cursor:
        found = {}
        for table in _tables(cursor):
            cursor.execute(f'SELECT count(*), coalesce(md5(string_agg(t::text, \'|\' ORDER BY t::text)), \'\') '
                           f'FROM public."{table}" t')
            found[table] = tuple(cursor.fetchone())
        return found


def wait_until_idle(timeout: float = IDLE_TIMEOUT) -> None:
    """Until no Celery task of PieFed's is queued or running, twice in a row a second apart: kombu keeps a sent task
    in its queue's list and a delivered, unacknowledged one in `unacked`."""
    client = redis.Redis.from_url(f'{REDIS_URL}/1')
    deadline, quiet = time.monotonic() + timeout, 0
    while quiet < 2:
        busy = sum(client.llen(queue) for queue in CELERY_QUEUES) + client.hlen('unacked')
        quiet = quiet + 1 if busy == 0 else 0
        if time.monotonic() > deadline:
            raise AssertionError(f'stack_state: PieFed\'s Celery still has {busy} task(s) after {timeout}s')
        time.sleep(1)


def take() -> Snapshot:
    """The state as it is now, once the session has seeded it."""
    wait_until_idle()
    snapshot = Snapshot()
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(f'DROP SCHEMA IF EXISTS {SNAPSHOT} CASCADE')
        cursor.execute(f'CREATE SCHEMA {SNAPSHOT}')
        snapshot.tables = _tables(cursor)
        for table in snapshot.tables:
            cursor.execute(f'CREATE TABLE {SNAPSHOT}."{table}" AS TABLE public."{table}"')
        cursor.execute(f'CREATE TABLE {SNAPSHOT}._sequences (name text PRIMARY KEY, value bigint, called boolean)')
        for sequence in _sequences(cursor):
            cursor.execute(f'INSERT INTO {SNAPSHOT}._sequences SELECT %s, last_value, is_called FROM public."{sequence}"',
                           (sequence,))
    client = redis.Redis.from_url(REDIS_URL)
    for number in _redis_databases(client):
        db = redis.Redis.from_url(f'{REDIS_URL}/{number}')
        snapshot.redis_keys[number] = [(key, db.dump(key), max(db.pttl(key), 0)) for key in db.scan_iter(count=1000)]
    for live, kept in FOLDERS.items():
        subprocess.run(['rsync', '-a', '--delete', '--mkpath', live, kept], check=True)
    snapshot.fingerprint = fingerprint()
    return snapshot


def _columns(cursor, table: str) -> tuple[list[str], bool]:
    """The columns a restore writes of `table` (a generated column is computed again, not written) and whether one
    is an identity GENERATED ALWAYS, whose values are written back with OVERRIDING SYSTEM VALUE."""
    cursor.execute("SELECT column_name, is_generated, identity_generation FROM information_schema.columns "
                   "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position", (table,))
    rows = cursor.fetchall()
    return ([name for name, generated, _identity in rows if generated != 'ALWAYS'],
            any(identity == 'ALWAYS' for _name, _generated, identity in rows))


def restore(snapshot: Snapshot) -> None:
    """Put the state back as `take` found it, and check it is."""
    wait_until_idle()
    with psycopg.connect(DSN) as connection, connection.cursor() as cursor:
        # a session idle inside a transaction would hold the locks TRUNCATE needs: none is ours to wait for
        cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'piefed' "
                       "AND state LIKE 'idle in transaction%%' AND pid <> pg_backend_pid()")
        cursor.execute("SET lock_timeout = '60s'")
        cursor.execute('SET session_replication_role = replica')
        current = _tables(cursor)
        extra = sorted(set(current) - set(snapshot.tables))
        assert not extra, f'stack_state: tables made since the snapshot: {extra}'
        cursor.execute('TRUNCATE ' + ', '.join(f'public."{table}"' for table in current))
        for table in snapshot.tables:
            names, identity = _columns(cursor, table)
            columns = ', '.join(f'"{name}"' for name in names)
            overriding = ' OVERRIDING SYSTEM VALUE' if identity else ''
            cursor.execute(f'INSERT INTO public."{table}" ({columns}){overriding} '
                           f'SELECT {columns} FROM {SNAPSHOT}."{table}"')
        cursor.execute(f'SELECT name, value, called FROM {SNAPSHOT}._sequences')
        for name, value, called in cursor.fetchall():
            cursor.execute('SELECT setval(%s, %s, %s)', (f'public."{name}"', value, called))
        cursor.execute('SET session_replication_role = DEFAULT')
    client = redis.Redis.from_url(REDIS_URL)
    client.flushall()
    for number, keys in snapshot.redis_keys.items():
        db = redis.Redis.from_url(f'{REDIS_URL}/{number}')
        for key, dumped, ttl in keys:
            db.restore(key, ttl, dumped, replace=True)
    for live, kept in FOLDERS.items():
        subprocess.run(['rsync', '-a', '--delete', kept, live], check=True)
    after = fingerprint()
    changed = sorted(table for table in set(after) | set(snapshot.fingerprint)
                     if after.get(table) != snapshot.fingerprint.get(table))
    assert not changed, f'stack_state: after the restore these tables differ from the snapshot: {changed}'
