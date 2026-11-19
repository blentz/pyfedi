"""Fixtures from tests/test_api_post_actions.py that other test modules request (registered in tests/conftest.py)."""
import pytest
from flask import g

from app import db
from app.models import Language, Site
from tests.factories import a_keypair, make_community, make_community_member, make_post


@pytest.fixture
def api_post_actions_env(app, api_baseline):
    """`author` wrote `post` in the local community `probeland`, which
    `moderator` moderates and `stranger` has nothing to do with.

    Every account that writes here carries keys: `can_create_post` refuses a
    local account with no `private_key`, which surfaces as "You are not
    permitted to make posts in this community".
    """
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    for code, name in [('en', 'English'), ('und', 'Undetermined'),
                       ('fr', 'French')]:
        db.session.add(Language(code=code, name=name))
    db.session.commit()
    db.session.get(Site, 1).language_id = Language.query.filter_by(
        code='en').one().id
    community = make_community('probeland')
    author = api_baseline.user2
    moderator = api_baseline.user4
    stranger = api_baseline.user3
    for who in (author, moderator, stranger):
        who.private_key, who.public_key = a_keypair()
    db.session.commit()
    make_community_member(author, community)
    make_community_member(moderator, community, is_moderator=True)
    post = make_post(community, author, ap_id='https://test.piefed.local/p/1')
    post.body = 'a body'
    post.body_html = '<p>a body</p>'
    db.session.commit()
    return SimpleNamespace(community=community, author=author, post=post,
                           moderator=moderator, stranger=stranger,
                           admin=api_baseline.user1, baseline=api_baseline)
