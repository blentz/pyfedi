import os
import re

import pytest

# Import app before config. config.py does `import app.constants`, which starts
# loading the app package; app/__init__.py in turn does `from config import
# Config` before Config is defined if config.py is the entry point, causing a
# circular ImportError. Every other test module in this repo sidesteps this by
# importing something from app.* first (e.g. `from app.utils import ...`); do
# the same here so `from config import Config` below sees a fully-initialised
# config module.
import app  # noqa: F401
from config import Config

TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL')


def is_disposable_database_url(url):
    """True if `url` names a database whose name marks it as disposable.

    The db_session fixture truncates every table in this database after every
    test, so this check must not be defeatable by a name that merely contains
    "test" as a substring (e.g. "attestation", "contest_archive",
    "latest_snapshot", "posttest_analytics") or by a query string/fragment
    appended after the real name (e.g.
    "...pyfedi_prod?application_name=pytest_test", whose real database name
    is "pyfedi_prod"). The database name is the last "/"-separated path
    segment, with any trailing "?query" or "#fragment" stripped, and it must
    END WITH "_test" — a bare substring match is not enough.
    """
    if not url:
        return False
    segment = url.rsplit('/', 1)[-1]
    segment = re.split(r'[?#]', segment, maxsplit=1)[0]
    return segment.endswith('_test')


class TestConfig(Config):
    """Test configuration. Inherits the real Config so tests exercise real settings."""
    TESTING = True
    WTF_CSRF_ENABLED = False
    MAIL_SUPPRESS_SEND = True
    SQLALCHEMY_DATABASE_URI = TEST_DATABASE_URL
    CACHE_TYPE = 'NullCache'
    SERVER_NAME = 'test.piefed.local'


@pytest.fixture(scope='session')
def app():
    """A Flask app bound to the test database.

    Skips rather than fails when TEST_DATABASE_URL is unset, so a bare checkout
    can still run the pure-function tests. Use ./run_tests.sh for the full suite.
    """
    if not TEST_DATABASE_URL:
        pytest.skip('TEST_DATABASE_URL is not set; run ./run_tests.sh instead')

    # The db_session fixture truncates every table. Refuse to point that at a
    # database whose name does not mark it as disposable.
    if not is_disposable_database_url(TEST_DATABASE_URL):
        pytest.fail(f'TEST_DATABASE_URL must name a disposable test database '
                    f'(name ending in "_test"), got {TEST_DATABASE_URL!r}')

    from app import create_app
    application = create_app(TestConfig)
    with application.app_context():
        yield application


@pytest.fixture
def db_session(app):
    """Give each test a clean database.

    Truncates rather than rolling back a nested transaction: the code under test
    calls db.session.commit() in several places, which a rollback-based fixture
    would have to fight.
    """
    from app import db
    from sqlalchemy import text

    yield db.session

    db.session.rollback()
    table_names = ', '.join(f'"{table.name}"' for table in reversed(db.metadata.sorted_tables))
    db.session.execute(text(f'TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE'))
    db.session.commit()
