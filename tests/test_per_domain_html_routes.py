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
from tests.three_domains import SHOP
from tests.two_domains import FIRST, ONION, default_request, onion_request

pytestmark = pytest.mark.usefixtures('site')

BASE = {FIRST: f'https://{FIRST}', SHOP: f'https://{SHOP}', ONION: f'http://{ONION}'}


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
    return ONION if host == FIRST else FIRST


EFFECTS = {
    'block': (lambda viewer: {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=viewer.id)}, {}),
    'follow': (lambda viewer: {f.remote_user_id for f in UserFollower.query.filter_by(local_user_id=viewer.id)}, {}),
    'note': (lambda viewer: {n.target_id for n in UserNote.query.filter_by(user_id=viewer.id)}, {'note': 'hi'}),
}


@pytest.mark.parametrize('host', [ONION, FIRST])
@pytest.mark.parametrize('action', sorted(EFFECTS))
def test_on_the_onion_a_first_profiles_buttons_act_on_first_domains_account(app, per_domain_html_routes_world, host, action):
    """A bare name is the storefront's own bob, a handle the other domain's, whichever storefront the button is on."""
    effect, form = EFFECTS[action]
    viewer = per_domain_html_routes_world.viewers[host]
    client = signed_in(app, viewer, host)
    response = client.post(f'/u/bob/{action}', data=form)
    assert response.status_code in (200, 302)
    assert effect(viewer) == {per_domain_html_routes_world.bob[host].id}
    response = client.post(f'/u/bob@{other(host)}/{action}', data=form)
    if host == FIRST:   # follow-up F6: off the onion its bob is unknown, so the button there is 404
        assert response.status_code == 404
        assert effect(viewer) == {per_domain_html_routes_world.bob[host].id}
        return
    assert response.status_code in (200, 302)
    assert effect(viewer) == {per_domain_html_routes_world.bob[host].id, per_domain_html_routes_world.bob[other(host)].id}


@pytest.mark.parametrize('host', [ONION, FIRST])
def test_unblocking_by_handle_unblocks_that_domains_account(app, per_domain_html_routes_world, host):
    viewer = per_domain_html_routes_world.viewers[host]
    for bob in per_domain_html_routes_world.bob.values():
        db.session.add(UserBlock(blocker_id=viewer.id, blocked_id=bob.id))
    db.session.commit()
    client = signed_in(app, viewer, host)
    if host == FIRST:   # follow-up F6: off the onion its bob is unknown
        assert client.post(f'/u/bob@{other(host)}/unblock').status_code == 404
        return
    assert client.post(f'/u/bob@{other(host)}/unblock').status_code in (200, 302)
    assert {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=viewer.id)} == {per_domain_html_routes_world.bob[host].id}


@pytest.mark.parametrize('host', [ONION, FIRST])
def test_joining_and_leaving_by_name_or_handle_is_that_domains_community(app, per_domain_html_routes_world, host):
    viewer = per_domain_html_routes_world.viewers[host]
    client = signed_in(app, viewer, host)

    def joined():
        return {m.community_id for m in CommunityMember.query.filter_by(user_id=viewer.id)}

    assert client.post('/community/art/subscribe').status_code == 302
    assert joined() == {per_domain_html_routes_world.art[host].id}
    if host == FIRST:   # follow-up F6: off the onion its community is unknown
        assert client.post(f'/community/art@{other(host)}/subscribe').status_code == 404
        assert joined() == {per_domain_html_routes_world.art[host].id}
        return
    assert client.post(f'/community/art@{other(host)}/subscribe').status_code == 302
    assert joined() == {per_domain_html_routes_world.art[host].id, per_domain_html_routes_world.art[other(host)].id}
    assert client.post(f'/community/art@{other(host)}/unsubscribe').status_code == 302
    assert joined() == {per_domain_html_routes_world.art[host].id}
    assert per_domain_html_routes_world.sent == []   # a hosted community is joined and left here, never by a Follow sent to ourselves


@pytest.mark.parametrize('host', [ONION, FIRST])
def test_a_communitys_feed_is_that_domains(app, per_domain_html_routes_world, host):
    cache.clear()
    per_domain_html_routes_world.viewers[host].rss_token = 'viewer-rss-token'   # a feed reader's: the onion's community is local-only
    db.session.commit()
    client = signed_in(app, per_domain_html_routes_world.viewers[host], host)
    mine = client.get('/community/art/feed?token=viewer-rss-token').get_data(as_text=True)
    theirs = client.get(f'/community/art@{other(host)}/feed?token=viewer-rss-token')
    if host == FIRST:   # follow-up F6: off the onion its community's feed is unknown
        assert theirs.status_code == 404
        assert per_domain_html_routes_world.art[host].title in mine and per_domain_html_routes_world.art[other(host)].title not in mine
        return
    theirs = theirs.get_data(as_text=True)
    assert per_domain_html_routes_world.art[host].title in mine and per_domain_html_routes_world.art[other(host)].title not in mine
    assert per_domain_html_routes_world.art[other(host)].title in theirs and per_domain_html_routes_world.art[host].title not in theirs


@pytest.mark.parametrize('host', [ONION, FIRST])
def test_the_block_settings_forms_take_a_bare_name_as_the_storefronts(app, per_domain_html_routes_world, host):
    viewer = per_domain_html_routes_world.viewers[host]
    client = signed_in(app, viewer, host)
    client.post('/user/settings/block/user', data={'username': 'bob'})
    assert {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=viewer.id)} == {per_domain_html_routes_world.bob[host].id}
    client.post('/user/settings/block/community', data={'community_name': 'art'})
    assert {b.community_id for b in CommunityBlock.query.filter_by(user_id=viewer.id)} == {per_domain_html_routes_world.art[host].id}


@pytest.mark.parametrize('kind, row, model_path', [('community', 'art', 'c'), ('user', 'bob', 'u')])
def test_a_lookup_of_a_hosted_name_names_that_domain_or_is_404(app, per_domain_html_routes_world, kind, row, model_path):
    """Signed in: a hosted miss is 404, never a redirect to the bare name, which on this storefront is another row."""
    for host in (FIRST, ONION):
        client = signed_in(app, per_domain_html_routes_world.viewers[host], host)
        response = client.get(f'/{kind}/lookup/{row}/{other(host)}')
        if host == FIRST:   # follow-up F6: off the onion a name there answers as a name nobody holds
            assert response.status_code == 404
        else:
            assert response.headers['Location'] == f'/{model_path}/{row}@{other(host)}'
        response = client.get(f'/{kind}/lookup/{row}/{host}')
        assert response.headers['Location'] == f'/{model_path}/{row}'   # the first domain's own redirect, unchanged
        assert client.get(f'/{kind}/lookup/nobody/{other(host)}').status_code == 404


@pytest.mark.parametrize('path', [f'/u/nobody@{ONION}', f'/c/nobody@{ONION}', f'/u/nobody@{FIRST}'])
def test_a_signed_in_miss_on_a_hosted_handle_is_404_and_fetches_nothing(app, per_domain_html_routes_world, no_http, path):
    """Only a signed-in visitor reaches search_for_user and search_for_community, the lookups that fetch a remote
    handle: a hosted one is in the database or nowhere, so a miss is 404 with nothing fetched."""
    response = signed_in(app, per_domain_html_routes_world.viewers[FIRST], FIRST).get(path, follow_redirects=True)
    assert response.status_code == 404
    assert no_http == []


def test_a_signed_in_miss_on_a_hosted_community_handle_ends_in_404(app, per_domain_html_routes_world):
    client = signed_in(app, per_domain_html_routes_world.viewers[ONION], ONION)
    response = client.get(f'/c/nobody@{FIRST}', follow_redirects=True)
    assert response.status_code == 404
    assert [step.headers['Location'] for step in response.history] == [f'/community/lookup/nobody/{FIRST}']
    # follow-up F6: off the onion a name there is 404 at once, held or not, so not even the redirect tells
    response = signed_in(app, per_domain_html_routes_world.viewers[FIRST], FIRST).get(f'/c/nobody@{ONION}', follow_redirects=True)
    assert response.status_code == 404 and not response.history


@pytest.mark.parametrize('host', [ONION, FIRST])
@pytest.mark.parametrize('field', ['user_name', 'suspect_user_name'])
def test_the_modlog_filters_by_the_domains_account(app, per_domain_html_routes_world, host, field):
    for domain, bob in per_domain_html_routes_world.bob.items():
        column = 'user_id' if field == 'user_name' else 'target_user_id'
        db.session.add(ModLog(**{column: bob.id}, type='mod', action='delete_post', link_text=f'entry-{domain}',
                              public=True))
    db.session.commit()
    client = signed_in(app, per_domain_html_routes_world.viewers[host], host)

    def listed(name):
        text = client.get(f'/modlog?{field}={name}').get_data(as_text=True)
        return {domain for domain in (FIRST, ONION) if f'entry-{domain}' in text}

    assert listed('bob') == {host}
    # follow-up F6: off the onion no entry names its bob
    assert listed(f'bob@{other(host)}') == (set() if host == FIRST else {other(host)})


def test_the_modlog_shows_a_default_domain_handle_as_its_bare_name_on_the_first_domain(app, per_domain_html_routes_world):
    text = signed_in(app, per_domain_html_routes_world.viewers[FIRST], FIRST).get(f'/modlog?suspect_user_name=bob@{FIRST}').get_data(as_text=True)
    assert 'value="bob"' in text
    text = signed_in(app, per_domain_html_routes_world.viewers[ONION], ONION).get(f'/modlog?suspect_user_name=bob@{FIRST}').get_data(as_text=True)
    assert f'value="bob@{FIRST}"' in text


def verify(name, password):
    try:
        return post_user_verify_credentials({'username': name, 'password': password})
    except BlockingIOError:
        return 'refused'


@pytest.mark.usefixtures('cheap_hashes')
def test_api_login_with_a_bare_name_is_the_storefronts_account(app, per_domain_html_routes_world):
    with default_request(app):
        assert verify('bob', 'first password') == {}
        assert verify('bob', 'onion password') == 'refused'
        assert verify(f'bob@{ONION}', 'onion password') == {}
    with onion_request(app):
        assert verify('BOB', 'onion password') == {}
        assert verify('bob', 'first password') == 'refused'
        assert verify(f'bob@{FIRST}', 'first password') == {}


@pytest.mark.usefixtures('cheap_hashes')
def test_api_login_with_an_unhosted_handle_fails_like_a_wrong_password(app, per_domain_html_routes_world, caplog):
    def failure(name, password):
        caplog.clear()
        with default_request(app):
            outcome = verify(name, password)
        return outcome, [record.getMessage() for record in caplog.records if 'login failed' in record.getMessage()]

    wrong_password = failure('bob', 'onion password')
    assert wrong_password[0] == 'refused' and len(wrong_password[1]) == 1
    assert failure('bob@elsewhere.example', 'first password') == wrong_password
    assert failure(f'nobody@{ONION}', 'first password') == wrong_password


def test_the_api_finds_a_person_on_the_storefronts_domain(app, per_domain_html_routes_world):
    for request_on, host in ((default_request, FIRST), (onion_request, ONION)):
        for name, owner in (('bob', host), (f'bob@{other(host)}', other(host))):
            if owner == ONION and host == FIRST:
                continue   # follow-up F6: off the onion its account is not found (test_onion_hidden_on_clearnet)
            data = {'username': name}
            with request_on(app):   # signed in: a logged-out caller is not shown the onion's account at all
                get_user(bearer(per_domain_html_routes_world.viewers[host]), data)
            assert data['person_id'] == per_domain_html_routes_world.bob[owner].id, (host, name)


def test_a_bare_mention_name_is_the_storefronts_account(app, per_domain_html_routes_world):
    with onion_request(app):
        assert stored_user('bob') == per_domain_html_routes_world.bob[ONION]
        assert stored_user('Bob') is None   # case as typed, as this lookup always matched it
    with default_request(app):
        assert stored_user('bob') == per_domain_html_routes_world.bob[FIRST]


@pytest.mark.parametrize('lister', [get_post_list, get_post_list2])
def test_the_api_lists_a_bare_community_names_posts_on_the_storefronts_domain(app, per_domain_html_routes_world, lister):
    for host, art in per_domain_html_routes_world.art.items():
        make_post(art, per_domain_html_routes_world.bob[host], f'{BASE[host]}/post/{host}', title=f'posted on {host}')
    db.session.commit()
    for request_on, host in ((default_request, FIRST), (onion_request, ONION)):
        for name, owner in (('art', host), (f'art@{other(host)}', other(host))):
            with request_on(app):
                g.admin_ids = []
                titles = {post['post']['title'] for post in lister(None, {'community_name': name})['posts']}
            assert titles == {f'posted on {owner}'}, (host, name)


def test_a_feeds_auto_follow_outside_a_request_joins_the_communitys_own_domain(app, per_domain_html_routes_world):
    """The worker that joins a feed's communities has no storefront: the queued name says which domain."""
    viewer = per_domain_html_routes_world.viewers[FIRST]
    viewer.feed_auto_follow = True
    feed = make_local_feed(name='firstfeed')
    feed.user_id = viewer.id
    db.session.commit()
    onion_art, first_art, viewer_id = per_domain_html_routes_world.art[ONION].id, per_domain_html_routes_world.art[FIRST].id, viewer.id
    _feed_add_community(onion_art, 0, feed.id, viewer_id)
    joined = {m.community_id for m in CommunityMember.query.filter_by(user_id=viewer_id)}
    assert joined == {onion_art} and first_art not in joined


def test_a_handle_names_the_rows_own_domain(app, per_domain_html_routes_world):
    assert actor_handle(per_domain_html_routes_world.art[ONION]) == f'art@{ONION}'
    assert actor_handle(per_domain_html_routes_world.bob[FIRST]) == f'bob@{FIRST}'
    assert actor_handle(Community(name='far', ap_id='far@remote.example')) == 'far@remote.example'


@pytest.mark.parametrize('host', [ONION, FIRST])
def test_the_profile_pages_show_the_named_domains_row(app, per_domain_html_routes_world, host):
    """Task 3's HTML pages, checked here by effect: /u/<actor> and /c/<actor> show the row the name means."""
    for domain, bob in per_domain_html_routes_world.bob.items():
        bob.title = f'Bob of {domain}'
    db.session.commit()
    client = signed_in(app, per_domain_html_routes_world.viewers[host], host)
    for path, mine, theirs in (('/u/bob', per_domain_html_routes_world.bob[host].title, per_domain_html_routes_world.bob[other(host)].title),
                               ('/c/art', per_domain_html_routes_world.art[host].title, per_domain_html_routes_world.art[other(host)].title)):
        page = client.get(path).get_data(as_text=True)
        assert mine in page and theirs not in page, path
        response = client.get(f'{path}@{other(host)}')
        if host == FIRST:   # follow-up F6: off the onion its rows are 404
            assert response.status_code == 404, path
            continue
        page = response.get_data(as_text=True)
        assert theirs in page and mine not in page, path


def test_a_name_that_names_nothing_is_404_on_the_onion(app, per_domain_html_routes_world):
    assert signed_in(app, per_domain_html_routes_world.viewers[ONION], ONION).post('/u/!/block').status_code == 404
