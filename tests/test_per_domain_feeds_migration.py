"""Each local feed belongs to a hosted domain; a feed name is unique per domain (per-domain feeds, task 1)."""
import importlib.util
from pathlib import Path

import pytest
from flask import g, request, request_finished
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Feed
from tests.factories import make_feed
from tests.three_domains import ONION, get, peer, three_domains, two_domains  # noqa: F401

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'd5a9c3e7f1b4_per_domain_feeds.py'
_spec = importlib.util.spec_from_file_location('per_domain_feeds_migration', MIGRATION)
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


def test_the_revision_chain():
    assert (migration.revision, migration.down_revision) == ('d5a9c3e7f1b4', 'c6e2a8f4b1d9')


def test_the_migration_backfills_each_local_feed_to_its_owners_domain(app, three_domains, peer):
    t = three_domains
    first_owner, shop_owner, onion_owner = (t.account('fo', t.first), t.account('so', t.shop), t.account('oo', t.onion))
    first_feed = t.local_feed('firstf', t.first, first_owner)
    shop_feed = t.local_feed('shopf', t.shop, shop_owner)
    onion_feed = t.local_feed('onionf', t.onion, onion_owner)
    ownerless = t.local_feed('ownerless', t.first)
    remote = make_feed(peer, 'remotef')   # make_feed sets ap_id
    db.session.execute(text('UPDATE feed SET site_domain_id = NULL'))
    db.session.execute(text('UPDATE feed SET user_id = :u WHERE id = :f'), {'u': shop_owner.id, 'f': shop_feed.id})
    db.session.execute(text('UPDATE feed SET user_id = :u WHERE id = :f'), {'u': onion_owner.id, 'f': onion_feed.id})
    db.session.execute(text('UPDATE feed SET user_id = :u WHERE id = :f'), {'u': first_owner.id, 'f': first_feed.id})
    db.session.commit()
    db.session.execute(text(migration.BACKFILL_FEEDS))
    db.session.commit()
    db.session.expire_all()
    stamped = {f.name: f.site_domain_id for f in db.session.query(Feed)}
    assert stamped == {'firstf': t.first.id, 'shopf': t.shop.id, 'onionf': t.onion.id, 'ownerless': t.first.id,
                       'remotef': None}


def test_an_owner_with_no_domain_backfills_to_the_default_domain(app, three_domains):
    t = three_domains
    owner = t.account('nodomain', t.shop)
    feed = t.local_feed('legacy', t.shop, owner)
    db.session.execute(text('UPDATE "user" SET site_domain_id = NULL WHERE id = :u'), {'u': owner.id})
    db.session.execute(text('UPDATE feed SET site_domain_id = NULL'))
    db.session.commit()
    db.session.execute(text(migration.BACKFILL_FEEDS))
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(Feed, feed.id).site_domain_id == t.first.id


def test_a_feed_name_is_unique_per_domain_case_insensitively(app, three_domains):
    t = three_domains
    t.local_feed('news', t.first)
    t.local_feed('news', t.shop)          # two domains: allowed
    with pytest.raises(IntegrityError):
        t.local_feed('News', t.first)
    db.session.rollback()


def test_remote_feeds_are_not_held_to_the_index(app, three_domains, peer):
    make_feed(peer, 'news')
    make_feed(peer, 'news2').name = 'news'
    db.session.commit()
    assert db.session.query(Feed).filter(Feed.name == 'news').count() == 2


@pytest.mark.parametrize('stamped', [None, 'first', 'shop'])
def test_a_feeds_home_domain_is_its_own_or_the_default(app, three_domains, stamped):
    t = three_domains
    feed = t.local_feed('homed', t.first)
    feed.site_domain_id = {None: None, 'first': t.first.id, 'shop': t.shop.id}[stamped]
    db.session.commit()
    assert feed.home_domain() == (t.shop if stamped == 'shop' else t.first)


@pytest.mark.usefixtures('site')
def test_get_on_the_onion_host_is_an_onion_request(app, three_domains):
    """No `onion_proxy` fixture here: `three_domains` itself arranges the onion guard, so the request is the onion's."""
    seen = []

    def note(sender, response, **extra):
        seen.append((g.get('storefront'), request.host))

    request_finished.connect(note, app)
    try:
        get(app, '/', host=ONION)
    finally:
        request_finished.disconnect(note, app)
    assert seen and all(host == ONION and domain.is_onion for domain, host in seen)
