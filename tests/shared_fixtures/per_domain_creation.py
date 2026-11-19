"""Fixtures from tests/test_per_domain_creation.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import Instance, Language, Site, SiteDomain
from app.storefront import clear_domain_cache
from tests.factories import make_instance, make_user
from tests.test_community_lifecycle import able_to_create


@pytest.fixture
def creators(app, db_session, per_domain_creation_domains):
    default, onion = per_domain_creation_domains
    instance = db.session.get(Instance, 1)
    founder = make_user(instance, 'founder', local=True)
    able_to_create(founder)
    founder.site_domain_id = default.id
    other = make_user(instance, 'onionite', local=True)
    able_to_create(other)
    other.site_domain_id = onion.id
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.get(Site, 1).community_creation_admin_only = False
    db.session.commit()
    return founder, other


@pytest.fixture
def per_domain_creation_domains(db_session):
    """The default clearnet row and an onion row, over the local instance row (id 1)."""
    make_instance('test.piefed.local')
    default = db.session.query(SiteDomain).filter_by(is_default=True).first()
    if default is None:
        default = SiteDomain(domain='default.example', is_default=True)
        db.session.add(default)
    onion = SiteDomain(domain='abc.onion', scheme='http', federates=False)
    db.session.add(onion)
    db.session.commit()
    clear_domain_cache()
    yield default, onion
    clear_domain_cache()
