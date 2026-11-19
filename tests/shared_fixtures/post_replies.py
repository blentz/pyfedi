"""Fixtures from tests/test_post_replies.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from app.models import Language, Site
from tests.factories import make_community, make_community_member, make_post, make_user
from tests.test_post_replies import as_user, instance


@pytest.fixture
def post_replies_env(app, db_session):
    """`mod` moderates `community`; `author` wrote the reply; `outsider` has no
    standing anywhere -- which is what the two defects are about."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    mod = make_user(local, 'mod', local=True)
    author = make_user(local, 'author', local=True)
    outsider = make_user(local, 'outsider', local=True)
    community = make_community('general')
    # `languages_for_form` skips code 'und', so a form row that has to pass
    # `language_id`'s DataRequired needs a real language as well.
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    make_community_member(mod, community, is_moderator=True)
    make_community_member(author, community)
    post = make_post(community, author, 'https://test.piefed.local/p/1',
                     title='a post')
    db.session.commit()
    return as_user(app, author), community, post, mod, author, outsider
