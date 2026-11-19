"""Fixtures from tests/test_vote_direction_route.py that other test modules request (registered in tests/conftest.py)."""
import pytest

from app import db
from tests.factories import (
    make_community,
    make_instance,
    make_post,
    make_post_reply,
    make_user,
)
from tests.test_admin_site_profile import csrf, login
from tests.test_vote_direction_route import Voting


@pytest.fixture
def voter(app, db_session):
    local = make_instance('test.piefed.local', software='piefed')
    author = make_user(local, 'voteauthor', local=True)
    user = make_user(local, 'votevoter', local=True, with_keys=True)   # an approved account (approval_required)
    user.verified = True
    post = make_post(make_community('votes'), author, ap_id='https://test.piefed.local/post/v1')
    reply = make_post_reply(post, author)
    db.session.commit()
    client = app.test_client()
    login(client, user)
    token = csrf(app, client)
    return Voting(client, token), post, reply
