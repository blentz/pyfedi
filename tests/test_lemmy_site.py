"""Lemmy's site document and login for Jerboa (plan W3; spec 2026-11-10-lemmy-v3-design §2, §3): the site document
decodes as Lemmy 0.19.11's, carries the person's own `my_user` when they send a token and is then never cached; the
login is alpha's under Lemmy's names, with its checks and limit; refusals are Lemmy's `{"error": code}`."""
import pytest
from flask import abort as flask_abort
from flask_smorest import abort as smorest_abort
from sqlalchemy.orm.exc import NoResultFound

from app import cache, db, limiter
from app.lemmy import errors
from app.models import Emoji
from tests import lemmy_app_contract as contract
from tests import test_login_gate as gate_tests
from tests import test_onion_guard_limits as limit_tests
from tests.factories import make_community, make_community_member, make_user, make_user_block

private = gate_tests.private   # the private-instance fixture
limited_app = limit_tests.limited_app   # an app whose limiter counts

PASSWORD = 'a-good-password'


@pytest.fixture
def world(app, api_baseline, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    cache.clear()
    person, other = make_user(None, 'lemmysite', local=True), make_user(None, 'lemmyother', local=True)
    person.set_password(PASSWORD)
    person.default_sort, person.default_filter, person.hide_nsfw = 'top', 'local', 2
    community = make_community('lemmysite')
    make_community_member(person, community)
    make_user_block(person, other)
    db.session.commit()
    return {'person': person, 'community': community, 'client': app.test_client(), 'flask': app}


def _login(client, name='lemmysite', password=PASSWORD, **kwargs):
    return client.post('/api/v3/user/login', json={'username_or_email': name, 'password': password,
                                                   'totp_2fa_token': None}, **kwargs)


def _refused(response, code, status=400):
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.get_json() == {'error': code}


def test_the_site_document_decodes_as_lemmys_and_names_no_one_logged_out(world):
    body = world['client'].get('/api/v3/site').get_json()
    assert (found := contract.problems(body, 'GetSiteResponse')) == [], found
    assert 'my_user' not in body


def test_the_sites_custom_emojis_decode_too(world):
    db.session.add(Emoji(token=':lemmywave:', url='/static/media/wave.png', category='hands', aliases='hi hello',
                         instance_id=1))
    db.session.commit()
    body = world['client'].get('/api/v3/site').get_json()
    assert (found := contract.problems(body, 'GetSiteResponse')) == [], found
    emoji = body['custom_emojis'][0]
    assert emoji['custom_emoji']['shortcode'] == 'lemmywave'
    assert [keyword['keyword'] for keyword in emoji['keywords']] == ['hi', 'hello']


def test_logging_in_gives_a_token_the_site_document_knows(world):
    response = _login(world['client'])
    assert response.status_code == 200
    body = response.get_json()
    assert contract.problems(body, 'LoginResponse') == [] and body['jwt']
    mine = world['client'].get('/api/v3/site', headers={'Authorization': f"Bearer {body['jwt']}"}).get_json()
    assert (found := contract.problems(mine, 'GetSiteResponse')) == [], found
    view = mine['my_user']['local_user_view']
    assert view['person']['name'] == 'lemmysite' and view['local_user']['person_id'] == world['person'].id
    assert (view['local_user']['default_sort_type'], view['local_user']['default_listing_type']) == ('TopDay', 'Local')
    assert view['local_user']['blur_nsfw'] and view['local_user']['show_nsfw']
    assert [row['community']['name'] for row in mine['my_user']['follows']] == ['lemmysite']
    assert [row['target']['name'] for row in mine['my_user']['person_blocks']] == ['lemmyother']


def test_a_persons_site_document_is_never_served_from_the_cache_nor_cached(world):
    token = world['person'].encode_jwt_token()
    assert 'my_user' in world['client'].get('/api/v3/site', headers={'Authorization': f'Bearer {token}'}).get_json()
    assert 'my_user' not in world['client'].get('/api/v3/site').get_json()
    assert 'my_user' in world['client'].get('/api/v3/site', headers={'Authorization': f'Bearer {token}'}).get_json()


def test_a_token_that_is_no_good_is_refused_on_the_site_document(world):
    _refused(world['client'].get('/api/v3/site', headers={'Authorization': 'Bearer nonsense'}), 'incorrect_login', 401)


def test_a_lapsed_account_is_told_so(world, monkeypatch):
    def lapsed(*_args, **_kwargs):
        raise Exception('access_lapsed')
    monkeypatch.setattr('app.lemmy.site.authorise_api_user', lapsed)
    _refused(world['client'].get('/api/v3/site', headers={'Authorization': 'Bearer x'}), 'access_lapsed', 403)


@pytest.mark.parametrize('name,password', [('lemmysite', 'wrong'), ('nobody', PASSWORD)])
def test_a_wrong_name_or_password_is_incorrect_login(world, name, password):
    _refused(_login(world['client'], name, password), 'incorrect_login')


@pytest.mark.parametrize('body', [{}, {'username_or_email': 'lemmysite'}, {'username_or_email': 3, 'password': 'x'},
                                  {'username_or_email': ' ', 'password': PASSWORD}, ['not', 'an', 'object']])
def test_a_login_missing_its_fields_is_incorrect_login(world, body):
    _refused(world['client'].post('/api/v3/user/login', json=body), 'incorrect_login')


def test_logins_are_limited_per_address(limited_app, api_baseline, monkeypatch):
    monkeypatch.setitem(limited_app.config, 'ENABLE_ALPHA_API', 'true')
    with limited_app.app_context():
        person = make_user(None, 'lemmylimit', local=True)
        person.set_password(PASSWORD)
        db.session.commit()
        client = limited_app.test_client()
        limiter.reset()
        limiter.enabled = True
        try:
            away = {'environ_base': {'REMOTE_ADDR': '203.0.113.9'}}   # 127.0.0.1 is trusted, as alpha's
            codes = [_login(client, 'lemmylimit', 'wrong', **away).status_code for _ in range(21)]
            last = _login(client, 'lemmylimit', **away)
        finally:
            limiter.reset()
            limiter.enabled = False
    assert codes[:20] == [400] * 20 and codes[20] == 429
    _refused(last, 'rate_limit_error', 429)


def test_logging_in_works_on_a_private_instance_and_nothing_else_does_logged_out(private, app):
    client = app.test_client()
    assert client.post('/api/v3/user/login', json={}).status_code == 400   # reached: incorrect_login, not the gate
    _refused(client.get('/api/v3/post/list'), 'incorrect_login', 401)


def test_the_api_is_served_where_alpha_is(world, monkeypatch):
    monkeypatch.setitem(world['flask'].config, 'ENABLE_ALPHA_API', 'false')
    monkeypatch.setattr(world['flask'], 'debug', False)
    _refused(_login(world['client']), 'api_disabled')


@pytest.mark.parametrize('method', ['get', 'post', 'put', 'delete'])
def test_an_endpoint_not_served_is_not_found(world, method):
    _refused(getattr(world['client'], method)('/api/v3/admin/purge/person'), 'not_found', 404)


@pytest.mark.parametrize('raised,expected', [
    (NoResultFound(), ('not_found', 404)), (Exception('post not found'), ('not_found', 404)),
    (Exception('domain_not_found'), ('not_found', 404)), (Exception('incorrect_login'), ('incorrect_login', 400)),
    (Exception('access_denied'), ('access_denied', 400)), (KeyError('internal'), ('unknown', 500))])
def test_alphas_refusals_are_lemmys(app, raised, expected):
    with app.test_request_context('/api/v3/post'):
        response, status = errors.lemmy_error(raised)
    assert (response.get_json(), status) == ({'error': expected[0]}, expected[1])


def test_an_abort_keeps_its_status(app):
    for abort, status, expected in ((flask_abort, 404, 'not_found'), (smorest_abort, 403, None)):
        with app.test_request_context('/api/v3/post'):
            try:
                abort(status, message='refused here') if abort is smorest_abort else abort(status)
            except Exception as raised:
                response, answered = errors.lemmy_error(raised)
        assert answered == status and response.get_json()['error'] == (expected or 'refused here')
