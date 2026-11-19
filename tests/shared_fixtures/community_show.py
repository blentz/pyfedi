"""Fixtures from tests/test_community_show.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import Language
from tests.factories import make_community, make_community_member, make_user
from tests.test_community_show import instance, public_instance


@pytest.fixture
def community_show_env(app, db_session):
    """A local community with one member, on a public instance."""
    public_instance()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    author = make_user(local, 'author', local=True)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    return app.test_client(), community, author
