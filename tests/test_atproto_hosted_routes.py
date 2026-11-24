"""ATProto P2: the pages for Bluesky accounts made here: the member's (§8) and the admins' (§9, §10)."""
import re

import httpx
import pytest

from app import db, storefront
from app.connections import setting_name
from app.connections import bluesky_hosted as hosted
from app.connections.apps import domain_switch
from app.models import SiteDomain, SocialConnection
from app.utils import get_setting, set_setting
from tests.shared_fixtures.atproto import a_hosted, a_member, follow
from tests.test_connections_domains import SHOP

PAGE = '/user/settings/connections'


def page(hosted_world) -> str:
    return hosted_world.client.get(PAGE).data.decode()


def post(hosted_world, path, **data):
    return hosted_world.client.post(path, data={'csrf_token': hosted_world.token, **data})


# --------------------------------------------------------------------------
# the member's page
# --------------------------------------------------------------------------

def test_the_page_offers_one_click_creation(hosted_world):
    body = page(hosted_world)
    assert f'Create @author.{hosted_world.home}' in body and 'Connect Bluesky' not in body   # no OAuth key here
    assert 'name="label" value="author"' in body
    assert f'Create @author-2.{hosted_world.home}' in hosted_world.client.get(f'{PAGE}?label=author-2').data.decode()
    assert f'Create @author.{hosted_world.home}' in hosted_world.client.get(f'{PAGE}?label=founder').data.decode()
    assert post(hosted_world, '/user/settings/connections/bluesky/create', label='founder').location.endswith(
        f'{PAGE}?label=author')   # someone else's name: offered their own
    assert SocialConnection.query.count() == 0


def test_the_page_offers_connecting_an_own_account_with_the_key(hosted_world, monkeypatch):
    from app.connections.bluesky import new_key_jwk
    monkeypatch.setitem(hosted_world.app.config, 'BLUESKY_OAUTH_KEY', new_key_jwk())
    assert 'Connect Bluesky' in page(hosted_world)


def test_with_no_handle_left_nothing_is_offered(hosted_world, monkeypatch):
    none_left = hosted.HandleRefused('No handle of your name is free.', None)
    monkeypatch.setattr(hosted, 'own_label', lambda user, wanted=None: (_ for _ in ()).throw(none_left))
    assert 'Create @' not in page(hosted_world)
    monkeypatch.setattr(hosted, 'request_account', lambda user, label=None: (_ for _ in ()).throw(none_left))
    response = post(hosted_world, '/user/settings/connections/bluesky/create', label='author')
    assert response.location.endswith(PAGE)
    assert 'No handle of your name is free.' in page(hosted_world)
    connection = a_hosted(hosted_world, hosted_world.author)
    monkeypatch.setattr(hosted, 'rename', lambda connection, label: (_ for _ in ()).throw(none_left))
    post(hosted_world, f'/user/settings/connections/{connection.id}/handle', label='author-2')
    body = page(hosted_world)
    assert 'No handle of your name is free.' in body and 'instead?' not in body


def test_no_creation_where_hosting_is_off(hosted_world):
    set_setting(setting_name(hosted.HOSTING), False)
    body = page(hosted_world)
    assert 'Create @' not in body and 'No services are available here yet.' in body
    assert post(hosted_world, '/user/settings/connections/bluesky/create').status_code == 404


def test_creating_from_the_page(hosted_world):
    response = post(hosted_world, '/user/settings/connections/bluesky/create', label='author')
    assert response.status_code == 302
    connection = SocialConnection.query.one()
    assert connection.slot_state == hosted.ACTIVE
    body = page(hosted_world)
    assert f'author.{hosted_world.home}' in body and 'Get an app password' in body and 'Turn off' in body
    assert 'Delete this Bluesky account' in body and 'Create @' not in body


def test_a_taken_handle_brings_the_page_back_with_the_next(hosted_world):
    hosted_world.pds.taken.add(f'author.{hosted_world.home}')
    response = post(hosted_world, '/user/settings/connections/bluesky/create', label='author')
    assert response.location.endswith(f'{PAGE}?label=author-2')
    assert f'Try @author-2.{hosted_world.home} instead?' in hosted_world.client.get(response.location).data.decode()


def test_an_unreachable_pds_is_said(hosted_world):
    hosted_world.pds.fail['com.atproto.server.createAccount'] = httpx.Response(500)
    post(hosted_world, '/user/settings/connections/bluesky/create')
    assert 'Bluesky could not be reached' in page(hosted_world) and SocialConnection.query.count() == 0


def test_waiting_shows_the_place(hosted_world):
    for name in ('first', 'second'):
        a_hosted(hosted_world, a_member(name))
    post(hosted_world, '/user/settings/connections/bluesky/create')
    body = page(hosted_world)
    assert 'you are on the waiting list' in body and 'On the waiting list: number 1' in body
    assert 'Leave the waiting list' in body and 'Get an app password' not in body


def test_turning_off_and_on_from_the_page(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author)
    assert post(hosted_world, f'/user/settings/connections/{connection.id}/turn-off').status_code == 302
    assert 'Turned off' in page(hosted_world) and 'Turn on' in page(hosted_world)
    assert post(hosted_world, f'/user/settings/connections/{connection.id}/turn-off').status_code == 400
    assert post(hosted_world, f'/user/settings/connections/{connection.id}/turn-on').status_code == 302
    assert db.session.get(SocialConnection, connection.id).slot_state == hosted.ACTIVE
    assert post(hosted_world, f'/user/settings/connections/{connection.id}/turn-on').status_code == 400


def test_turning_on_with_every_place_taken(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author, state=hosted.DEACTIVATED)
    for name in ('first', 'second'):
        a_hosted(hosted_world, a_member(name))
    post(hosted_world, f'/user/settings/connections/{connection.id}/turn-on')
    assert 'you are on the waiting list' in page(hosted_world)


def test_turning_off_or_on_when_the_pds_is_down(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author)
    hosted_world.pds.fail['com.atproto.server.deactivateAccount'] = httpx.Response(503)
    post(hosted_world, f'/user/settings/connections/{connection.id}/turn-off')
    assert 'Bluesky could not be reached' in page(hosted_world)
    connection.slot_state = hosted.DEACTIVATED
    db.session.commit()
    hosted_world.pds.fail['com.atproto.server.createSession'] = httpx.Response(503)
    post(hosted_world, f'/user/settings/connections/{connection.id}/turn-on')
    assert 'Bluesky could not be reached' in page(hosted_world)


def test_changing_the_handle_from_the_page(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author)
    post(hosted_world, f'/user/settings/connections/{connection.id}/handle', label='author-2')
    assert f'Your Bluesky handle is now @author-2.{hosted_world.home}.' in page(hosted_world)
    hosted_world.pds.taken.add(f'author-5.{hosted_world.home}')
    post(hosted_world, f'/user/settings/connections/{connection.id}/handle', label='author-5')
    assert f'Try @author-6.{hosted_world.home} instead?' in page(hosted_world)
    post(hosted_world, f'/user/settings/connections/{connection.id}/handle', label='founder')
    assert f'Try @author.{hosted_world.home} instead?' in page(hosted_world)
    hosted_world.pds.fail['com.atproto.identity.updateHandle'] = httpx.Response(502)
    post(hosted_world, f'/user/settings/connections/{connection.id}/handle', label='author')
    assert 'Bluesky could not be reached' in page(hosted_world)


def test_an_app_password_is_shown_once_and_never_kept(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author)
    response = post(hosted_world, f'/user/settings/connections/{connection.id}/app-password')
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert 'abcd-efgh-ijkl-mnop' in response.data.decode()
    with hosted_world.client.session_transaction() as session:
        assert 'abcd-efgh-ijkl-mnop' not in str(dict(session))
    hosted_world.pds.fail['com.atproto.server.createAppPassword'] = httpx.Response(500)
    assert post(hosted_world, f'/user/settings/connections/{connection.id}/app-password').status_code == 302
    assert 'Bluesky could not be reached' in page(hosted_world)


@pytest.mark.parametrize('action', ['handle', 'app-password'])
def test_an_inactive_account_refuses_changes(hosted_world, action):
    connection = a_hosted(hosted_world, hosted_world.author, state=hosted.WAITING)
    assert post(hosted_world, f'/user/settings/connections/{connection.id}/{action}').status_code == 400


def test_someone_elses_or_an_oauth_connection_is_not_found(hosted_world):
    other = a_hosted(hosted_world, a_member('other'))
    assert post(hosted_world, f'/user/settings/connections/{other.id}/turn-off').status_code == 404
    own = SocialConnection(user_id=hosted_world.author.id, service='bluesky', variant='oauth', remote_id='did:plc:own',
                           remote_name='me', sealed_tokens='x')
    db.session.add(own)
    db.session.commit()
    assert post(hosted_world, f'/user/settings/connections/{own.id}/turn-off').status_code == 404


def test_deleting_from_the_page(hosted_world):
    connection = a_hosted(hosted_world, hosted_world.author)
    did = connection.remote_id
    post(hosted_world, f'/user/settings/connections/{connection.id}/disconnect')
    assert did not in hosted_world.pds.accounts and SocialConnection.query.count() == 0


# --------------------------------------------------------------------------
# the admins' pages
# --------------------------------------------------------------------------

@pytest.fixture
def admin(hosted_world):
    from tests.factories import make_admin
    make_admin(hosted_world.author)
    return hosted_world


def test_the_admin_page_shows_slots_and_lists(admin):
    waiting = a_hosted(admin, a_member('waiter'), state=hosted.WAITING)
    follow(waiting.user, 2)
    a_hosted(admin, a_member('active'), slot_pinned=True)
    body = admin.client.get('/admin/connections').data.decode()
    assert 'Bluesky accounts made here' in body and admin.home in body
    assert 'Active: 1 of 2. Accounts on the server: 1 of 3. Slots are kept: sticky.' in body
    assert f'@waiter.{admin.home}' in body and 'Give a place now' in body and 'Kept' in body
    assert re.search(r'<td>@waiter\.[^<]+</td><td>2</td>', body)


def test_the_admin_switch(admin):
    post(admin, '/admin/connections', action='hosting', on='0')
    assert get_setting(setting_name(hosted.HOSTING), True) is False
    post(admin, '/admin/connections', action='hosting', on='1')
    assert get_setting(setting_name(hosted.HOSTING), False) is True


def test_granting_and_pinning(admin):
    waiting = a_hosted(admin, a_member('waiter'), state=hosted.WAITING)
    post(admin, '/admin/connections', action='grant', connection_id=waiting.id)
    waiting = db.session.get(SocialConnection, waiting.id)
    assert waiting.slot_state == hosted.ACTIVE and waiting.slot_pinned
    post(admin, '/admin/connections', action='unpin', connection_id=waiting.id)
    assert db.session.get(SocialConnection, waiting.id).slot_pinned is False
    post(admin, '/admin/connections', action='pin', connection_id=waiting.id)
    assert db.session.get(SocialConnection, waiting.id).slot_pinned is True
    assert post(admin, '/admin/connections', action='grant', connection_id=waiting.id).status_code == 400


def test_a_grant_that_fails_is_said(admin):
    waiting = a_hosted(admin, a_member('waiter'), state=hosted.WAITING)
    admin.pds.fail['com.atproto.server.createAccount'] = httpx.Response(500)
    post(admin, '/admin/connections', action='grant', connection_id=waiting.id)
    assert 'could not make that account' in admin.client.get('/admin/connections').data.decode()


def test_admin_actions_refused(admin, monkeypatch):
    waiting = a_hosted(admin, a_member('waiter'), state=hosted.WAITING)
    assert post(admin, '/admin/connections', action='pin', connection_id=waiting.id).status_code == 400
    assert post(admin, '/admin/connections', action='grant', connection_id=999999).status_code == 404
    monkeypatch.setitem(admin.app.config, 'ATPROTO_PDS_URL', '')
    assert post(admin, '/admin/connections', action='hosting', on='1').status_code == 400
    assert 'Bluesky accounts made here' not in admin.client.get('/admin/connections').data.decode()


@pytest.fixture
def shop(admin, monkeypatch):
    row = SiteDomain(domain=SHOP, scheme='https', federates=True, is_default=False)
    db.session.add(row)
    db.session.commit()
    storefront.clear_domain_cache()
    monkeypatch.setattr(admin.pds, 'domains', [admin.home, SHOP])
    return row


def test_a_domain_switch(admin, shop):
    path = f'/admin/domains/{shop.id}/connections'
    body = admin.client.get(path).data.decode()
    assert 'Bluesky accounts on this domain' in body
    post(admin, path, action='hosting', value='off')
    assert domain_switch(hosted.HOSTING, shop.id) == 'off'
    post(admin, path, action='hosting', value='')
    assert domain_switch(hosted.HOSTING, shop.id) is None
    assert post(admin, path, action='hosting', value='maybe').status_code == 400


def test_no_domain_switch_where_the_pds_gives_no_handles(admin, shop, monkeypatch):
    monkeypatch.setattr(admin.pds, 'domains', [admin.home])
    path = f'/admin/domains/{shop.id}/connections'
    assert 'Bluesky accounts on this domain' not in admin.client.get(path).data.decode()
    assert post(admin, path, action='hosting', value='on').status_code == 400
