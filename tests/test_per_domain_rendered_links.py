"""The links and redirects a page builds name the row it shows, from either storefront (plan C1, final fix wave).

`bob` and `art` are on hell.cloud and on the onion (`world`, from test_per_domain_html_routes). Each test reads a URL
the app built, from the page or the redirect, and follows it: a link built from the bare name names the storefront's
own namesake, so following it acts on, or shows, the wrong row on one of the two storefronts."""
import re

import pytest

from app import db
from app.models import UserBlock
from tests.test_per_domain_html_routes import other, signed_in, world  # noqa: F401
from tests.two_domains import HELL, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


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
