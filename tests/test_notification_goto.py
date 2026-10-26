"""`Notification.url` for a community follows by id (follow-up F1).

A new-moderator notification used to store `/c/<stored name>`, which on the onion is the onion's own community of that
name. A new row stores `/community/<id>`; a row stored before keeps its `/c/<name>` and is resolved when followed: by
the `community_id` in its targets, else by looking the name up (a bare name is the first domain's). A name that finds
nothing sends the reader to their notifications instead of to a namesake."""
import pytest

from app import db
from app.constants import NOTIF_NEW_MOD
from app.local_names import notification_href
from app.models import Notification
from tests.two_domains import default_request, onion_request, two_domains  # noqa: F401  (the fixture)

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def world(two_domains):
    first, onion = two_domains.domains
    reader = two_domains.local_user('reader', first)
    return reader, two_domains.local_community('alpha', first), two_domains.local_community('alpha', onion)


def notification(reader, url, targets=None):
    row = Notification(title='You are now a moderator', url=url, user_id=reader.id, author_id=reader.id,
                       notif_type=NOTIF_NEW_MOD, targets=targets if targets is not None else {'gen': '0'})
    db.session.add(row)
    db.session.commit()
    return row


def test_a_url_that_is_not_a_stored_community_name_is_followed_as_it_is(app, world):
    reader, first_alpha, onion_alpha = world
    for url in ('/post/5', f'/community/{first_alpha.id}', '/admin/reports'):
        assert notification_href(notification(reader, url)) == url


def test_an_old_url_with_the_community_id_in_its_targets_goes_by_that_id(app, world):
    reader, first_alpha, onion_alpha = world
    row = notification(reader, '/c/alpha', {'gen': '0', 'community_id': onion_alpha.id})
    with onion_request(app):
        assert notification_href(row) == f'/community/{onion_alpha.id}'


def test_an_old_url_without_an_id_goes_to_the_first_domains_community_of_that_name(app, world):
    reader, first_alpha, onion_alpha = world
    row = notification(reader, '/c/alpha')
    with onion_request(app):
        assert notification_href(row) == f'/community/{first_alpha.id}'
    with default_request(app):
        assert notification_href(row) == f'/community/{first_alpha.id}'


def test_an_old_url_with_a_domain_goes_to_that_domains_community(app, world):
    reader, first_alpha, onion_alpha = world
    with default_request(app):
        assert notification_href(notification(reader, '/c/alpha@abc.onion')) == f'/community/{onion_alpha.id}'


def test_an_old_url_whose_name_matches_nothing_has_no_href(app, world):
    reader = world[0]
    with onion_request(app):
        assert notification_href(notification(reader, '/c/ghost')) is None


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def test_following_an_old_notification_redirects_to_the_community_by_id(app, world):
    reader, first_alpha, onion_alpha = world
    row = notification(reader, '/c/alpha')
    client = app.test_client()
    login(client, reader)
    response = client.get(f'/notification/{row.id}/goto')
    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/community/{first_alpha.id}')


def test_following_a_notification_that_names_nothing_goes_to_the_notification_list(app, world):
    reader = world[0]
    row = notification(reader, '/c/ghost')
    client = app.test_client()
    login(client, reader)
    response = client.get(f'/notification/{row.id}/goto')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/notifications')
