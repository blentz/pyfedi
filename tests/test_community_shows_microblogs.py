"""A post in a community is visible when you browse that community.

`Post.private` is the microblog marker (see
tests/test_post_private_is_only_the_microblog_marker.py). Filtering on it in the
community listing and the community RSS feed hid microblog posts from the one
place they belong -- `/c/microblogs@piefed.social` showed nothing while the same
posts turned up in the subscribed feed.
"""

import pytest

from tests.factories import make_community, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def community_with_a_microblog(db_session):
    """A community holding one microblog post and one ordinary post.

    The viewer is a logged-in local user because Site.private_instance defaults to
    True (app/models.py:3855), so an anonymous GET of either route is bounced to
    /auth/login before the post query runs.
    """
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('m.example'), 'noteauthor')
    viewer = make_user(None, 'browsingviewer', local=True)
    community = make_community('notes')
    microblog = make_post(community, author, 'https://m.example/notes/1', microblog=True)
    ordinary = make_post(community, author, 'https://m.example/notes/2', title='an ordinary post')
    return viewer, community, microblog, ordinary


def logged_in_client(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


def test_microblog_post_is_listed_when_browsing_its_community(app, community_with_a_microblog):
    """app/community/routes.py:370 filtered these out of the listing."""
    viewer, community, microblog, ordinary = community_with_a_microblog

    body = logged_in_client(app, viewer).get(f'/c/{community.name}').get_data(as_text=True)

    assert f'/post/{ordinary.id}' in body, 'the ordinary post should be listed'
    assert f'/post/{microblog.id}' in body


def test_microblog_post_is_in_the_community_rss_feed(app, community_with_a_microblog):
    """app/community/routes.py:715 carried the same filter on the RSS route."""
    viewer, community, microblog, ordinary = community_with_a_microblog

    body = logged_in_client(app, viewer).get(f'/community/{community.name}/feed').get_data(as_text=True)

    assert f'/post/{ordinary.id}' in body, 'the ordinary post should be in the feed'
    assert f'/post/{microblog.id}' in body
