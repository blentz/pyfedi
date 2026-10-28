"""Block until the database schema matches the code's migrations.

The autodeploy restarts every unit at once and only the web container runs `flask db upgrade`. A Celery worker or
the notifs server that starts first would run on a half-migrated schema. This guard waits for the migration to
finish. It does not call create_app (slow, and it has side effects): it reads DATABASE_URL like config.py and
compares alembic's own view of the database with the migrations directory.

Exit 0 when the schema is current, 1 on timeout (systemd then restarts the unit)."""
import os
import sys
import time

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

MIGRATIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'migrations')


def schema_is_current(connection, script_location):
    """True only when the revisions in alembic_version equal the heads of the code's migrations."""
    database = set(MigrationContext.configure(connection).get_current_heads())
    return database == set(ScriptDirectory(script_location).get_heads())


def wait_for_schema(connect, check, timeout, interval, sleep=time.sleep, clock=time.monotonic):
    """Poll until check(connection) is true; an unreachable database counts as "not yet". False on timeout."""
    deadline = clock() + timeout
    while True:
        try:
            connection = connect()
            try:
                if check(connection):
                    return True
            finally:
                connection.close()
        except OperationalError:
            pass
        if clock() >= deadline:
            return False
        sleep(interval)


def main():
    timeout = int(os.environ.get('SCHEMA_WAIT_TIMEOUT', '600'))
    engine = create_engine(os.environ['DATABASE_URL'])
    print(f'Waiting for the database schema to match the migrations (up to {timeout}s)...', flush=True)
    current = wait_for_schema(engine.connect, lambda connection: schema_is_current(connection, MIGRATIONS),
                              timeout=timeout, interval=2)
    if not current:
        print(f'The database schema was not current after {timeout}s.', file=sys.stderr, flush=True)
        return 1
    print('The database schema is current.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
