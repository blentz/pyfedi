"""Fixtures from tests/test_storefront_api.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import cache, db
from app.models import SiteDomain
from app.storefront import clear_domain_cache
from tests.test_storefront_api import CANONICAL, ONION


@pytest.fixture
def storefront_api_onion(db_session):
    if SiteDomain.query.filter_by(domain=CANONICAL).first() is None:   # the canonical host stays served
        db.session.add(SiteDomain(domain=CANONICAL, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=ONION, scheme='http', federates=False))
    db.session.commit()
    clear_domain_cache()
    cache.clear()
    yield ONION
    clear_domain_cache()
