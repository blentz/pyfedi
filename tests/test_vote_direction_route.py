"""The web's vote routes take the direction as a path segment (fuzz-routes finding, 2026-11-13): POST
/post/<id>/comment/<x>/delete reached post_vote with the direction 'comment', and Post.vote raised ValueError, a 500
to any signed-in person. A direction none of upvote, downvote or reversal names no vote: a 404."""
import pytest

from app import db
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')


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


class Voting:
    """The signed-in voter's client, every POST carrying the CSRF token login_required checks."""

    def __init__(self, client, token):
        self.client, self.token = client, token

    def post(self, path):
        return self.client.post(path, data={'csrf_token': self.token})


@pytest.mark.parametrize('direction', ['comment', 'sideways'], ids=['comment', 'sideways'])   # a blank one: the router's 308
def test_a_post_vote_in_no_direction_is_not_found(voter, direction):
    client, post, _reply = voter
    assert client.post(f'/post/{post.id}/{direction}/public').status_code == 404
    assert client.post(f'/post/{post.id}/{direction}/public/x').status_code == 404


def test_a_comment_vote_in_no_direction_is_not_found(voter):
    client, _post, reply = voter
    assert client.post(f'/comment/{reply.id}/sideways/public').status_code == 404


def test_a_vote_in_a_direction_still_counts(voter):
    client, post, _reply = voter
    assert client.post(f'/post/{post.id}/upvote/public').status_code == 200
