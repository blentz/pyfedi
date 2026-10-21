"""The links and redirects a page builds name the row it shows, from either storefront (plan C1, final fix wave).

`bob` and `art` are on hell.cloud and on the onion (`world`, from test_per_domain_html_routes). Each test reads a URL
the app built, from the page or the redirect, and follows it: a link built from the bare name names the storefront's
own namesake, so following it acts on, or shows, the wrong row on one of the two storefronts."""
import glob
import os
import re
from unittest.mock import patch

import pytest
from flask import render_template

from app import db
from app.models import Community, CommunityBlock, Language, Post, Report, Site, UserBlock
from app.post.util import convert_archived_replies_to_tree, retrieve_archived_post
from app.utils import archive_post
from tests.factories import make_post, make_post_reply
from tests.test_community_lifecycle import _add_payload as add_payload
from tests.test_community_lifecycle import able_to_create
from tests.test_per_domain_html_routes import BASE, other, signed_in, world  # noqa: F401
from tests.two_domains import HELL, ONION, default_request, onion_request, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

ARCHIVED_DIR = 'app/static/media/archived'


def hx_posts(page: str, action: str) -> list[str]:
    """The `hx-post` URLs in `page` that end in `/<action>`."""
    return re.findall(rf'hx-post="([^"]*/{action})"', page)


def blocked(viewer) -> set[int]:
    return {b.blocked_id for b in UserBlock.query.filter_by(blocker_id=viewer.id)}


@pytest.mark.parametrize('host', [ONION, HELL])
def test_the_profiles_block_and_unblock_buttons_act_on_the_profiles_account(app, world, host):
    """Critical 1: on one storefront, the other domain's bob's profile blocks and unblocks that bob."""
    viewer, theirs = world.viewers[host], world.bob[other(host)]
    client = signed_in(app, viewer, host)
    page = client.get(f'/u/bob@{other(host)}').get_data(as_text=True)
    [block] = hx_posts(page, 'block')
    assert client.post(block).status_code in (200, 302)
    assert blocked(viewer) == {theirs.id}
    page = client.get(f'/u/bob@{other(host)}').get_data(as_text=True)
    [unblock] = hx_posts(page, 'unblock')
    assert client.post(unblock).status_code in (200, 302)
    assert blocked(viewer) == set()


@pytest.mark.parametrize('host', [ONION, HELL])
def test_the_block_lists_unblock_button_unblocks_that_account(app, world, host, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the page renders its form's token
    viewer, theirs = world.viewers[host], world.bob[other(host)]
    db.session.add(UserBlock(blocker_id=viewer.id, blocked_id=theirs.id))
    db.session.commit()
    client = signed_in(app, viewer, host)
    page = client.get('/user/settings/filters').get_data(as_text=True)
    [unblock] = hx_posts(page, 'unblock')
    assert client.post(unblock).status_code in (200, 302)
    assert blocked(viewer) == set()


def profile_links(html: str) -> list[str]:
    return re.findall(r'href="(/u/[^"]*)"', html)


def shows(app, world, host, path) -> set[str]:
    """Which bob the page at `path` shows, read by its title, as a viewer on `host` sees it."""
    page = signed_in(app, world.viewers[host], host).get(path).get_data(as_text=True)
    return {domain for domain, bob in world.bob.items() if bob.title in page}


@pytest.fixture
def titled(world):
    for domain, bob in world.bob.items():
        bob.title = f'Bob of {domain}'
    db.session.commit()
    return world


@pytest.mark.parametrize('reporter_host', [ONION, HELL])
def test_a_report_about_the_onions_bob_links_to_the_onions_bob_on_hell(app, titled, reporter_host):
    """Important 3. A report written on either storefront about the onion's bob: the admin queue on hell.cloud links
    to the onion's bob, not hell's namesake. (Written on the onion, a storefront-relative spelling stores it bare.)"""
    world = titled
    client = signed_in(app, world.viewers[reporter_host], reporter_host)
    target = 'bob' if reporter_host == ONION else f'bob@{ONION}'
    assert client.post(f'/u/{target}/report', data={'reasons': '7', 'description': 'spam'}).status_code == 302
    report = Report.query.filter_by(suspect_user_id=world.bob[ONION].id).one()
    with default_request(app):
        html = render_template('admin/reports/user_report.html', report=report)
    suspect, reporter = profile_links(html)
    assert shows(app, world, HELL, suspect) == {ONION}
    assert reporter == f'/u/{world.viewers[reporter_host].stored_link()}'


def test_a_follow_notification_from_the_onions_bob_links_to_the_onions_bob(app, titled):
    world = titled
    watcher = world.viewers[HELL]
    follower = signed_in(app, world.bob[ONION], ONION)
    assert follower.post(f'/u/{watcher.user_name}@{HELL}/follow').status_code in (200, 302)
    page = signed_in(app, watcher, HELL).get('/notifications').get_data(as_text=True)
    [link] = [href for href in profile_links(page) if href.startswith('/u/bob')]
    assert shows(app, world, HELL, link) == {ONION}


def test_a_stored_name_is_the_same_on_every_storefront(app, world):
    """Bare on the default domain (hell.cloud's stored data stays byte-identical), `name@domain` on another."""
    for request_on in (default_request, onion_request):
        with request_on(app):
            assert world.bob[HELL].stored_link() == 'bob'
            assert world.bob[ONION].stored_link() == f'bob@{ONION}'
            assert world.art[HELL].stored_link() == 'art'
            assert world.art[ONION].stored_link() == f'art@{ONION}'


def test_an_archived_reply_by_the_onions_bob_links_to_the_onions_bob(app, world):
    """An archive keeps the author's name; the author it is read back into links by the domain the archive kept."""
    post = make_post(world.art[HELL], world.viewers[HELL], f'https://{HELL}/post/archived')
    post.body, post.body_html = 'x' * 300, '<p>archived</p>'
    make_post_reply(post, world.bob[ONION], body='a reply from the onion')
    post.reply_count = 1
    db.session.commit()
    written = set(glob.glob(f'{ARCHIVED_DIR}/*'))
    try:
        archive_post(post.id, None)
        db.session.expire_all()
        replies = retrieve_archived_post(db.session.get(Post, post.id).archived)['replies']
        [reply] = [node['comment'] for node in convert_archived_replies_to_tree(replies, post)]
    finally:
        for path in set(glob.glob(f'{ARCHIVED_DIR}/*')) - written:
            os.unlink(path)
    with default_request(app):
        assert reply.author.link() == f'bob@{ONION}'


# Important 2: links and redirects the app builds. Each follows the URL the app built and reads which row it shows.

def community_shown(app, world, host, path) -> set[str]:
    page = signed_in(app, world.viewers[host], host).get(path).get_data(as_text=True)
    return {domain for domain, art in world.art.items() if art.title in page}


def test_creating_a_community_on_the_onion_lands_on_the_new_community(app, world, two_domains):
    """A hell.cloud account creating on the onion makes a hell.cloud community: the redirect names hell.cloud."""
    creator = world.viewers[HELL]
    able_to_create(creator)
    two_domains.local_community('fresh', two_domains.domains[1]).title = 'Onion fresh'
    db.session.add_all([Language(code='und', name='Undetermined'), Language(code='en', name='English')])
    db.session.get(Site, 1).community_creation_admin_only = False
    db.session.commit()
    visitor = signed_in(app, creator, ONION)
    with patch('app.community.routes.task_selector'):
        response = visitor.client.post('/community/add_local', base_url=visitor.base,
                                       data=add_payload(visitor.token, url='fresh', community_name='Hell fresh'),
                                       content_type='multipart/form-data')
    made = Community.query.filter_by(title='Hell fresh').one()
    assert made.site_domain_id == two_domains.domains[0].id
    page = visitor.get(response.headers['Location']).get_data(as_text=True)
    assert 'Hell fresh' in page and 'Onion fresh' not in page


@pytest.mark.parametrize('host', [ONION, HELL])
def test_a_profiles_pagination_stays_on_the_profiles_account(app, titled, host):
    world = titled
    theirs = other(host)
    for number in range(21):   # one past the overview's page of 20, so page 2 has an item and a link back
        make_post(world.art[theirs], world.bob[theirs], f'{BASE[theirs]}/post/paged-{number}', title=f'paged {number}')
    db.session.commit()
    page = signed_in(app, world.viewers[host], host).get(f'/u/bob@{theirs}?overview_page=2').get_data(as_text=True)
    [back] = {href.replace('&amp;', '&') for href in re.findall(r'href="([^"]*overview_page=1[^"]*)"', page)}
    assert shows(app, world, host, back) == {theirs}


@pytest.mark.parametrize('host', [ONION, HELL])
def test_deleting_a_post_returns_to_its_own_community(app, world, host):
    author = world.viewers[host]
    post = make_post(world.art[other(host)], author, f'{BASE[host]}/post/to-delete', title='doomed')
    db.session.commit()
    with patch('app.shared.post.task_selector'):
        response = signed_in(app, author, host).post(f'/post/{post.id}/delete', data={'reason': ''})
    assert response.status_code == 302
    assert community_shown(app, world, host, response.headers['Location']) == {other(host)}


@pytest.mark.parametrize('host', [ONION, HELL])
def test_the_block_lists_community_link_names_that_domains_community(app, world, host, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the page renders its form's token
    viewer = world.viewers[host]
    db.session.add(CommunityBlock(user_id=viewer.id, community_id=world.art[other(host)].id))
    db.session.commit()
    page = signed_in(app, viewer, host).get('/user/settings/filters').get_data(as_text=True)
    [link] = re.findall(r'href="(/c/art[^"]*)"', page)
    assert community_shown(app, world, host, link) == {other(host)}


# Minors

def test_the_posting_pattern_counts_only_this_accounts_posts(app, world):
    """Minor 5: the hour-of-day graphs counted every account with the name, now another domain's too. The graph is
    an admin's, so the values are read where the view hands them to the template."""
    for number in range(3):
        make_post(world.art[ONION], world.bob[ONION], f'{BASE[ONION]}/post/hours-{number}', title=f'hours {number}')
    db.session.commit()

    def posts_per_hour(path):
        with patch('app.user.routes.render_template', return_value='rendered') as render:
            signed_in(app, world.viewers[HELL], HELL).get(path)
        return render.call_args.kwargs['posting_pattern_values']

    assert posts_per_hour('/u/bob') == [0] * 24
    assert sum(posts_per_hour(f'/u/bob@{ONION}')) == 3


def test_a_communitys_alternate_link_is_lower_case_whatever_its_stored_name(app, two_domains):
    """The ActivityPub document's `Link: rel=alternate` header, byte-identical to before C1 for a name stored with
    capitals (one made through the API)."""
    two_domains.local_community('Art', two_domains.domains[0])
    response = app.test_client().get('/c/ART', headers={'Accept': 'application/activity+json'},
                                     base_url=f'https://{HELL}')
    assert response.status_code == 200
    assert response.headers['Link'] == f'<https://{HELL}/c/art>; rel="alternate"; type="text/html"'
