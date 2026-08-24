"""Unit tests for the TEST_DATABASE_URL disposability guard.

Plain function tests: no database, no app fixture. tests/conftest.py's
db_session fixture truncates every table in TEST_DATABASE_URL after every
test, so the guard must not be defeatable by a database name that merely
contains "test" as a substring, or by a query string/fragment appended
after the real database name.
"""

from tests.conftest import is_disposable_database_url


def test_accepts_name_ending_in__test():
    url = 'postgresql+psycopg2://pyfedi:pyfedi@test-db:5432/pyfedi_test'
    assert is_disposable_database_url(url) is True


def test_rejects_attestation():
    """"attestation" contains "test" as a substring but does not end with "_test"."""
    url = 'postgresql+psycopg2://pyfedi:pyfedi@db:5432/attestation'
    assert is_disposable_database_url(url) is False


def test_rejects_contest_archive():
    url = 'postgresql+psycopg2://pyfedi:pyfedi@db:5432/contest_archive'
    assert is_disposable_database_url(url) is False


def test_rejects_latest_snapshot():
    url = 'postgresql+psycopg2://pyfedi:pyfedi@db:5432/latest_snapshot'
    assert is_disposable_database_url(url) is False


def test_rejects_posttest_analytics():
    url = 'postgresql+psycopg2://pyfedi:pyfedi@db:5432/posttest_analytics'
    assert is_disposable_database_url(url) is False


def test_rejects_query_string_containing_test():
    """A query string containing "test" must not defeat the guard: the real
    database name here is "pyfedi_prod"."""
    url = 'postgresql+psycopg2://pyfedi:pyfedi@db:5432/pyfedi_prod?application_name=pytest_test'
    assert is_disposable_database_url(url) is False


def test_rejects_name_with_no_test_marker():
    url = 'postgresql+psycopg2://pyfedi:pyfedi@db:5432/pyfedi'
    assert is_disposable_database_url(url) is False


def test_rejects_empty_string():
    assert is_disposable_database_url('') is False


def test_rejects_none():
    assert is_disposable_database_url(None) is False
