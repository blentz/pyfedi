"""One gate in front of every route that names an account, community, post or reply (follow-up F6, review I8).

Off the onion, a route whose arguments name an onion row answers as a route naming a row nobody holds: the same status
and the same body, so no route is an oracle for what exists on the onion. The gate is one table of argument names
(app/hidden_routes.py): tests/test_hidden_routes_table.py walks the URL map against it."""
import pytest

from app import db
from tests.test_instance_visible_here import get, scene  # noqa: F401 -- scene is a fixture
from tests.test_local_only_actors_hidden import world  # noqa: F401 -- world is a fixture
from tests.test_onion_hidden_on_clearnet import onion  # noqa: F401 -- a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401
from tests.conftest import forget_login

pytestmark = pytest.mark.usefixtures('site')

NOBODY = 2 ** 30   # an id no row holds


def answer(response):
    return response.status_code, response.headers.get('Location'), response.get_data()


def visit(app, user, method, path, host=FIRST):
    forget_login()
    visitor = Visitor(app, user, host)
    return visitor.post(path) if method == 'POST' else visitor.get(path)


def assert_same(app, user, method, hidden, missing, host=FIRST):
    found, absent = visit(app, user, method, hidden, host), visit(app, user, method, missing, host)
    assert answer(found) == answer(absent), (hidden, answer(found)[:2], answer(absent)[:2])
    assert found.status_code == 404, hidden


# --- C3: a moderator's routes on a user id ------------------------------------------------------------------------

def moderator_routes(onion, user_id):
    community = onion.first_art.id
    return [('GET', f'/community/community/{community}/{user_id}/ban_user_community'),
            ('POST', f'/community/community/{community}/{user_id}/ban_user_community'),
            ('POST', f'/community/community/{community}/{user_id}/unban_user_community'),
            ('POST', f'/community/community/{community}/{user_id}/kick_user_community'),
            ('POST', f'/community/community/{community}/make_owner/{user_id}'),
            ('POST', f'/community/community/{community}/remove_owner/{user_id}'),
            ('POST', f'/community/community/{community}/moderators/add/{user_id}'),
            ('POST', f'/community/community/{community}/moderators/remove/{user_id}'),
            ('GET', f'/chat/ban_from_mod/{user_id}/{community}')]


def test_a_moderators_routes_answer_an_onion_account_as_an_id_nobody_holds(app, onion):
    for (method, hidden), (_, missing) in zip(moderator_routes(onion, onion.onion_bob.id),
                                              moderator_routes(onion, NOBODY)):
        assert_same(app, onion.admin, method, hidden, missing)
    assert db.session.get(type(onion.onion_bob), onion.onion_bob.id).id == onion.onion_bob.id   # nothing was done


def test_on_the_onion_the_ban_page_names_its_account(app, onion):
    response = visit(app, onion.admin, 'GET', f'/community/community/{onion.onion_art.id}/{onion.onion_bob.id}'
                                              f'/ban_user_community', host=ONION)
    assert response.status_code == 200


# --- C2: a new message to an account by id --------------------------------------------------------------------------

@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_a_new_message_to_an_onion_account_answers_as_one_to_an_id_nobody_holds(app, onion, method):
    assert_same(app, onion.admin, method, f'/chat/{onion.onion_bob.id}/new', f'/chat/{NOBODY}/new')


def test_on_the_onion_a_new_message_to_it_is_offered(app, onion):
    assert visit(app, onion.admin, 'GET', f'/chat/{onion.onion_bob.id}/new', host=ONION).status_code == 200


# --- I1: the lookup redirects -----------------------------------------------------------------------------------------

@pytest.mark.parametrize('signed_in', [False, True])
@pytest.mark.parametrize('hidden, missing', [
    (f'/community/lookup/art/{ONION}', f'/community/lookup/nope/{ONION}'),
    (f'/user/lookup/bob/{ONION}', f'/user/lookup/nobody/{ONION}'),
    (f'/community/lookup/art/www.{ONION}', f'/community/lookup/nope/www.{ONION}'),
    (f'/c/art@{ONION}', f'/c/nope@{ONION}'),
    (f'/u/bob@{ONION}', f'/u/nobody@{ONION}'),
    (f'/community/art@{ONION}/moderate', f'/community/nope@{ONION}/moderate'),
])
def test_a_lookup_of_an_onion_name_answers_as_one_of_a_name_nobody_holds(app, onion, signed_in, hidden, missing):
    if signed_in:
        assert_same(app, onion.first_bob, 'GET', hidden, missing)
    else:
        found, absent = get(app, hidden), get(app, missing)
        assert answer(found) == answer(absent), hidden
        assert found.status_code == 404, hidden


def test_on_the_onion_its_lookups_still_redirect(app, onion):
    response = visit(app, onion.first_bob, 'GET', f'/community/lookup/art/{ONION}', host=ONION)
    assert response.status_code == 302 and response.headers['Location'].endswith('/c/art')
    response = visit(app, onion.first_bob, 'GET', f'/user/lookup/bob/{ONION}', host=ONION)
    assert response.status_code == 302 and response.headers['Location'].endswith('/u/bob')


# --- I2: subscribing to an account's posts ---------------------------------------------------------------------------

def test_subscribing_to_an_onion_account_answers_as_for_an_id_nobody_holds(app, onion):
    assert_same(app, onion.first_bob, 'POST', f'/user/{onion.onion_bob.id}/notification',
                f'/user/{NOBODY}/notification')
    assert not onion.onion_bob.notify_new_posts(onion.first_bob.id)


# --- M6: an account's alternate name ------------------------------------------------------------------------------

@pytest.mark.parametrize('sub', ['', '/report', '/block', '/upvotes'])
def test_an_onion_accounts_alternate_name_answers_as_a_name_nobody_holds(app, onion, sub):
    onion.onion_bob.alt_user_name = 'quietotter'
    db.session.commit()
    method = 'POST' if sub == '/block' else 'GET'
    assert_same(app, onion.first_bob, method, f'/u/quietotter{sub}', f'/u/loudbadger{sub}')


# --- M10: junk in an argument -------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/u/%00', '/u/%00/upvotes', '/u/bob%00@x/report', '/c/%00', '/community/%00/moderate',
                                  '/community/%00/feed', f'/community/lookup/%00/{ONION}', '/community/lookup/art/%00',
                                  '/user/lookup/%00/x', '/user/lookup/bob/a%00b', '/u/bob@[::/upvotes',
                                  '/community/lookup/art/[::', '/c/a@b@c'])
@pytest.mark.parametrize('signed_in', [False, True])
def test_junk_in_an_object_argument_is_not_a_server_error(app, onion, path, signed_in):
    response = visit(app, onion.first_bob, 'GET', path) if signed_in else get(app, path)
    assert response.status_code in (302, 400, 404), (path, response.status_code)
