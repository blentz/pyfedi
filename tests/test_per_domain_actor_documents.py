"""An account's or community's ActivityPub document names its own domain (plan C3, task 2).

shop.example federates beside hell.cloud. Owner ruling: nothing a sister domain federates names hell.cloud, so every
URL in a shop account's or community's document, its collections, the Group Update it sends and the shop's instance
actor is on shop.example, and none of those bodies holds hell.cloud's host (the test app's SERVER_NAME) at all.
hell.cloud's own documents are pinned byte for byte in tests/test_c3_golden_documents.py. The onion never federates,
so its instance actor answers no other server."""
import json
from unittest.mock import MagicMock, patch

import pytest
from flask import g

import app.shared.tasks.groups as groups_module
from app import cache, cli, db
from app.models import Community, CommunityMember, File, Instance, Language, SiteDomain, User, UserExtraField
from app.shared.tasks.groups import edit_community
from app.storefront import default_domain, domain_for_host
from tests.factories import a_keypair, grant_permission, make_user
from tests.test_dev_tools import csrf
from tests.test_c3_golden_documents import AP, CREATED, SHOP, UPDATED, golden_site, pem  # noqa: F401  (fixture)
from tests.test_onion_guard_limits import PROXY, a_session, onion_get
from tests.two_domains import HELL, two_domains  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures('golden_site')

SHOP_BASE = f'https://{SHOP}'


@pytest.fixture(autouse=True)
def onion_proxy(app, monkeypatch):
    monkeypatch.setitem(app.config, 'ONION_PROXY_IP', PROXY)


@pytest.fixture
def shop_rows(app):
    """goldie and goldart again, on shop.example: the same names as hell's, so a lookup or URL that ignores the
    domain shows."""
    shop = domain_for_host(SHOP)
    avatar, cover = File(file_path='app/static/media/users/sh/op/avatar.webp'), \
        File(thumbnail_path='app/static/media/users/sh/op/cover.webp')
    icon, banner = File(file_path='app/static/media/communities/sh/op/icon.webp'), \
        File(file_path='app/static/media/communities/sh/op/banner.webp')
    db.session.add_all([avatar, cover, icon, banner])
    db.session.flush()
    user = User(user_name='goldie', title='Shop goldie', email='goldie@shop.example', instance_id=shop.instance_id,
                site_domain_id=shop.id, verified=True, banned=False, public_key=pem('SHOPUSER'), created=CREATED,
                ap_profile_id=f'{SHOP_BASE}/u/goldie', ap_public_url=f'{SHOP_BASE}/u/goldie',
                ap_inbox_url=f'{SHOP_BASE}/u/goldie/inbox', avatar_id=avatar.id, cover_id=cover.id)
    db.session.add(user)
    db.session.flush()
    db.session.add(UserExtraField(user_id=user.id, label='Site', text='https://goldie.example'))
    community = Community(name='goldart', title='Shop art', instance_id=shop.instance_id, site_domain_id=shop.id,
                          user_id=user.id, public_key=pem('SHOPCOMMUNITY'), created_at=CREATED, last_active=UPDATED,
                          ap_profile_id=f'{SHOP_BASE}/c/goldart', ap_public_url=f'{SHOP_BASE}/c/goldart',
                          ap_followers_url=f'{SHOP_BASE}/c/goldart/followers',
                          ap_moderators_url=f'{SHOP_BASE}/c/goldart/moderators', ap_domain=SHOP,
                          subscriptions_count=1, local_only=False, nsfw=False, icon_id=icon.id, image_id=banner.id)
    db.session.add(community)
    db.session.flush()
    db.session.add(CommunityMember(user_id=user.id, community_id=community.id, is_moderator=True, is_owner=True))
    db.session.commit()
    cache.clear()
    return shop, user, community


def served(app, path, host=SHOP):
    response = app.test_client().get(path, headers=AP, base_url=f'https://{host}')
    assert response.status_code == 200, (path, response.status_code)
    return response


def assert_on_shop(body: bytes):
    assert HELL.encode() not in body   # hell.cloud's host is nowhere in the document, in any field


def on_shop(url: str) -> bool:
    return url == SHOP_BASE or url.startswith(f'{SHOP_BASE}/') or url.startswith(f'{SHOP_BASE}#')


def test_a_shop_accounts_document_names_only_shop(app, shop_rows):
    response = served(app, '/u/goldie')
    assert_on_shop(response.data)
    document = json.loads(response.data)
    for field in ('id', 'inbox', 'outbox', 'followers', 'following', 'liked'):
        assert on_shop(document[field]), field
    assert document['id'] == f'{SHOP_BASE}/u/goldie'
    assert document['endpoints'] == {'sharedInbox': f'{SHOP_BASE}/inbox'}
    assert document['publicKey']['id'] == f'{SHOP_BASE}/u/goldie#main-key'
    assert document['publicKey']['owner'] == f'{SHOP_BASE}/u/goldie'
    assert document['icon']['url'] == f'{SHOP_BASE}/static/media/users/sh/op/avatar.webp'
    assert document['image']['url'] == f'{SHOP_BASE}/static/media/users/sh/op/cover.webp'
    assert document['attributionDomains'] == [SHOP]
    assert response.headers['Link'] == f'<{SHOP_BASE}/u/goldie>; rel="alternate"; type="text/html"'


def test_a_shop_communitys_document_names_only_shop(app, shop_rows):
    response = served(app, '/c/goldart')
    assert_on_shop(response.data)
    document = json.loads(response.data)
    for field in ('id', 'inbox', 'outbox', 'followers', 'following', 'moderators', 'featured', 'attributedTo',
                  'url'):
        assert on_shop(document[field]), field
    assert document['id'] == document['url'] == f'{SHOP_BASE}/c/goldart'
    assert document['endpoints'] == {'sharedInbox': f'{SHOP_BASE}/inbox'}
    assert document['publicKey']['id'] == f'{SHOP_BASE}/c/goldart#main-key'
    assert document['publicKey']['owner'] == f'{SHOP_BASE}/c/goldart'
    assert document['icon']['url'] == f'{SHOP_BASE}/static/media/communities/sh/op/icon.webp'
    assert document['image']['url'] == f'{SHOP_BASE}/static/media/communities/sh/op/banner.webp'
    assert response.headers['Link'] == f'<{SHOP_BASE}/c/goldart>; rel="alternate"; type="text/html"'


@pytest.mark.parametrize('path', ['/c/goldart/outbox', '/c/goldart/followers', '/c/goldart/featured',
                                  '/c/goldart/moderators', '/c/goldart/following', '/u/goldie/outbox',
                                  '/u/goldie/followers', '/u/goldie/following'])
def test_a_shop_collection_names_only_shop(app, shop_rows, path):
    response = served(app, path)
    assert_on_shop(response.data)
    assert json.loads(response.data)['id'] == f'{SHOP_BASE}{path}'


def test_the_shops_instance_actor_is_the_shops(app, shop_rows):
    shop = shop_rows[0]
    response = served(app, '/actor')
    assert_on_shop(response.data)
    document = json.loads(response.data)
    assert document['id'] == f'{SHOP_BASE}/actor'
    assert document['preferredUsername'] == document['name'] == SHOP
    assert document['url'] == f'{SHOP_BASE}/about'
    assert document['inbox'] == f'{SHOP_BASE}/actor/inbox'
    assert document['outbox'] == f'{SHOP_BASE}/actor/outbox'
    assert document['endpoints'] == {'sharedInbox': f'{SHOP_BASE}/inbox'}
    assert document['publicKey'] == {'id': f'{SHOP_BASE}/actor#main-key', 'owner': f'{SHOP_BASE}/actor',
                                     'publicKeyPem': db.session.get(SiteDomain, shop.id).public_key}
    assert document['publicKey']['publicKeyPem'] != pem('SITE')


def test_the_shops_instance_actor_outbox_is_the_shops(app, shop_rows):
    response = served(app, '/actor/outbox')
    assert_on_shop(response.data)
    assert json.loads(response.data)['id'] == f'{SHOP_BASE}/actor/outbox'


def test_the_shops_application_document_is_the_shops(app, shop_rows):
    shop = shop_rows[0]
    hell_first = served(app, '/', host=HELL).data   # cached per host: hell's copy never answers the shop
    response = served(app, '/')
    assert_on_shop(response.data)
    document = json.loads(response.data)
    assert document['id'] == f'{SHOP_BASE}/'
    assert document['inbox'] == f'{SHOP_BASE}/inbox'
    assert document['outbox'] == f'{SHOP_BASE}/site_outbox'
    assert document['icon']['url'] == f'{SHOP_BASE}/static/images/piefed_logo_icon_t_75.png'
    assert document['publicKey'] == {'id': f'{SHOP_BASE}/#main-key', 'owner': f'{SHOP_BASE}/',
                                     'publicKeyPem': db.session.get(SiteDomain, shop.id).public_key}
    assert served(app, '/', host=HELL).data == hell_first   # and the shop's never answers hell


@pytest.mark.parametrize('path', ['/actor', '/actor/outbox', '/'])
def test_the_onions_instance_actor_answers_no_other_server(app, path):
    client = app.test_client()
    a_session(client)
    assert onion_get(client, path, headers=AP).status_code == 404


def test_hells_feed_stays_hells_whatever_host_asks(app, shop_rows):
    """A feed has no domain of its own: its owner is the default domain (Task 1's owner_domain)."""
    assert json.loads(served(app, '/f/goldfeed').data)['id'] == f'https://{HELL}/f/goldfeed'


# --------------------------------------------------------------------------
# the Group Update a moderator's edit sends, from a Celery worker (no request)
# --------------------------------------------------------------------------

def test_the_group_update_names_the_shop(app, shop_rows, monkeypatch):
    shop, user, community = shop_rows
    user.private_key, user.public_key = a_keypair()
    community.private_key, community.public_key = a_keypair()
    peer = Instance(domain='peer.example', inbox='https://peer.example/inbox', software='lemmy')
    db.session.add(peer)
    db.session.flush()
    follower = make_user(peer, 'follower')
    db.session.add(CommunityMember(user_id=follower.id, community_id=community.id))
    db.session.commit()
    sent = []
    monkeypatch.setattr(groups_module, 'send_post_request', lambda inbox, body, *args: sent.append((inbox, body, args)))

    edit_community(False, user.id, community.id)

    [(inbox, announce, (_, key_id))] = sent
    assert inbox == 'https://peer.example/inbox'
    assert HELL not in json.dumps(announce)
    assert key_id == f'{SHOP_BASE}/c/goldart#main-key'
    assert announce['id'].startswith(f'{SHOP_BASE}/activities/announce/')
    update, group = announce['object'], announce['object']['object']
    assert update['id'].startswith(f'{SHOP_BASE}/activities/update/')
    assert group['id'] == f'{SHOP_BASE}/c/goldart'
    assert group['endpoints'] == {'sharedInbox': f'{SHOP_BASE}/inbox'}
    assert group['icon']['url'] == f'{SHOP_BASE}/static/media/communities/sh/op/icon.webp'
    assert group['image']['url'] == f'{SHOP_BASE}/static/media/communities/sh/op/banner.webp'


# --------------------------------------------------------------------------
# what the models fall back to when a local row has no stored URL
# --------------------------------------------------------------------------

def test_a_shop_rows_fallback_urls_are_the_shops(app, shop_rows):
    shop, user, community = shop_rows
    user.ap_profile_id = user.ap_public_url = None
    community.ap_profile_id = community.ap_public_url = None
    user.ap_domain = None
    db.session.commit()
    assert user.profile_id() == f'{SHOP_BASE}/u/goldie'
    assert user.public_url() == f'{SHOP_BASE}/u/goldie'
    assert user.instance_domain() == SHOP
    assert user.mention_tag() == f'@goldie@{SHOP}'
    assert community.profile_id() == f'{SHOP_BASE}/c/goldart'
    assert community.public_url() == f'{SHOP_BASE}/c/goldart'


def test_a_hell_rows_fallback_urls_are_unchanged(app):
    user = User.query.filter_by(user_name='goldie', site_domain_id=default_domain().id).one()
    user.ap_profile_id = user.ap_public_url = user.ap_domain = None
    community = Community.query.filter_by(name='goldart', site_domain_id=default_domain().id).one()
    community.ap_profile_id = community.ap_public_url = None
    db.session.commit()
    server_url = app.config['SERVER_URL']
    assert (user.profile_id(), user.public_url()) == (f'{server_url}/u/goldie', f'{server_url}/u/goldie')
    assert (user.instance_domain(), user.mention_tag()) == (HELL, f'@goldie@{HELL}')
    assert (community.profile_id(), community.public_url()) == (f'{server_url}/c/goldart',
                                                                f'{server_url}/c/goldart')


# --------------------------------------------------------------------------
# creation paths
# --------------------------------------------------------------------------

def test_the_feed_bot_is_made_on_the_default_domain(app):
    cli.register(app)
    result = app.test_cli_runner().invoke(args=['rss-feeds'])
    assert result.exception is None, result.exception
    bot = User.query.filter_by(user_name='feed_bot').one()
    base = app.config['SERVER_URL']
    assert (bot.ap_profile_id, bot.ap_public_url, bot.ap_inbox_url) == \
        (f'{base}/u/feed_bot', f'{base}/u/feed_bot', f'{base}/u/feed_bot/inbox')
    assert bot.site_domain_id == default_domain().id


def test_the_cli_admin_is_made_on_the_default_domain(app):
    cli.register(app)
    fake_db = MagicMock()
    fake_db.inspect.return_value.get_table_names.return_value = ['alembic_version']
    fake_db.session.execute.return_value.fetchone.return_value = ('UTF8',)
    answers = iter(['rootish', 'a@example.com', 'password123'])
    with patch('app.cli.db', fake_db), patch('builtins.input', lambda prompt='': next(answers)), \
            patch('app.cli.retrieve_block_list', return_value=''), \
            patch('app.cli.retrieve_peertube_block_list', return_value=''):
        result = app.test_cli_runner().invoke(args=['init-db', '--interactive', 'yes'], input='n\nn\nn\nn\nn\n')
    assert result.exception is None, result.exception
    [admin] = [call.args[0] for call in fake_db.session.add.call_args_list if isinstance(call.args[0], User)]
    base = app.config['SERVER_URL']
    assert (admin.ap_profile_id, admin.ap_public_url, admin.ap_inbox_url) == \
        (f'{base}/u/rootish', f'{base}/u/rootish', f'{base}/u/rootish/inbox')


def _logged_in(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def test_dev_communities_are_made_on_their_creators_domain(app, shop_rows):
    shop, user, _ = shop_rows
    grant_permission(user, 'change instance settings')
    was = app.debug
    app.debug = True
    try:
        client = _logged_in(app, user)
        with patch('app.dev.routes.render_template', return_value='rendered'), \
                patch('app.dev.routes.RsaKeys.generate_keypair', return_value=a_keypair()):
            response = client.post('/dev/tools', data={'communities_submit': 'Go', 'csrf_token': csrf(app, client)})
    finally:
        app.debug = was
    assert response.status_code == 302
    made = Community.query.filter(Community.name.like('dev_Community_%')).all()
    assert len(made) == 30
    for community in made:
        assert community.ap_profile_id == f'{SHOP_BASE}/c/{community.name.lower()}'
        assert community.ap_public_url == f'{SHOP_BASE}/c/{community.name}'
        assert community.ap_followers_url == f'{SHOP_BASE}/c/{community.name.lower()}/followers'
        assert (community.ap_domain, community.site_domain_id, community.instance_id) == \
            (SHOP, shop.id, shop.instance_id)


def _an_admin(app):
    admin = User.query.filter_by(user_name='goldie', site_domain_id=default_domain().id).one()
    grant_permission(admin, 'administer all communities')
    grant_permission(admin, 'change instance settings')
    g.admin_ids = [admin.id]
    return admin


def test_an_admin_rename_keeps_a_shop_community_on_the_shop(app, shop_rows):
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    community = shop_rows[2]
    client = _logged_in(app, _an_admin(app))
    data = {'csrf_token': csrf(app, client), 'url': 'renamed', 'title': 'Shop art', 'description': '', 'rules': '',
            'content_retention': -1, 'topic': -1, 'default_layout': '', 'posting_warning': '',
            'downvote_accept_mode': 0}
    response = client.post(f'/admin/community/{community.id}/edit', data=data)
    assert response.status_code == 302
    db.session.refresh(community)
    base = f'{SHOP_BASE}/c/renamed'
    assert (community.ap_profile_id, community.ap_public_url) == (base, base)
    assert (community.ap_followers_url, community.ap_featured_url, community.ap_moderators_url) == \
        (f'{base}/followers', f'{base}/featured', f'{base}/moderators')
    assert community.ap_domain == SHOP


def test_an_admin_move_makes_a_default_domain_community(app):
    remote = Instance(domain='peer.example', software='lemmy')
    db.session.add(remote)
    db.session.flush()
    community = Community(name='moving', title='Moving', instance_id=remote.id, user_id=1, ap_id='moving@peer.example',
                          ap_profile_id='https://peer.example/c/moving', ap_domain='peer.example')
    db.session.add(community)
    db.session.commit()
    admin = _an_admin(app)
    client = _logged_in(app, admin)
    with patch('app.admin.routes.RsaKeys.generate_keypair', return_value=a_keypair()):
        response = client.post(f'/admin/community/{community.id}/move/{admin.id}',
                               data={'csrf_token': csrf(app, client), 'new_url': 'moved', 'new_owner': 'y'})
    assert response.status_code == 302
    db.session.refresh(community)
    base = f"{app.config['SERVER_URL']}/c/moved"
    assert (community.ap_profile_id, community.ap_public_url, community.ap_followers_url) == \
        (base, base, f'{base}/followers')
    assert (community.ap_domain, community.site_domain_id) == (HELL, default_domain().id)
