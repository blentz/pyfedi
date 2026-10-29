"""Pixelfed seeding (spec 2026-10-29 §3.1, §3.7): the admin Discovery tab sets how many Pixelfed people per server
the instance actor follows, and lists the ones it does."""
import pytest

from app import db
from app.discovery import SYNC_ACCEPTED, SYNC_PENDING
from app.models import DiscoveryPersonSync, utcnow
from app.utils import get_setting
from tests.discovery_fixtures import admin, fresh_cache  # noqa: F401
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site', 'fresh_cache')
PAGE = '/admin/federation/discovery'


def test_the_form_shows_the_people_setting_at_zero(admin):
    client, _ = admin
    html = client.get(PAGE).get_data(as_text=True)
    assert 'name="sync_people_per_host"' in html
    assert 'Pixelfed people to follow per server' in html


def test_saving_stores_the_people_setting_apart_from_the_channels(admin):
    client, token = admin
    response = client.post(PAGE, data={'csrf_token': token, 'sync_per_host': '0', 'sync_people_per_host': '3',
                                       'sync_save': 'Save'})
    assert response.status_code == 302
    assert (get_setting('discovery_people_per_host'), get_setting('discovery_sync_per_host')) == (3, 0)


@pytest.mark.parametrize('value', ['-1', '51', 'many'])
def test_an_out_of_range_people_count_is_refused_and_nothing_stored(admin, value):
    client, token = admin
    response = client.post(PAGE, data={'csrf_token': token, 'sync_per_host': '0', 'sync_people_per_host': value,
                                       'sync_save': 'Save'})
    assert response.status_code == 200
    assert get_setting('discovery_people_per_host') is None


def test_the_followed_people_are_listed_with_their_state(admin):
    client, _ = admin
    host = make_instance('pixelfed.example', software='pixelfed')
    for name, state, error in (('erin', SYNC_ACCEPTED, None), ('finn', SYNC_PENDING, 'Atom feed: HTTP 404')):
        user = make_user(host, name)
        db.session.add(DiscoveryPersonSync(user_id=user.id, follow_target=f'https://pixelfed.example/users/{name}',
                                           follow_state=state, followed_at=utcnow(), last_error=error))
    db.session.commit()

    html = client.get(PAGE).get_data(as_text=True)

    assert 'Pixelfed people followed' in html
    assert 'https://pixelfed.example/users/erin' in html and 'https://pixelfed.example/users/finn' in html
    assert 'Following' in html and 'Waiting for an answer' in html
    assert 'Atom feed: HTTP 404' in html


def test_no_followed_people_says_so(admin):
    client, _ = admin
    assert 'No Pixelfed people are followed yet.' in client.get(PAGE).get_data(as_text=True)
