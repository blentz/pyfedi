"""Subscribing to a feed on the web kept the request's database session (fuzz-routes finding, 2026-11-13):
join_feed ended with `finally: db.session.remove()`, which ran inside the request, so the signed-in person, expired by
its rollback or commit, could not be loaded again, and after_request's current_user.is_active raised
DetachedInstanceError: POST /feed/<unknown>/subscribe was a 500, not a 404. Flask's own teardown ends the session."""
import pytest

from app import db
from app.models import FeedMember
from tests.factories import make_instance, make_local_feed, make_user
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def subscriber(app, db_session):
    local = make_instance('test.piefed.local', software='piefed')
    user = make_user(local, 'feedfan', local=True, with_keys=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    return client, csrf(app, client), user, local


def test_an_unknown_feed_is_a_404(subscriber):
    client, token, _user, _local = subscriber
    assert client.post('/feed/no-such-feed/subscribe', data={'csrf_token': token}).status_code == 404


def test_a_feed_is_joined_and_the_page_goes_on(subscriber):
    client, token, user, local = subscriber
    feed = make_local_feed('joinable', public=True, user=user)
    db.session.commit()
    response = client.post(f'/feed/{feed.name}/subscribe', data={'csrf_token': token})
    assert response.status_code == 302
    assert FeedMember.query.filter_by(user_id=user.id, feed_id=feed.id).count() == 1
