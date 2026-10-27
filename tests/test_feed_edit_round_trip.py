"""Saving the feed edit form unchanged changes nothing, on any storefront (plan F4, final review I1).

The form names each community by its own home domain, so a shop feed's shop community is `cats@shop`, never the first
domain's `cats`: saving it must not drop the shop community, swap in a namesake, send an Add or Remove, or subscribe
the owner to anything."""
from unittest.mock import MagicMock

import pytest

import app.shared.feed as shared_feed
from app import db
from app.models import FeedItem
from tests.test_feed_lifecycle_routes import _capture_form
from tests.test_feed_local_only_contents import public_site  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.three_domains import FIRST, ONION, SHOP, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


def _holds(feed):
    return {item.community_id for item in FeedItem.query.filter_by(feed_id=feed.id)}


@pytest.mark.parametrize('host, owner_on, community_on', [
    (SHOP, 'shop', 'shop'),      # a shop feed holding a shop community, and the first domain has a namesake
    (ONION, 'onion', 'onion'),   # an onion feed holding an onion community
    (FIRST, 'first', 'shop'),    # a first-domain feed holding a shop community
])
def test_saving_the_edit_form_unchanged_changes_nothing(app, three_domains, monkeypatch, host, owner_on, community_on):  # noqa: F811
    d = three_domains
    domains = {'first': d.first, 'shop': d.shop, 'onion': d.onion}
    owner = d.account('editor', domains[owner_on])
    held = d.local_community('cats', domains[community_on])
    namesakes = [d.local_community('cats', domain) for key, domain in domains.items() if key != community_on]
    feed = d.local_feed('mine', domains[owner_on], owner)
    db.session.add(FeedItem(feed_id=feed.id, community_id=held.id))
    feed.num_communities = 1
    db.session.commit()
    announce, subscribe = MagicMock(), MagicMock()
    monkeypatch.setattr(shared_feed, 'announce_feed_add_remove_to_subscribers', announce)
    monkeypatch.setattr('app.community.routes.do_subscribe', subscribe)
    owner.feed_auto_follow = True
    db.session.commit()

    visitor = Visitor(app, owner, host)
    captured, fake_render = _capture_form()   # the test config has no CSRF field, so the page itself cannot render
    monkeypatch.setattr('app.feed.routes.render_template', fake_render)
    visitor.get(f'/feed/{feed.id}/edit')
    text = captured['form'].communities.data
    monkeypatch.undo()
    monkeypatch.setattr(shared_feed, 'announce_feed_add_remove_to_subscribers', announce)
    monkeypatch.setattr('app.community.routes.do_subscribe', subscribe)
    assert text == f'cats@{domains[community_on].domain}'
    response = visitor.post(f'/feed/{feed.id}/edit', {'title': feed.title, 'url': feed.name, 'communities': text, 'description': '',
                                                       'public': 'y', 'parent_feed_id': '0'})

    assert response.status_code == 302
    assert _holds(feed) == {held.id}
    assert not {c.id for c in namesakes} & _holds(feed)
    assert not announce.called and not announce.delay.called and not subscribe.called and not subscribe.delay.called
