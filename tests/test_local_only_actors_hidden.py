"""A logged-out visitor never sees a local-only account or community, as plan B already does for local-only posts and
replies (owner ruling 2026-10-21, plan C2 task 6 fix round 1). They are left out of every listing and the modlog, and
their pages and feeds answer 404; a logged-in user still sees them. On hell.cloud their names read `bob@abc.onion`, so
the onion hostname is what each test looks for. hell.cloud's own accounts and communities are the control."""
from types import SimpleNamespace

import pytest

from app import db
from app.models import File, ModLog, utcnow
from tests.conftest import forget_login
from tests.test_instance_visible_here import HOSTS, get, scene  # noqa: F401 -- scene is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.two_domains import HELL, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

ONION_BOB = f'bob@{ONION}'
ONION_ART = f'art@{ONION}'


@pytest.fixture
def world(app, scene, twins):  # noqa: F811
    hell_bob, hell_art, onion_bob, onion_art, *_ = twins
    avatar = File(file_path='app/static/media/avatar.png')
    db.session.add(avatar)
    db.session.flush()
    for user in (hell_bob, onion_bob):   # what /instance/people/interesting asks of an account
        user.post_count = user.post_reply_count = user.reputation = 5
        user.last_seen = utcnow()
        user.avatar_id = avatar.id
    db.session.commit()
    return SimpleNamespace(hell_bob=hell_bob, hell_art=hell_art, onion_bob=onion_bob, onion_art=onion_art)


def visit(app, path, signed_in, world):
    return get(app, path, user=world.hell_bob if signed_in else None)


# --- the modlog ---------------------------------------------------------------------------------------------------

@pytest.fixture
def entries(world):
    """Public `add_mod` entries, their link text a mark: one in the onion's community, one about an onion account in
    hell's community, and hell's own."""
    rows = [ModLog(community_id=world.onion_art.id, user_id=world.onion_bob.id, target_user_id=world.hell_bob.id,
                   action='add_mod', link='u/bob', link_text='MARK-ONION-COMMUNITY', public=True, type='mod'),
            ModLog(community_id=world.hell_art.id, user_id=world.hell_bob.id, target_user_id=world.onion_bob.id,
                   action='add_mod', link=f'u/{ONION_BOB}', link_text='MARK-ONION-TARGET', public=True, type='mod'),
            ModLog(community_id=world.hell_art.id, user_id=world.hell_bob.id, target_user_id=world.hell_bob.id,
                   action='add_mod', link='u/bob', link_text='MARK-HELL', public=True, type='mod')]
    db.session.add_all(rows)
    db.session.commit()
    return rows


def test_a_logged_out_modlog_lists_nothing_about_a_local_only_community_or_account(app, world, entries):
    body = visit(app, '/modlog', False, world).get_data(as_text=True)
    assert 'MARK-HELL' in body
    assert 'MARK-ONION-COMMUNITY' not in body and 'MARK-ONION-TARGET' not in body
    assert ONION not in body
    assert 'Onion art' not in body   # nor in the community chooser


def test_a_logged_in_modlog_still_lists_them(app, world, entries):
    body = visit(app, '/modlog', True, world).get_data(as_text=True)
    assert {'MARK-HELL', 'MARK-ONION-COMMUNITY', 'MARK-ONION-TARGET'} <= set(body.split())
    assert ONION_ART in body


@pytest.mark.parametrize('signed_in, listed', [(False, 0), (True, 1)])
def test_the_api_modlog_follows_the_same_rule(app, world, entries, monkeypatch, signed_in, listed):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    forget_login()
    from_api = app.test_client().get('/api/alpha/modlog', base_url=HOSTS[HELL],
                                     headers={'Authorization': f'Bearer {world.hell_bob.encode_jwt_token()}'}
                                     if signed_in else {})
    assert from_api.status_code == 200
    body = from_api.get_data(as_text=True)
    assert f'https://{HELL}/c/art' in body
    assert (ONION in body) == bool(listed)


def test_the_modlog_name_suggestions_leave_out_local_only_accounts_when_logged_out(app, world):
    forget_login()
    logged_out = app.test_client().post('/modlog/search_suggestions', data={'user_name': 'bob'},
                                        base_url=HOSTS[HELL]).get_data(as_text=True)
    assert logged_out.count('<option') == 2   # hell's and the shop's bob, not the onion's


# --- people -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/instance/all/people', '/instance/people/interesting'])
def test_logged_out_people_lists_leave_out_local_only_accounts(app, world, path):
    body = visit(app, path, False, world).get_data(as_text=True)
    assert 'href="/u/bob"' in body
    assert ONION not in body


@pytest.mark.parametrize('path', ['/instance/all/people', '/instance/people/interesting'])
def test_logged_in_people_lists_still_show_them(app, world, path):
    assert f'/u/{ONION_BOB}' in visit(app, path, True, world).get_data(as_text=True)
