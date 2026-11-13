"""A domain id in a URL beyond PostgreSQL's integer (fuzz-routes finding, 2026-11-13): GET /d/99999999999999999999
passed the id as text to the domain.id lookup and PostgreSQL refused it, NumericValueOutOfRange, a 500. An <int:>
segment passes a Python int, which PostgreSQL compares with the column whatever its size: those stay 404s, as checked."""
import pytest

from app import db
from tests.factories import make_instance, make_user
from tests.test_admin_site_profile import csrf, login

pytestmark = pytest.mark.usefixtures('site')

HUGE = '99999999999999999999'


@pytest.mark.parametrize('path', [f'/d/{HUGE}', f'/d/{HUGE}/feed', f'/post/{HUGE}', '/d/2147483648', '/d/١٢'],
                         ids=['domain', 'domain-rss', 'post', 'domain-just-over', 'domain-arabic-digits'])
def test_an_id_no_row_can_have_is_not_found(app, db_session, path):
    assert app.test_client().get(path).status_code == 404


def test_an_id_no_row_can_have_is_not_found_posted(app, db_session):
    user = make_user(make_instance('test.piefed.local', software='piefed'), 'rangeposter', local=True)
    user.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, user)
    token = csrf(app, client)
    assert client.post(f'/chat/{HUGE}/block_instance', data={'csrf_token': token}).status_code == 404


def test_the_largest_id_still_routes(app):
    adapter = app.url_map.bind('localhost')
    assert adapter.match('/post/2147483647')[1] == {'post_id': 2147483647}
