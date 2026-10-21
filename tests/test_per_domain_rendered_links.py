"""The links and redirects a page builds name the row it shows, from either storefront (plan C1, final fix wave).

`bob` and `art` are on hell.cloud and on the onion (`world`, from test_per_domain_html_routes). Each test reads a URL
the app built, from the page or the redirect, and follows it: a link built from the bare name names the storefront's
own namesake, so following it acts on, or shows, the wrong row on one of the two storefronts."""
import glob
import os
import re

import pytest
from flask import render_template

from app import db
from app.models import Post, Report, UserBlock
from app.post.util import convert_archived_replies_to_tree, retrieve_archived_post
from app.utils import archive_post
from tests.factories import make_post, make_post_reply
from tests.test_per_domain_html_routes import other, signed_in, world  # noqa: F401
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
