"""A local-only domain's instance row stays off every other storefront (plan C2, task 6). Owner ruling: on hell.cloud
the onion's row is hidden, so the lists leave it out and `/instance/<onion>` answers 404; on the onion it is shown as a
sister. A federating sister (shop.example, from `twins`) is listed like any row, but never as "federated". The admin
instance list shows every row to an admin."""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Instance, ModLog, Site
from app.storefront import hidden_instance_ids, instance_visible_here
from tests.conftest import forget_login
from tests.factories import bearer, grant_permission, make_instance, make_post
from tests.test_per_domain_ap_routes import SHOP, onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import BASE, Visitor
from tests.two_domains import HELL, ONION, default_request, onion_request, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

PEER = 'peer.example'
HOSTS = {**BASE, SHOP: f'https://{SHOP}'}


@pytest.fixture
def scene(app, twins):
    """hell.cloud, the onion and shop.example, each with a local user and community, and a remote peer. The onion's
    row is given what a peer's has (software, a post), so only its being local-only keeps it out."""
    hell_bob, hell_art, onion_bob, onion_art, shop_bob, shop_art = twins
    onion_bob.private_key = hell_bob.private_key = 'a key'   # approved accounts (approval_required)
    onion_row = db.session.get(Instance, onion_bob.instance_id)
    onion_row.software = 'piefed'
    peer = make_instance(PEER, software='piefed')
    db.session.get(Site, 1).private_instance = False   # anonymous visitors reach the pages
    db.session.commit()
    make_post(onion_art, onion_bob, f'http://{ONION}/post/1', title='ONIONPOST').local_only = True   # as creation stamps it
    db.session.commit()
    return SimpleNamespace(hell_bob=hell_bob, onion_bob=onion_bob, onion_art=onion_art, onion_row=onion_row,
                           shop_row=db.session.get(Instance, shop_bob.instance_id), peer=peer)


def get(app, path, host=HELL, user=None):
    forget_login()
    if user is not None:
        return Visitor(app, user, host).get(path)
    return app.test_client().get(path, base_url=HOSTS[host])


# --- the predicate -----------------------------------------------------------------------------------------------

def test_the_onion_row_is_hidden_everywhere_but_on_the_onion(app, scene):
    with default_request(app):
        assert hidden_instance_ids() == {scene.onion_row.id}
        assert not instance_visible_here(scene.onion_row.id)
        assert instance_visible_here(1) and instance_visible_here(scene.shop_row.id) and instance_visible_here(scene.peer.id)
    with onion_request(app):
        assert hidden_instance_ids() == set()
        assert instance_visible_here(scene.onion_row.id)
    assert not instance_visible_here(scene.onion_row.id)   # outside a request the storefront is the default


# --- /instances ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('signed_in', [False, True])
@pytest.mark.parametrize('query', ['', '?search=onion', '?filters=online', '?filters=trusted'])
def test_hells_instance_list_names_no_onion(app, scene, signed_in, query):
    scene.onion_row.trusted = scene.peer.trusted = True
    db.session.commit()
    response = get(app, f'/instances{query}', user=scene.hell_bob if signed_in else None)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert ONION not in body
    if 'search' not in query:
        assert PEER in body


def test_the_onions_instance_list_shows_it_as_a_sister(app, scene):
    body = get(app, '/instances', host=ONION, user=scene.onion_bob).get_data(as_text=True)
    assert ONION in body and HELL in body and PEER in body


def test_a_federating_sister_is_listed_but_not_as_federated(app, scene):
    assert SHOP in get(app, '/instances').get_data(as_text=True)
    federated = get(app, '/instances?filters=federated').get_data(as_text=True)
    assert PEER in federated
    assert SHOP not in federated and ONION not in federated


@pytest.mark.parametrize('signed_in', [False, True])
def test_hells_federated_instances_apis_name_no_onion(app, scene, monkeypatch, signed_in):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    lemmy = get(app, '/api/v3/federated_instances', user=scene.hell_bob if signed_in else None)
    forget_login()
    headers = {'Authorization': bearer(scene.hell_bob)} if signed_in else {}
    alpha = app.test_client().get('/api/alpha/federated_instances', base_url=HOSTS[HELL], headers=headers)
    for response in (lemmy, alpha):
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert PEER in body
        assert ONION not in body


# --- /instance/<domain> -------------------------------------------------------------------------------------------

@pytest.mark.parametrize('signed_in', [False, True])
@pytest.mark.parametrize('suffix', ['', '/people', '/posts'])
def test_the_onions_instance_pages_are_not_found_on_hell(app, scene, signed_in, suffix):
    response = get(app, f'/instance/{ONION}{suffix}', user=scene.hell_bob if signed_in else None)
    assert response.status_code == 404
    assert ONION not in response.get_data(as_text=True)


@pytest.mark.parametrize('suffix', ['', '/people', '/posts'])
def test_the_onions_instance_pages_render_on_the_onion(app, scene, suffix):
    response = get(app, f'/instance/{ONION}{suffix}', host=ONION, user=scene.onion_bob)
    assert response.status_code == 200
    assert ONION in response.get_data(as_text=True)


@pytest.mark.parametrize('suffix', ['', '/people', '/posts'])
def test_a_federating_sisters_and_a_peers_instance_pages_render_on_hell(app, scene, suffix):
    for domain in (SHOP, PEER):
        response = get(app, f'/instance/{domain}{suffix}')
        assert response.status_code == 200
        assert domain in response.get_data(as_text=True)


# --- the modlog ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('signed_in', [False, True])
def test_hells_modlog_names_no_onion(app, scene, signed_in):
    """The entry is an onion admin's, so the instance label is all that could name the onion: a reader who is not
    an admin sees no actor's name. (A community entry also links the community, `/c/art@abc.onion`: a community's
    path name, not the instance row, and left as earlier tasks made it.)"""
    db.session.add(ModLog(user_id=scene.onion_bob.id, action='ban_user', link='u/somebody', public=True, type='admin'))
    db.session.commit()
    response = get(app, '/modlog', user=scene.hell_bob if signed_in else None)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert ' admin' in body   # the entry itself is listed, with no instance named
    assert ONION not in body


def test_the_onions_modlog_names_its_own_instance(app, scene):
    db.session.add(ModLog(user_id=scene.onion_bob.id, action='ban_user', link='u/somebody', public=True, type='admin'))
    db.session.commit()
    assert f'{ONION} admin' in get(app, '/modlog', host=ONION, user=scene.onion_bob).get_data(as_text=True)


# --- the admin instance list --------------------------------------------------------------------------------------

def test_hells_admin_instance_list_still_shows_the_onion(app, scene):
    grant_permission(scene.hell_bob, 'change instance settings')
    g.admin_ids = []
    response = get(app, '/admin/instances', user=scene.hell_bob)
    assert response.status_code == 200
    assert ONION in response.get_data(as_text=True)
