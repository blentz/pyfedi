"""Fixtures from tests/test_community_add_post.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import Language
from tests.factories import make_community, make_community_member, make_user
from tests.test_community_add_post import able_to_post, csrf, instance, login


@pytest.fixture
def community_add_post_poster(app, db_session):
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    author = make_user(local, 'author', local=True)
    able_to_post(author)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    english = Language(code='en', name='English')
    db.session.add(english)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()

    client = app.test_client()
    login(client, author)
    return client, csrf(app, client), community, author, english
