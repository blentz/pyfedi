"""The URL campaign checks that reach the domain registry never read the database.

tests/fuzz/campaign.py build_test_app builds an app with no database, so a check that touches one is a harness bug.
is_safe_redirect_target and process_webfinger_request ask the registry (app.storefront.domain_rows, a SiteDomain
read); the checks stub it, so a campaign on a fresh, unmigrated database still runs."""
import pytest

from app import db, storefront
from tests.fuzz.targets.urls import check_redirect_targets, check_webfinger_resource


@pytest.fixture
def no_database(app, monkeypatch):
    storefront.clear_domain_cache()

    def refuse(*args, **kwargs):
        raise AssertionError('the check read the database')

    monkeypatch.setattr(db.session, 'query', refuse)
    yield
    storefront.clear_domain_cache()


def test_redirect_targets_never_reads_the_database(app, no_database):
    # A host that is not SERVER_NAME is the one the registry is asked about.
    check_redirect_targets(b'\x00https://other.example/x')


def test_webfinger_resource_never_reads_the_database(no_database):
    check_webfinger_resource(b'acct:x@other.example')


def test_the_registry_is_restored_afterwards(app):
    before = storefront.domain_rows
    check_redirect_targets(b'\x00/x')
    assert storefront.domain_rows is before


def test_a_trailing_dot_on_this_servers_host_is_this_server(app):
    check_redirect_targets(b'\x80' + f'http://{app.config["SERVER_NAME"]}.'.encode())
