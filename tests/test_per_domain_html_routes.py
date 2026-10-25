"""Pages and API calls find a local name on its own domain (plan C1, task 4). `bob` and `art` are on the first domain and on
the onion. Each test gives the two rows different, observable effects (a block, a follow, a note, a membership, a
password, a modlog entry) and checks which row changed, from both storefronts: a lookup that ignores the domain picks
one row for both, so it acts on the wrong one on one of them."""
from types import SimpleNamespace

import pytest
from flask import g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import cache, db
from app.api.alpha.utils.post import get_post_list, get_post_list2
from app.api.alpha.utils.user import get_user, post_user_verify_credentials
from app.local_names import actor_handle
from app.shared.feed import _feed_add_community
from app.models import Community, CommunityBlock, CommunityMember, ModLog, Site, UserBlock, UserFollower, UserNote
from app.mentions import stored_user
from tests.factories import bearer, make_local_feed, make_post
from tests.test_onion_guard_limits import cheap_hashes  # noqa: F401
from tests.two_domains import HELL, ONION, default_request, no_http, onion_request, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

BASE = {HELL: f'https://{HELL}', ONION: f'http://{ONION}'}


@pytest.fixture
def world(app, two_domains, monkeypatch):
    """Onion rows first, so the heap does not hand hell's row to an unscoped lookup by accident of order."""
    hell, onion = two_domains.domains
    onion_bob, onion_art = two_domains.local_user('bob', onion), two_domains.local_community('art', onion)
    hell_bob, hell_art = two_domains.local_user('bob', hell), two_domains.local_community('art', hell)
    onion_art.title, hell_art.title = 'Onion art', 'Hell art'
    onion_bob.set_password('onion password')
    hell_bob.set_password('hell password')
    viewers = {ONION: two_domains.local_user('viewer', onion), HELL: two_domains.local_user('watcher', hell)}
    for viewer in viewers.values():
        viewer.private_key = 'a key'   # an approved account (approval_required)
    db.session.get(Site, 1).private_instance = False   # the RSS feeds answer
    monkeypatch.setitem(app.config, 'RSS_FEEDS', True)
    db.session.commit()
    sent = []
    monkeypatch.setattr('app.community.routes.send_post_request', lambda *args, **kwargs: sent.append(args))
    bob = {HELL: hell_bob, ONION: onion_bob}
    art = {HELL: hell_art, ONION: onion_art}
    return SimpleNamespace(bob=bob, art=art, viewers=viewers, sent=sent)


class Visitor:
    """A browser signed in as `user` on `host`, its session holding a CSRF token that each POST carries."""

    def __init__(self, app, user, host):
        self.client, self.base = app.test_client(), BASE[host]
        with app.test_request_context():
            g.pop('csrf_token', None)   # generate_csrf keeps its token in g, which the suite's app context shares
            self.token = generate_csrf()
            raw = flask_session['csrf_token']
        with self.client.session_transaction(base_url=self.base) as session:
            session.update({'_user_id': str(user.id), '_fresh': True, 'csrf_token': raw})

    def post(self, path, data=None):
        g.pop('_login_user', None)   # the test's app context outlives a request, and Flask-Login's user with it
        return self.client.post(path, data={**(data or {}), 'csrf_token': self.token}, base_url=self.base)

    def get(self, path, **kwargs):
        g.pop('_login_user', None)
        return self.client.get(path, base_url=self.base, **kwargs)


def signed_in(app, user, host):
    return Visitor(app, user, host)


def other(host):
    return ONION if host == HELL else HELL


EFFECTS = {
    'block': (lambda viewer: {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=viewer.id)}, {}),
    'follow': (lambda viewer: {f.remote_user_id for f in UserFollower.query.filter_by(local_user_id=viewer.id)}, {}),
    'note': (lambda viewer: {n.target_id for n in UserNote.query.filter_by(user_id=viewer.id)}, {'note': 'hi'}),
}


@pytest.mark.parametrize('host', [ONION, HELL])
@pytest.mark.parametrize('action', sorted(EFFECTS))
def test_on_the_onion_a_hell_profiles_buttons_act_on_hells_account(app, world, host, action):
    """A bare name is the storefront's own bob, a handle the other domain's, whichever storefront the button is on."""
    effect, form = EFFECTS[action]
    viewer = world.viewers[host]
    client = signed_in(app, viewer, host)
    response = client.post(f'/u/bob/{action}', data=form)
    assert response.status_code in (200, 302)
    assert effect(viewer) == {world.bob[host].id}
    response = client.post(f'/u/bob@{other(host)}/{action}', data=form)
    assert response.status_code in (200, 302)
    assert effect(viewer) == {world.bob[host].id, world.bob[other(host)].id}


@pytest.mark.parametrize('host', [ONION, HELL])
def test_unblocking_by_handle_unblocks_that_domains_account(app, world, host):
    viewer = world.viewers[host]
    for bob in world.bob.values():
        db.session.add(UserBlock(blocker_id=viewer.id, blocked_id=bob.id))
    db.session.commit()
    client = signed_in(app, viewer, host)
    assert client.post(f'/u/bob@{other(host)}/unblock').status_code in (200, 302)
    assert {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=viewer.id)} == {world.bob[host].id}


@pytest.mark.parametrize('host', [ONION, HELL])
def test_joining_and_leaving_by_name_or_handle_is_that_domains_community(app, world, host):
    viewer = world.viewers[host]
    client = signed_in(app, viewer, host)

    def joined():
        return {m.community_id for m in CommunityMember.query.filter_by(user_id=viewer.id)}

    assert client.post('/community/art/subscribe').status_code == 302
    assert joined() == {world.art[host].id}
    assert client.post(f'/community/art@{other(host)}/subscribe').status_code == 302
    assert joined() == {world.art[host].id, world.art[other(host)].id}
    assert client.post(f'/community/art@{other(host)}/unsubscribe').status_code == 302
    assert joined() == {world.art[host].id}
    assert world.sent == []   # a hosted community is joined and left here, never by a Follow sent to ourselves


@pytest.mark.parametrize('host', [ONION, HELL])
def test_a_communitys_feed_is_that_domains(app, world, host):
    cache.clear()
    world.viewers[host].rss_token = 'viewer-rss-token'   # a feed reader's: the onion's community is local-only
    db.session.commit()
    client = signed_in(app, world.viewers[host], host)
    mine = client.get('/community/art/feed?token=viewer-rss-token').get_data(as_text=True)
    theirs = client.get(f'/community/art@{other(host)}/feed?token=viewer-rss-token').get_data(as_text=True)
    assert world.art[host].title in mine and world.art[other(host)].title not in mine
    assert world.art[other(host)].title in theirs and world.art[host].title not in theirs


@pytest.mark.parametrize('host', [ONION, HELL])
def test_the_block_settings_forms_take_a_bare_name_as_the_storefronts(app, world, host):
    viewer = world.viewers[host]
    client = signed_in(app, viewer, host)
    client.post('/user/settings/block/user', data={'username': 'bob'})
    assert {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=viewer.id)} == {world.bob[host].id}
    client.post('/user/settings/block/community', data={'community_name': 'art'})
    assert {b.community_id for b in CommunityBlock.query.filter_by(user_id=viewer.id)} == {world.art[host].id}


@pytest.mark.parametrize('kind, row, model_path', [('community', 'art', 'c'), ('user', 'bob', 'u')])
def test_a_lookup_of_a_hosted_name_names_that_domain_or_is_404(app, world, kind, row, model_path):
    """Signed in: a hosted miss is 404, never a redirect to the bare name, which on this storefront is another row."""
    for host in (HELL, ONION):
        client = signed_in(app, world.viewers[host], host)
        response = client.get(f'/{kind}/lookup/{row}/{other(host)}')
        assert response.headers['Location'] == f'/{model_path}/{row}@{other(host)}'
        response = client.get(f'/{kind}/lookup/{row}/{host}')
        assert response.headers['Location'] == f'/{model_path}/{row}'   # the first domain's own redirect, unchanged
        assert client.get(f'/{kind}/lookup/nobody/{other(host)}').status_code == 404


@pytest.mark.parametrize('path', [f'/u/nobody@{ONION}', f'/c/nobody@{ONION}', f'/u/nobody@{HELL}'])
def test_a_signed_in_miss_on_a_hosted_handle_is_404_and_fetches_nothing(app, world, no_http, path):
    """Only a signed-in visitor reaches search_for_user and search_for_community, the lookups that fetch a remote
    handle: a hosted one is in the database or nowhere, so a miss is 404 with nothing fetched."""
    response = signed_in(app, world.viewers[HELL], HELL).get(path, follow_redirects=True)
    assert response.status_code == 404
    assert no_http == []


def test_a_signed_in_miss_on_a_hosted_community_handle_ends_in_404(app, world):
    client = signed_in(app, world.viewers[HELL], HELL)
    response = client.get(f'/c/nobody@{ONION}', follow_redirects=True)
    assert response.status_code == 404
    assert [step.headers['Location'] for step in response.history] == [f'/community/lookup/nobody/{ONION}']


@pytest.mark.parametrize('host', [ONION, HELL])
@pytest.mark.parametrize('field', ['user_name', 'suspect_user_name'])
def test_the_modlog_filters_by_the_domains_account(app, world, host, field):
    for domain, bob in world.bob.items():
        column = 'user_id' if field == 'user_name' else 'target_user_id'
        db.session.add(ModLog(**{column: bob.id}, type='mod', action='delete_post', link_text=f'entry-{domain}',
                              public=True))
    db.session.commit()
    client = signed_in(app, world.viewers[host], host)

    def listed(name):
        text = client.get(f'/modlog?{field}={name}').get_data(as_text=True)
        return {domain for domain in (HELL, ONION) if f'entry-{domain}' in text}

    assert listed('bob') == {host}
    assert listed(f'bob@{other(host)}') == {other(host)}


def test_the_modlog_shows_a_default_domain_handle_as_its_bare_name_on_the_first_domain(app, world):
    text = signed_in(app, world.viewers[HELL], HELL).get(f'/modlog?suspect_user_name=bob@{HELL}').get_data(as_text=True)
    assert 'value="bob"' in text
    text = signed_in(app, world.viewers[ONION], ONION).get(f'/modlog?suspect_user_name=bob@{HELL}').get_data(as_text=True)
    assert f'value="bob@{HELL}"' in text


def verify(name, password):
    try:
        return post_user_verify_credentials({'username': name, 'password': password})
    except BlockingIOError:
        return 'refused'


@pytest.mark.usefixtures('cheap_hashes')
def test_api_login_with_a_bare_name_is_the_storefronts_account(app, world):
    with default_request(app):
        assert verify('bob', 'hell password') == {}
        assert verify('bob', 'onion password') == 'refused'
        assert verify(f'bob@{ONION}', 'onion password') == {}
    with onion_request(app):
        assert verify('BOB', 'onion password') == {}
        assert verify('bob', 'hell password') == 'refused'
        assert verify(f'bob@{HELL}', 'hell password') == {}


@pytest.mark.usefixtures('cheap_hashes')
def test_api_login_with_an_unhosted_handle_fails_like_a_wrong_password(app, world, caplog):
    def failure(name, password):
        caplog.clear()
        with default_request(app):
            outcome = verify(name, password)
        return outcome, [record.getMessage() for record in caplog.records if 'login failed' in record.getMessage()]

    wrong_password = failure('bob', 'onion password')
    assert wrong_password[0] == 'refused' and len(wrong_password[1]) == 1
    assert failure('bob@elsewhere.example', 'hell password') == wrong_password
    assert failure(f'nobody@{ONION}', 'hell password') == wrong_password


def test_the_api_finds_a_person_on_the_storefronts_domain(app, world):
    for request_on, host in ((default_request, HELL), (onion_request, ONION)):
        for name, owner in (('bob', host), (f'bob@{other(host)}', other(host))):
            data = {'username': name}
            with request_on(app):   # signed in: a logged-out caller is not shown the onion's account at all
                get_user(bearer(world.viewers[host]), data)
            assert data['person_id'] == world.bob[owner].id, (host, name)


def test_a_bare_mention_name_is_the_storefronts_account(app, world):
    with onion_request(app):
        assert stored_user('bob') == world.bob[ONION]
        assert stored_user('Bob') is None   # case as typed, as this lookup always matched it
    with default_request(app):
        assert stored_user('bob') == world.bob[HELL]


@pytest.mark.parametrize('lister', [get_post_list, get_post_list2])
def test_the_api_lists_a_bare_community_names_posts_on_the_storefronts_domain(app, world, lister):
    for host, art in world.art.items():
        make_post(art, world.bob[host], f'{BASE[host]}/post/{host}', title=f'posted on {host}')
    db.session.commit()
    for request_on, host in ((default_request, HELL), (onion_request, ONION)):
        for name, owner in (('art', host), (f'art@{other(host)}', other(host))):
            with request_on(app):
                g.admin_ids = []
                titles = {post['post']['title'] for post in lister(None, {'community_name': name})['posts']}
            assert titles == {f'posted on {owner}'}, (host, name)


def test_a_feeds_auto_follow_outside_a_request_joins_the_communitys_own_domain(app, world):
    """The worker that joins a feed's communities has no storefront: the queued name says which domain."""
    viewer = world.viewers[HELL]
    viewer.feed_auto_follow = True
    feed = make_local_feed(name='hellfeed')
    feed.user_id = viewer.id
    db.session.commit()
    onion_art, hell_art, viewer_id = world.art[ONION].id, world.art[HELL].id, viewer.id
    _feed_add_community(onion_art, 0, feed.id, viewer_id)
    joined = {m.community_id for m in CommunityMember.query.filter_by(user_id=viewer_id)}
    assert joined == {onion_art} and hell_art not in joined


def test_a_handle_names_the_rows_own_domain(app, world):
    assert actor_handle(world.art[ONION]) == f'art@{ONION}'
    assert actor_handle(world.bob[HELL]) == f'bob@{HELL}'
    assert actor_handle(Community(name='far', ap_id='far@remote.example')) == 'far@remote.example'


@pytest.mark.parametrize('host', [ONION, HELL])
def test_the_profile_pages_show_the_named_domains_row(app, world, host):
    """Task 3's HTML pages, checked here by effect: /u/<actor> and /c/<actor> show the row the name means."""
    for domain, bob in world.bob.items():
        bob.title = f'Bob of {domain}'
    db.session.commit()
    client = signed_in(app, world.viewers[host], host)
    for path, mine, theirs in (('/u/bob', world.bob[host].title, world.bob[other(host)].title),
                               ('/c/art', world.art[host].title, world.art[other(host)].title)):
        page = client.get(path).get_data(as_text=True)
        assert mine in page and theirs not in page, path
        page = client.get(f'{path}@{other(host)}').get_data(as_text=True)
        assert theirs in page and mine not in page, path


def test_a_name_that_names_nothing_is_404_on_the_onion(app, world):
    assert signed_in(app, world.viewers[ONION], ONION).post('/u/!/block').status_code == 404
